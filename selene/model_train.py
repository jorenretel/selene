import logging
import math
import os
import shutil
from time import strftime, time

import numpy as np
import torch
import torch.nn as nn
from torch.autograd import Variable
from torch.optim.lr_scheduler import ReduceLROnPlateau

from .utils import initialize_logger
from .utils import PerformanceMetrics

logger = logging.getLogger("selene")


class ModelController(object):
    """Methods to train and validate a PyTorch model.

    Parameters
    ----------
    model : torch.nn.Module
    data_sampler : Sampler
    loss_criterion : torch.nn._Loss
    optimizer_class :
    optimizer_args : dict
    batch_size : int
        Specify the batch size to process examples. Should be a power of 2.
    max_steps : int
    report_stats_every_n_steps : int
    output_dir : str
    save_checkpoint_every_n_steps : int|None, optional
        Default is 1000. If None, set to the same value as
        `report_stats_every_n_steps`
    n_validation_samples : int|None, optional
    n_test_samples : int|None, optional
    cpu_n_threads : int, optional
        Default is 32.
    use_cuda : bool, optional
        Default is False. Specify whether CUDA is available for torch
        to use during training.
    data_parallel : bool, optional
        Default is False. Specify whether multiple GPUs are available
        for torch to use during training.
    checkpoint_resume : torch.save object, optional
        Default is None. If `checkpoint_resume` is not None, assumes
        the input is a model saved via `torch.save` that can be
        loaded to resume training.

    Attributes
    ----------
    model : torch.nn.Module
    sampler : Sampler
    criterion : torch.nn._Loss
    optimizer : torch.optim
    batch_size : int
    max_steps : int
    nth_step_report_stats : int
    nth_step_save_checkpoint : int
    use_cuda : bool
    data_parallel : bool
    output_dir : str
    training_loss : list(float)
    nth_step_stats : dict
    """

    def __init__(self,
                 model,
                 data_sampler,
                 loss_criterion,
                 optimizer_class,
                 optimizer_args,
                 batch_size,
                 max_steps,
                 report_stats_every_n_steps,
                 output_dir,
                 save_checkpoint_every_n_steps=1000,
                 report_gt_feature_n_positives=10,
                 n_validation_samples=None,
                 n_test_samples=None,
                 cpu_n_threads=32,
                 use_cuda=False,
                 data_parallel=False,
                 logging_verbosity=2,
                 checkpoint_resume=None):
        self.model = model
        self.sampler = data_sampler
        self.criterion = loss_criterion
        self.optimizer = optimizer_class(
            self.model.parameters(), **optimizer_args)

        self.batch_size = batch_size
        self.max_steps = max_steps
        self.nth_step_report_stats = report_stats_every_n_steps
        self.nth_step_save_checkpoint = None
        if not save_checkpoint_every_n_steps:
            self.nth_step_save_checkpoint = report_stats_every_n_steps
        else:
            self.nth_step_save_checkpoint = save_checkpoint_every_n_steps

        torch.set_num_threads(cpu_n_threads)

        self.use_cuda = use_cuda
        self.data_parallel = data_parallel

        os.makedirs(output_dir, exist_ok=True)
        current_run_output_dir = os.path.join(
            output_dir, strftime("%Y-%m-%d-%H-%M-%S"))
        os.makedirs(current_run_output_dir)
        self.output_dir = current_run_output_dir

        initialize_logger(
            os.path.join(self.output_dir, f"{__name__}.log"),
            verbosity=logging_verbosity)

        if self.data_parallel:
            self.model = nn.DataParallel(model)
            logger.debug("Wrapped model in DataParallel")

        if self.use_cuda:
            self.model.cuda()
            self.criterion.cuda()
            logger.debug("Set modules to use CUDA")

        self._create_validation_set(n_samples=n_validation_samples)
        self._validation_metrics = PerformanceMetrics(
            self.sampler.get_feature_from_index,
            report_gt_feature_n_positives=report_gt_feature_n_positives)

        if "test" in self.sampler.modes:
            self._create_test_set(n_samples=n_test_samples)
            self._test_metrics = PerformanceMetrics(
                self.sampler.get_feature_from_index,
                report_gt_feature_n_positives=report_gt_feature_n_positives)

        self._start_step = 0
        self._min_loss = float("inf")
        if checkpoint_resume is not None:
            self._start_step = checkpoint_resume["step"]
            self._min_loss = checkpoint_resume["min_loss"]
            self.optimizer.load_state_dict(
                checkpoint_resume["optimizer"])
            logger.info(
                ("Resuming from checkpoint: "
                 "step {0}, min loss {1}").format(
                    self._start_step, self._min_loss))

        self.losses = {
            "training": [],
            "validation": [],
        }

    def _create_validation_set(self, n_samples=None):
        t_i = time()
        self._validation_data, self._all_validation_targets = \
            self.sampler.get_validation_set(
                self.batch_size, n_samples=n_samples)
        t_f = time()
        logger.info(("{0} s to load {1} validation examples ({2} validation "
                     "batches) to evaluate after each training step.").format(
                      t_f - t_i,
                      len(self._validation_data) * self.batch_size,
                      len(self._validation_data)))

    def _create_test_set(self, n_samples=None):
        t_i = time()
        self._test_data, self._all_test_targets = \
            self.sampler.get_test_set(
                self.batch_size, n_samples=n_samples)
        t_f = time()
        logger.info(("{0} s to load {1} test examples ({2} test batches) "
                     "to evaluate after all training steps.").format(
                      t_f - t_i,
                      len(self._test_data) * self.batch_size,
                      len(self._test_data)))

    def _get_batch(self):
        t_i_sampling = time()
        batch_sequences, batch_targets = self.sampler.sample(
            batch_size=self.batch_size)
        t_f_sampling = time()
        logger.debug(
            ("[BATCH] Time to sample {0} examples: {1} s.").format(
                 self.batch_size,
                 t_f_sampling - t_i_sampling))
        return (batch_sequences, batch_targets)

    def train_and_validate(self):
        logger.info(
            ("[TRAIN] max_steps: {0}, batch_size: {1}").format(
                self.max_steps, self.batch_size))

        min_loss = self._min_loss
        scheduler = ReduceLROnPlateau(
            self.optimizer, 'max', patience=16, verbose=True,
            factor=0.8)
        for step in range(self._start_step, self.max_steps):
            train_loss = self.train()
            self.losses["training"].append(train_loss)

            # @TODO: if step and step % ...
            if step % self.nth_step_report_stats == 0:
                valid_scores = self.validate()
                validation_loss = valid_scores["loss"]
                self.losses["training"].append(train_loss)
                self.losses["validation"].append(validation_loss)
                if valid_scores["roc_auc"]:
                    scheduler.step(
                        math.ceil(valid_scores["roc_auc"] * 1000.0) / 1000.0)

                is_best = validation_loss < min_loss
                min_loss = min(validation_loss, min_loss)
                self._save_checkpoint({
                    "step": step,
                    "arch": self.model.__class__.__name__,
                    "state_dict": self.model.state_dict(),
                    "min_loss": min_loss,
                    "optimizer": self.optimizer.state_dict()}, is_best)
                logger.info(
                    ("[STATS] step={0}: "
                     "Training loss: {1}, validation loss: {2}.").format(
                        step, train_loss, validation_loss))

            if step % self.nth_step_save_checkpoint == 0:
                self._save_checkpoint({
                    "step": step,
                    "arch": self.model.__class__.__name__,
                    "state_dict": self.model.state_dict(),
                    "min_loss": min_loss,
                    "optimizer": self.optimizer.state_dict()}, False)

    def train(self):
        self.model.train()
        self.sampler.set_mode("train")
        inputs, targets = self._get_batch()

        inputs = torch.Tensor(inputs)
        targets = torch.Tensor(targets)

        if self.use_cuda:
            inputs = inputs.cuda()
            targets = targets.cuda()

        inputs = Variable(inputs)
        targets = Variable(targets)

        predictions = self.model(inputs.transpose(1, 2))
        loss = self.criterion(predictions, targets)

        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()

        return loss.data[0]

    def _evaluate_on_data(self, data_in_batches):
        self.model.eval()

        batch_losses = []
        all_predictions = []

        for (inputs, targets) in data_in_batches:
            inputs = torch.Tensor(inputs)
            targets = torch.Tensor(targets)

            if self.use_cuda:
                inputs = inputs.cuda()
                targets = targets.cuda()

            inputs = Variable(inputs, volatile=True)
            targets = Variable(targets, volatile=True)

            predictions = self.model(inputs.transpose(1, 2))
            loss = self.criterion(predictions, targets)

            all_predictions.append(predictions.data.cpu().numpy())
            batch_losses.append(loss.data[0])

        all_predictions = np.vstack(all_predictions)
        return np.average(batch_losses), all_predictions

    def validate(self):
        average_loss, all_predictions = self._evaluate_on_data(
            self._validation_data)

        average_scores = self._validation_metrics.update(
            self._all_validation_targets, all_predictions)

        for name, score in average_scores.items():
            logger.debug(f"[STATS] average {name}: {score}")
            print(f"[VALIDATE] average {name}: {score}")

        average_scores["loss"] = average_loss
        return average_scores

    def evaluate(self):
        average_loss, all_predictions = self._evaluate_on_data(
            self._test_data)

        average_scores = self._test_metrics.update(
            self._all_test_targets, all_predictions)

        np.savez_compressed(
            os.path.join(self.output_dir, "test_predictions.npz"),
            data=all_predictions)

        for name, score in average_scores.items():
            logger.debug(f"[STATS] average {name}: {score}")
            print(f"[TEST] average {name}: {score}")

        test_performance = os.path.join(
            self.output_dir, "test_performance.txt")
        feature_scores_dict = self._test_metrics.write_feature_scores_to_file(
            test_performance)

        average_scores["loss"] = average_loss
        return (average_scores, feature_scores_dict)

    def write_datasets_to_file(self):
        data_dir = os.path.join(
            self.output_dir, "data")
        os.makedirs(data_dir, exist_ok=True)
        self.sampler.save_datasets_to_file(data_dir)

    def _save_checkpoint(self, state, is_best,
                         dir_path=None,
                         filename="checkpoint.pth.tar"):
        """Saves snapshot of the model state to file.

        Parameters
        ----------
        state : dict
            Information about the state of the model
        is_best : bool
            Is this the model's best performance so far?
        dir_path : str, optional
            Default is None. Will output file to the current working directory
            if no path to directory is specified.
        filename : str, optional
            Default is "checkpoint.pth.tar". Specify the checkpoint filename.

        Returns
        -------
        None
        """
        logger.info("[TRAIN] {0}: Saving model state to file.".format(
            state["step"]))
        cp_filepath = os.path.join(
            self.output_dir, filename)
        torch.save(state, cp_filepath)
        if is_best:
            best_filepath = os.path.join(
                self.output_dir,
                "best_model.pth.tar")
            shutil.copyfile(cp_filepath, best_filepath)
