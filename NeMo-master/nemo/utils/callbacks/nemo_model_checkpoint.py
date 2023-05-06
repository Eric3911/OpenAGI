# Copyright (c) 2023, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

import pytorch_lightning
import torch
from pytorch_lightning.callbacks import ModelCheckpoint
from pytorch_lightning.utilities import rank_zero_info

from nemo.collections.common.callbacks import EMA
from nemo.utils import logging
from nemo.utils.app_state import AppState
from nemo.utils.get_rank import is_global_rank_zero
from nemo.utils.model_utils import inject_model_parallel_rank, uninject_model_parallel_rank


class NeMoModelCheckpoint(ModelCheckpoint):
    """ Light wrapper around Lightning's ModelCheckpoint to force a saved checkpoint on train_end.
    Extends Lightning's on_save_checkpoint func to save the .nemo file. Saves the .nemo file based 
    on the best checkpoint saved (according to the monitor value).
    Also contains func to save the EMA copy of the model.
    """

    def __init__(
        self,
        always_save_nemo: bool = False,
        save_nemo_on_train_end: bool = True,
        save_best_model: bool = False,
        postfix: str = ".nemo",
        n_resume: bool = False,
        model_parallel_size: int = None,
        **kwargs,
    ):
        # Parse and store "extended" parameters: save_best model and postfix.
        self.always_save_nemo = always_save_nemo
        self.save_nemo_on_train_end = save_nemo_on_train_end
        self.save_best_model = save_best_model
        if self.save_best_model and not self.save_nemo_on_train_end:
            logging.warning(
                (
                    "Found save_best_model is True and save_nemo_on_train_end is False. "
                    "Set save_nemo_on_train_end to True to automatically save the best model."
                )
            )
        self.postfix = postfix
        self.previous_best_path = ""
        self.model_parallel_size = model_parallel_size

        # `prefix` is deprecated
        if 'prefix' in kwargs:
            self.prefix = kwargs.pop('prefix')
        else:
            self.prefix = ""

        # Call the parent class constructor with the remaining kwargs.
        super().__init__(**kwargs)

        if self.save_top_k != -1 and n_resume:
            logging.debug("Checking previous runs")
            self.nemo_topk_check_previous_run()

    def nemo_topk_check_previous_run(self):
        try:
            self.best_k_models
            self.kth_best_model_path
            self.best_model_score
            self.best_model_path
        except AttributeError:
            raise AttributeError("Lightning's ModelCheckpoint was updated. NeMoModelCheckpoint will need an update.")
        self.best_k_models = {}
        self.kth_best_model_path = ""
        self.best_model_score = None
        self.best_model_path = ""

        checkpoints = list(path for path in self._saved_checkpoint_paths if not self._is_ema_filepath(path))
        for checkpoint in checkpoints:
            if 'mp_rank' in str(checkpoint) or 'tp_rank' in str(checkpoint):
                checkpoint = uninject_model_parallel_rank(checkpoint)
            checkpoint = str(checkpoint)
            if checkpoint[-10:] == '-last.ckpt':
                continue
            index = checkpoint.find(self.monitor) + len(self.monitor) + 1  # Find monitor in str + 1 for '='
            if index != -1:
                match = re.search('[A-z]', checkpoint[index:])
                if match:
                    value = checkpoint[index : index + match.start() - 1]  # -1 due to separator hypen
                    self.best_k_models[checkpoint] = float(value)
        if len(self.best_k_models) < 1:
            return  # No saved checkpoints yet

        _reverse = False if self.mode == "min" else True

        best_k_models = sorted(self.best_k_models, key=self.best_k_models.get, reverse=_reverse)

        ### This section should be ok as rank zero will delete all excess checkpoints, since all other ranks are
        ### instantiated after rank zero. models_to_delete should be 0 for all other ranks.
        if self.model_parallel_size is not None:
            models_to_delete = len(best_k_models) - self.model_parallel_size * self.save_top_k
        else:
            models_to_delete = len(best_k_models) - self.save_top_k
        logging.debug(f'Number of models to delete: {models_to_delete}')

        # If EMA enabled, delete the additional EMA weights
        ema_enabled = self._has_ema_ckpts(self._saved_checkpoint_paths)

        for _ in range(models_to_delete):
            model = best_k_models.pop(-1)
            self.best_k_models.pop(model)
            self._del_model_without_trainer(model)
            if ema_enabled and self._fs.exists(self._ema_format_filepath(model)):
                self._del_model_without_trainer(self._ema_format_filepath(model))
            logging.debug(f"Removed checkpoint: {model}")

        self.kth_best_model_path = best_k_models[-1]
        self.best_model_path = best_k_models[0]
        self.best_model_score = self.best_k_models[self.best_model_path]

    def on_save_checkpoint(self, trainer, pl_module, checkpoint):
        output = super().on_save_checkpoint(trainer, pl_module, checkpoint)
        if not self.always_save_nemo:
            return output
        # Load the best model and then re-save it
        app_state = AppState()
        if app_state.model_parallel_size is not None and app_state.model_parallel_size > 1:
            logging.warning(f'always_save_nemo will slow down training for model_parallel > 1.')
        # since we are creating tarfile artifacts we need to update .nemo path
        app_state.model_restore_path = os.path.abspath(
            os.path.expanduser(os.path.join(self.dirpath, self.prefix + self.postfix))
        )
        if app_state.model_parallel_size is not None and app_state.model_parallel_size > 1:
            maybe_injected_best_model_path = inject_model_parallel_rank(self.best_model_path)
        else:
            maybe_injected_best_model_path = self.best_model_path

        if self.save_best_model:
            if not os.path.exists(maybe_injected_best_model_path):
                return

            if self.best_model_path == self.previous_best_path:
                return output

            self.previous_model_path = self.best_model_path
            old_state_dict = deepcopy(pl_module.state_dict())
            checkpoint = torch.load(maybe_injected_best_model_path, map_location='cpu')
            if 'state_dict' in checkpoint:
                checkpoint = checkpoint['state_dict']
            # get a new instanace of the model
            pl_module.load_state_dict(checkpoint, strict=True)
            if torch.distributed.is_initialized():
                torch.distributed.barrier()
            pl_module.save_to(save_path=app_state.model_restore_path)
            logging.info(f"New best .nemo model saved to: {app_state.model_restore_path}")
            pl_module.load_state_dict(old_state_dict, strict=True)
        else:
            if torch.distributed.is_initialized():
                torch.distributed.barrier()
            pl_module.save_to(save_path=app_state.model_restore_path)
            logging.info(f"New .nemo model saved to: {app_state.model_restore_path}")
        return output

    def on_train_end(self, trainer, pl_module):
        if trainer.fast_dev_run:
            return None

        # check if we need to save a last checkpoint manually as validation isn't always run based on the interval
        if self.save_last and trainer.val_check_interval != 0:
            should_save_last_checkpoint = False
            if isinstance(trainer.val_check_interval, float) and trainer.val_check_interval % trainer.global_step != 0:
                should_save_last_checkpoint = True
            if isinstance(trainer.val_check_interval, int) and trainer.global_step % trainer.val_check_interval != 0:
                should_save_last_checkpoint = True
            if should_save_last_checkpoint:
                monitor_candidates = self._monitor_candidates(trainer)
                super()._save_last_checkpoint(trainer, monitor_candidates)
        # Call parent on_train_end() to save the -last checkpoint
        super().on_train_end(trainer, pl_module)

        # Load the best model and then re-save it
        if self.save_best_model:
            # wait for all processes
            trainer.strategy.barrier("SaveBestCheckpointConnector.resume_end")
            if self.best_model_path == "":
                logging.warning(
                    f"{self} was told to save the best checkpoint at the end of training, but no saved checkpoints "
                    "were found. Saving latest model instead."
                )
            else:
                self.best_model_path = trainer.strategy.broadcast(self.best_model_path)
                trainer._checkpoint_connector.restore(self.best_model_path)

        if self.save_nemo_on_train_end:
            pl_module.save_to(save_path=os.path.join(self.dirpath, self.prefix + self.postfix))

    def _del_model_without_trainer(self, filepath: str) -> None:
        app_state = AppState()
        if app_state.model_parallel_size is not None and app_state.model_parallel_size > 1:
            # filepath needs to be updated to include mp_rank
            filepath = inject_model_parallel_rank(filepath)

        # each model parallel rank needs to remove its model
        if is_global_rank_zero() or (app_state.model_parallel_size is not None and app_state.data_parallel_rank == 0):
            try:
                self._fs.rm(filepath)
                logging.info(f"Removed checkpoint: {filepath}")
            except:
                logging.info(f"Tried to remove checkpoint: {filepath} but failed.")

    def _ema_callback(self, trainer: 'pytorch_lightning.Trainer') -> Optional[EMA]:
        ema_callback = None
        for callback in trainer.callbacks:
            if isinstance(callback, EMA):
                ema_callback = callback
        return ema_callback

    def _save_checkpoint(self, trainer: 'pytorch_lightning.Trainer', filepath: str) -> None:
        ema_callback = self._ema_callback(trainer)
        if ema_callback is not None:
            with ema_callback.save_original_optimizer_state(trainer):
                super()._save_checkpoint(trainer, filepath)

            # save EMA copy of the model as well.
            with ema_callback.save_ema_model(trainer):
                filepath = self._ema_format_filepath(filepath)
                if self.verbose:
                    rank_zero_info(f"Saving EMA weights to separate checkpoint {filepath}")
                super()._save_checkpoint(trainer, filepath)
        else:
            super()._save_checkpoint(trainer, filepath)

    def _remove_checkpoint(self, trainer: "pytorch_lightning.Trainer", filepath: str) -> None:
        super()._remove_checkpoint(trainer, filepath)
        ema_callback = self._ema_callback(trainer)
        if ema_callback is not None:
            # remove EMA copy of the state dict as well.
            filepath = self._ema_format_filepath(filepath)
            super()._remove_checkpoint(trainer, filepath)

    def _ema_format_filepath(self, filepath: str) -> str:
        return filepath.replace(self.FILE_EXTENSION, f'-EMA{self.FILE_EXTENSION}')

    def _has_ema_ckpts(self, checkpoints: Iterable[Path]) -> bool:
        return any(self._is_ema_filepath(checkpoint_path) for checkpoint_path in checkpoints)

    def _is_ema_filepath(self, filepath: Union[Path, str]) -> bool:
        return str(filepath).endswith(f'-EMA{self.FILE_EXTENSION}')

    @property
    def _saved_checkpoint_paths(self) -> Iterable[Path]:
        return Path(self.dirpath).rglob("*.ckpt")
