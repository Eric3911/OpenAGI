# Copyright (c) 2021, NVIDIA CORPORATION.  All rights reserved.
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

import itertools
from typing import Any, List, Optional, Union

import numpy as np
import torch
from omegaconf.dictconfig import DictConfig
from pytorch_lightning.accelerators import CPUAccelerator
from pytorch_lightning.plugins.precision.native_amp import NativeMixedPrecisionPlugin
from pytorch_lightning.trainer.trainer import Trainer

from nemo.collections.nlp.data.language_modeling.megatron.data_samplers import (
    MegatronPretrainingRandomSampler,
    MegatronPretrainingSampler,
)
from nemo.collections.nlp.data.language_modeling.megatron.gpt_dataset import build_train_valid_test_datasets
from nemo.collections.nlp.models.language_modeling.megatron.gpt_model import GPTModel
from nemo.collections.nlp.models.language_modeling.megatron_base_model import MegatronBaseModel
from nemo.collections.nlp.modules.common.megatron.build_model import build_model
from nemo.collections.nlp.modules.common.megatron.module import Float16Module
from nemo.collections.nlp.modules.common.megatron.utils import (
    average_losses_across_data_parallel_group,
    get_all_params_for_weight_decay_optimization,
    get_params_for_weight_decay_optimization,
)
from nemo.collections.nlp.modules.common.text_generation_utils import (
    generate,
    get_computeprob_response,
    get_default_length_params,
    get_default_sampling_params,
    megatron_gpt_generate,
)
from nemo.collections.nlp.modules.common.transformer.text_generation import (
    LengthParam,
    OutputType,
    SamplingParam,
    TextGeneration,
)
from nemo.collections.nlp.parts.nlp_overrides import GradScaler
from nemo.collections.nlp.parts.utils_funcs import get_last_rank
from nemo.core.classes.common import PretrainedModelInfo
from nemo.utils import logging

try:
    import apex.transformer.pipeline_parallel.utils
    from apex.transformer.pipeline_parallel.utils import get_num_microbatches

    HAVE_APEX = True

except (ImportError, ModuleNotFoundError):

    HAVE_APEX = False

try:
    from megatron.core import parallel_state
    from megatron.core.pipeline_parallel.schedules import get_forward_backward_func

    HAVE_MEGATRON_CORE = True

except (ImportError, ModuleNotFoundError):

    HAVE_MEGATRON_CORE = False

try:
    import transformer_engine

    HAVE_TE = True

except (ImportError, ModuleNotFoundError):
    HAVE_TE = False


class MegatronGPTModel(MegatronBaseModel, TextGeneration):
    """
    Megatron GPT pretraining
    """

    def __init__(self, cfg: DictConfig, trainer: Trainer):
        if not HAVE_APEX:
            raise ImportError(
                "Apex was not found. Please see the NeMo README for installation instructions: https://github.com/NVIDIA/NeMo#megatron-gpt."
            )

        if not HAVE_MEGATRON_CORE:
            raise ImportError(
                "megatron-core was not found. Please see the NeMo README for installation instructions: https://github.com/NVIDIA/NeMo#megatron-gpt."
            )
        # this prevents base constructor from initializing tokenizer
        self.tokenizer = None
        super().__init__(cfg, trainer=trainer, no_lm_init=True)

        self._validate_trainer()

        self.megatron_amp_o2 = cfg.get('megatron_amp_O2', False)

        if not self.megatron_amp_o2 and self.cfg.get('virtual_pipeline_model_parallel_size', None):
            raise ValueError('Virtual pipeline model parallel is only supported when using megatron_amp_O2')

        # build_model returns a list of modules which are used for interleaved pipeline parallelism
        if isinstance(self.trainer.accelerator, CPUAccelerator):
            self.model = build_model(
                model_provider_func=self.model_provider_func,
                wrap_with_ddp=False,
                on_cpu=True,
                virtual_pipeline_model_parallel_size=self.cfg.get('virtual_pipeline_model_parallel_size', None),
            )
        else:
            self.model = build_model(
                model_provider_func=self.model_provider_func,
                wrap_with_ddp=False,
                virtual_pipeline_model_parallel_size=self.cfg.get('virtual_pipeline_model_parallel_size', None),
            )

        # if we're not using interleaved, then self.model is a module.
        if self.cfg.get('virtual_pipeline_model_parallel_size', None) is None:
            self.model = self.model[0]

        if self.megatron_amp_o2:

            if not self.with_distributed_adam:
                # Pre-allocate the model on GPU to have master parameters allocated on the same device with matching data type
                if isinstance(self.model, list):
                    for module in self.model:
                        module.cuda(torch.cuda.current_device())
                else:
                    self.model.cuda(torch.cuda.current_device())

            # Model wrapper to convert both model and inputs to half precision
            if isinstance(self.model, list):
                converted_model = []
                for module in self.model:
                    converted_model.append(Float16Module(module=module, precision=cfg.precision))
                    self.model = converted_model
            else:
                self.model = Float16Module(module=self.model, precision=cfg.precision)

        if self.trainer.precision == 'bf16':
            self.autocast_dtype = torch.bfloat16
        elif int(self.trainer.precision) == 32:
            self.autocast_dtype = torch.float
        elif int(self.trainer.precision) == 16:
            self.autocast_dtype = torch.half
        else:
            raise ValueError('precision must be in [32, 16, "bf16"]')

        self.transformer_engine = cfg.get('transformer_engine', False)

        # configuration used for inference
        self._inference_config = None

        # Convert the global-batch-based profile index to micro-batch index
        if hasattr(self, '_nsys_profile_enabled'):
            mp_size = cfg.get('tensor_model_parallel_size', 1) * cfg.get('pipeline_model_parallel_size', 1)
            data_parallel_world_size = trainer.world_size // mp_size
            grad_accum_steps = cfg.get('global_batch_size') // (cfg.get('micro_batch_size') * data_parallel_world_size)
            self._nsys_profile_start_step *= grad_accum_steps
            self._nsys_profile_end_step *= grad_accum_steps

        self.get_attention_mask_from_fusion = self.cfg.get('get_attention_mask_from_fusion', True)

    def get_gpt_module_list(self):
        if isinstance(self.model, list):
            return [model.module if isinstance(model, Float16Module) else model for model in self.model]
        elif isinstance(self.model, Float16Module):
            return [self.model.module]
        else:
            return [self.model]

    def set_inference_config(self, inference_config):
        self._inference_config = inference_config

    def get_inference_config(self):
        return self._inference_config

    def model_provider_func(self, pre_process, post_process):
        """Model depends on pipeline paralellism."""
        model = GPTModel(
            vocab_size=self.padded_vocab_size,
            hidden_size=self.cfg.hidden_size,
            max_position_embeddings=self.cfg.max_position_embeddings,
            num_layers=self.cfg.num_layers,
            num_attention_heads=self.cfg.num_attention_heads,
            apply_query_key_layer_scaling=self.cfg.get('apply_query_key_layer_scaling', True),
            kv_channels=self.cfg.get('kv_channels', None),
            ffn_hidden_size=self.cfg.ffn_hidden_size,
            num_tokentypes=0,
            parallel_output=True,
            pre_process=pre_process,
            post_process=post_process,
            init_method_std=self.cfg.get('init_method_std', 0.02),
            use_scaled_init_method=self.cfg.get('use_scaled_init_method', True),
            fp16_lm_cross_entropy=self.cfg.get('fp16_lm_cross_entropy', False),
            use_cpu_initialization=self.cfg.get('use_cpu_initialization', False),
            hidden_dropout=self.cfg.get('hidden_dropout', 0.1),
            attention_dropout=self.cfg.get('attention_dropout', 0.1),
            ffn_dropout=self.cfg.get('ffn_dropout', 0.0),
            precision=self.cfg.get('precision', 16),
            fp32_residual_connection=self.cfg.get('fp32_residual_connection', False),
            activations_checkpoint_granularity=self.cfg.get('activations_checkpoint_granularity', None),
            activations_checkpoint_method=self.cfg.get('activations_checkpoint_method', None),
            activations_checkpoint_num_layers=self.cfg.get('activations_checkpoint_num_layers', 1),
            activations_checkpoint_layers_per_pipeline=self.cfg.get(
                'activations_checkpoint_layers_per_pipeline', None
            ),
            normalization=self.cfg.get('normalization', 'layernorm'),
            layernorm_epsilon=self.cfg.get('layernorm_epsilon', 1e-5),
            onnx_safe=self.cfg.get('onnx_safe', False),
            bias=self.cfg.get('bias', True),
            bias_activation_fusion=self.cfg.get('bias_activation_fusion', True),
            bias_dropout_add_fusion=self.cfg.get('bias_dropout_add_fusion', True),
            activation=self.cfg.get('activation', 'gelu'),
            headscale=self.cfg.get('headscale', False),
            transformer_block_type=self.cfg.get('transformer_block_type', 'pre_ln'),
            openai_gelu=self.cfg.get('openai_gelu', False),
            normalize_attention_scores=self.cfg.get('normalize_attention_scores', True),
            position_embedding_type=self.cfg.get('position_embedding_type', 'learned_absolute'),
            rotary_percentage=self.cfg.get('rotary_percentage', 1.0),
            share_embeddings_and_output_weights=self.cfg.get('share_embeddings_and_output_weights', True),
            attention_type=self.cfg.get('attention_type', 'multihead'),
            masked_softmax_fusion=self.cfg.get('masked_softmax_fusion', True),
            gradient_accumulation_fusion=self.cfg.get('gradient_accumulation_fusion', False),
            persist_layer_norm=self.cfg.get('persist_layer_norm', False),
            sequence_parallel=self.cfg.get('sequence_parallel', False),
            transformer_engine=self.cfg.get('transformer_engine', False),
            fp8=self.cfg.get('fp8', False),
            fp8_e4m3=self.cfg.get('fp8_e4m3', False),
            fp8_hybrid=self.cfg.get('fp8_hybrid', False),
            fp8_margin=self.cfg.get('fp8_margin', 0),
            fp8_interval=self.cfg.get('fp8_interval', 1),
            fp8_amax_history_len=self.cfg.get('fp8_amax_history_len', 1),
            fp8_amax_compute_algo=self.cfg.get('fp8_amax_compute_algo', 'most_recent'),
            reduce_amax=self.cfg.get('reduce_amax', True),
            use_emha=self.cfg.get('use_emha', False),
        )

        return model

    def setup_optimizer_param_groups(self):
        """ModelPT override. Optimizer will get self._optimizer_param_groups"""
        if self.cfg.get('do_layer_norm_weight_decay', False):
            if isinstance(self.model, list):
                self._optimizer_param_groups = get_all_params_for_weight_decay_optimization(self.model)
            else:
                self._optimizer_param_groups = get_all_params_for_weight_decay_optimization([self.model])

        else:
            self._optimizer_param_groups = get_params_for_weight_decay_optimization(self.model)

    def configure_optimizers(self):

        if self.with_distributed_adam:

            # Disable overlapped grad sync for embedding grad when
            # pipeline parallelism is enabled
            if parallel_state.get_pipeline_model_parallel_world_size() > 1:
                if parallel_state.is_pipeline_first_stage(ignore_virtual=True):
                    if isinstance(self.model, list):
                        module = self.model[0]  # only the first virtual rank has the embeddings
                    else:
                        module = self.model
                    if module.share_token_embeddings:
                        param = module.word_embeddings_weight()
                        param._disable_greedy_grad_copy = not self.megatron_amp_o2
                        param._disable_overlap_grad_sync = True
                if parallel_state.is_pipeline_last_stage(ignore_virtual=True):
                    if isinstance(self.model, list):
                        module = self.model[-1]  # only the last virtual rank has the embeddings
                    else:
                        module = self.model
                    if module.share_token_embeddings:
                        param = module.word_embeddings_weight()
                        param._disable_greedy_grad_copy = not self.megatron_amp_o2
                        param._disable_overlap_grad_sync = True

            # Disable overlapped grad sync for layer norm grads when
            # sequence parallelism is enabled
            for param in self.parameters():
                if getattr(param, 'sequence_parallel', False):
                    param._disable_greedy_grad_copy = not self.megatron_amp_o2
                    param._disable_overlap_grad_sync = True

            # Initialize parameter buckets for overlapped grad and param syncs
            # Note: Params with disabled overlapping are put in the
            # last param bucket
            buckets = []
            if self.cfg.get('virtual_pipeline_model_parallel_size', None) is not None:
                # Initialize a bucket for each virtual pipeline stage
                for module in self.model:
                    if isinstance(module, Float16Module):
                        module = module.module
                    stage_bucket = []
                    for layer in module.language_model.encoder.layers:
                        stage_bucket.extend(
                            p for p in layer.parameters() if not getattr(p, '_disable_overlap_grad_sync', False)
                        )
                    buckets.append(stage_bucket)
            else:
                # Initialize a bucket for each Transformer layer
                modules = self.model if isinstance(self.model, list) else [self.model]
                for module in modules:
                    if isinstance(module, Float16Module):
                        module = module.module
                    for layer in module.language_model.encoder.layers:
                        buckets.append(
                            [p for p in layer.parameters() if not getattr(p, '_disable_overlap_grad_sync', False)]
                        )
            buckets.reverse()
            used_params = set()
            for bucket in buckets:
                used_params.update(bucket)
            buckets[-1].extend(p for p in self.parameters() if p not in used_params)
            self.distributed_adam_buckets = buckets

        return super().configure_optimizers()

    def forward(self, tokens, text_position_ids, attention_mask, labels):
        output_tensor = self.model(tokens, text_position_ids, attention_mask, labels=labels)
        return output_tensor

    def training_step(self, dataloader_iter, batch_idx):
        """
            We pass the dataloader iterator function to the micro-batch scheduler.
            The input batch to each micro-batch is fetched using the dataloader function
            in the micro-batch fwd function.
        """

        # we zero grads here because we also call backward in the megatron-core fwd/bwd functions
        self._optimizer.zero_grad()

        if self.with_distributed_adam:
            # hack to enable overlapping param sync and forward compute
            # note: the distributed optimizer monkey-patches each
            # parameter's __getattribute__ function so that it can
            # launch parameter all-gathers the first time the
            # parameter is accessed after the optimizer step. However,
            # PyTorch directly passes embedding parameters into a C++,
            # bypassing this process. A quick-and-dirty hack is to
            # manually interact with the parameter.
            modules = self.model if isinstance(self.model, list) else [self.model]
            for module in modules:
                if isinstance(module, Float16Module):
                    module = module.module
                module = module.language_model
                if hasattr(module, 'embedding'):
                    for param in module.embedding.parameters():
                        param.data_ptr()

        tensor_shape = [self.cfg.encoder_seq_length, self.cfg.micro_batch_size, self.cfg.hidden_size]

        # run forward and backwards passes for an entire global batch
        # we do this inside training_step to support pipeline parallelism
        fwd_bwd_function = get_forward_backward_func()

        # TODO @akhattar: remove sync related stuff from config, add num_micro_batches_with_partial_activation_checkpoints when ready
        losses_reduced_per_micro_batch = fwd_bwd_function(
            forward_step_func=self.get_forward_output_and_loss_func(),
            data_iterator=dataloader_iter,
            model=[self.model],
            num_microbatches=get_num_microbatches(),
            forward_only=False,
            tensor_shape=tensor_shape,
            dtype=self.autocast_dtype,
            grad_scaler=self.trainer.precision_plugin.scaler if self.cfg.precision == 16 else None,
            sequence_parallel=self.cfg.get('sequence_parallel', False),
            enable_autocast=True,
        )

        # only the last stages of the pipeline return losses
        if losses_reduced_per_micro_batch:
            # average loss across micro batches
            loss_tensors_list = [loss_reduced['avg'] for loss_reduced in losses_reduced_per_micro_batch]
            loss_tensor = torch.concat(loss_tensors_list)
            loss_mean = loss_tensor.mean()
        else:
            loss_mean = torch.tensor(0.0).cuda()

        # when using sequence parallelism, the sequence parallel layernorm grads must be all-reduced
        if self.cfg.get('tensor_model_parallel_size', 1) > 1 and self.cfg.get('sequence_parallel', False):
            self.allreduce_sequence_parallel_gradients()

        if self.with_distributed_adam:
            # synchronize asynchronous grad reductions
            # note: not necessary, but reduces performance degradation
            # from multiple simultaneous NCCL calls
            self._optimizer._finish_bucket_grad_sync()
        elif self.megatron_amp_o2:
            # when using pipeline parallelism grads must be all-reduced after the pipeline (not asynchronously)
            if self.cfg.get('pipeline_model_parallel_size', 1) > 1 or self.cfg.get('sequence_parallel', False):
                # main grads are stored in the MainParamsOptimizer wrapper
                self._optimizer.allreduce_main_grads()
        else:
            # async grad allreduce is not currently implemented for O1/autocasting mixed precision training
            # so we all-reduce gradients after the pipeline
            self.allreduce_gradients()  # @sangkug we think this is causing memory to blow up (hurts perf)

        if self.cfg.get('pipeline_model_parallel_size', 1) > 1 and self.cfg.get(
            'share_embeddings_and_output_weights', True
        ):
            # when using pipeline parallelism the first and last stage must keep embeddings in sync
            self.allreduce_first_last_embeddings()

        ## logging
        # we can only log on one rank if it is rank zero so we broadcast from last rank
        # we can avoid this broadcast by updating the PTL log function to accept specific ranks
        torch.distributed.broadcast(loss_mean, get_last_rank())

        if self.cfg.precision == 16:
            loss_scale = self.trainer.precision_plugin.scaler._scale
            if loss_scale is not None:
                self.log('loss_scale', loss_scale, batch_size=1)

        self.log('reduced_train_loss', loss_mean, prog_bar=True, rank_zero_only=True, batch_size=1)
        lr = self._optimizer.param_groups[0]['lr']
        self.log('lr', lr, rank_zero_only=True, batch_size=1)
        self.log(
            'global_step', self.trainer.global_step, prog_bar=True, rank_zero_only=True, batch_size=1,
        )

        consumed_samples = self.compute_consumed_samples(self.trainer.global_step - self.init_global_step)
        # TODO: make sure compute_consumed_samples works for pipeline parallelism
        self.log(
            'consumed_samples', consumed_samples, prog_bar=True, rank_zero_only=True, batch_size=1,
        )

        if self.cfg.get('rampup_batch_size', None):
            micro_batch_size = self.cfg.get('micro_batch_size', 1)
            total_gpus_number = self.trainer.num_devices * self.trainer.num_nodes
            current_global_batch_size = get_num_microbatches() * micro_batch_size * total_gpus_number
            self.log('global_batch_size', current_global_batch_size, prog_bar=True, rank_zero_only=True, batch_size=1)

            num_microbatch_calculator = apex.transformer.pipeline_parallel.utils._GLOBAL_NUM_MICROBATCHES_CALCULATOR
            num_microbatch_calculator.update(
                consumed_samples=consumed_samples, consistency_check=True,
            )

        return loss_mean

    def backward(self, *args, **kwargs):
        """ LightningModule hook to do backward.
            We want this to do nothing since we run backward in the fwd/bwd functions from megatron-core.
            No need to call it here.
        """
        return

    def optimizer_zero_grad(self, *args, **kwargs):
        """ LightningModule hook to zero grad.
            We want this to do nothing as we are zeroing grads during the training_step.
        """
        return

    def _append_sequence_parallel_module_grads(self, module, grads):
        """ Helper method for allreduce_sequence_parallel_gradients"""

        for param in module.parameters():
            sequence_parallel_param = getattr(param, 'sequence_parallel', False)
            # (@adithyare) adapter training now extends MegatronGPTModel
            # so we have to add this check here to ensure we do not
            # perform all_reduce when grad is None.
            # grad can be None when performing PeFT training.
            if sequence_parallel_param and param.requires_grad:
                if self.megatron_amp_o2:
                    grad = param.main_grad
                else:
                    grad = param.grad
                grads.append(grad.data)

    def allreduce_sequence_parallel_gradients(self):
        """ All-reduce layernorm parameters across model parallel nodes when sequence parallelism is used.
            Modified from megatron-lm:
            https://gitlab-master.nvidia.com/ADLR/megatron-lm/-/blob/3f91f09bb2ab32f9904b47f46f19d2fc3f518ed8/megatron/training.py#L425
        """

        grads = []
        if isinstance(self.model, list):
            for module in self.model:
                self._append_sequence_parallel_module_grads(module, grads)
        else:
            self._append_sequence_parallel_module_grads(self.model, grads)

        coalesced = torch._utils._flatten_dense_tensors(grads)
        torch.distributed.all_reduce(coalesced, group=parallel_state.get_tensor_model_parallel_group())
        for buf, synced in zip(grads, torch._utils._unflatten_dense_tensors(coalesced, grads)):
            buf.copy_(synced)

    def allreduce_first_last_embeddings(self):

        # Modified from megatron-lm: https://github.com/NVIDIA/Megatron-LM/blob/d41696840ed0a7edb7e0499eb82a48ae112d9bb3/megatron/training.py#L407
        # All-reduce word_embeddings' grad across first and last stages to ensure
        # that word_embeddings parameters stay in sync.
        # This should only run for models that support pipelined model parallelism
        # (BERT and GPT-2).
        if parallel_state.get_pipeline_model_parallel_world_size() > 1 and (
            parallel_state.is_pipeline_first_stage(ignore_virtual=True)
            or parallel_state.is_pipeline_last_stage(ignore_virtual=True)
        ):
            if parallel_state.is_pipeline_first_stage(ignore_virtual=True):
                if isinstance(self.model, list):
                    module = self.model[0]  # only the first virtual rank has the embeddings
                else:
                    module = self.model
            if parallel_state.is_pipeline_last_stage(ignore_virtual=True):
                if isinstance(self.model, list):
                    module = self.model[-1]  # only the last virtual rank has the embeddings
                else:
                    module = self.model
            if module.share_token_embeddings:
                word_embeddings_weight = module.word_embeddings_weight()
                # (@adithyare) adapter training now extends MegatronGPTModel so we have to add this check here to ensure we do not perform all_reduce when grad is None.
                # grad can be None when performing PeFT training.
                if word_embeddings_weight.requires_grad:
                    if self.megatron_amp_o2:
                        # O2 recipe stores a "main" copy of weights and grads
                        grad = word_embeddings_weight.main_grad
                    else:
                        grad = word_embeddings_weight.grad
                    torch.distributed.all_reduce(grad, group=parallel_state.get_embedding_group())

    def get_forward_output_and_loss_func(self, validation_step=False):
        def fwd_output_and_loss_func(dataloader_iter, model, checkpoint_activations_all_layers=None):
            if parallel_state.get_pipeline_model_parallel_world_size() == 1:
                batch = next(dataloader_iter)
                for k in batch.keys():
                    if self.get_attention_mask_from_fusion:
                        batch[k] = batch[k].cuda(non_blocking=True) if k not in ['attention_mask'] else None
                    else:
                        batch[k] = batch[k].cuda(non_blocking=True)
            else:
                if parallel_state.is_pipeline_first_stage():
                    batch = next(dataloader_iter)
                    # First pipeline stage needs tokens, position_ids, and attention_mask
                    for k in batch.keys():
                        if self.get_attention_mask_from_fusion:
                            batch[k] = batch[k].cuda(non_blocking=True) if k in ['tokens', 'position_ids'] else None
                        else:
                            batch[k] = (
                                batch[k].cuda(non_blocking=True)
                                if k in ['tokens', 'position_ids', 'attention_mask']
                                else None
                            )
                elif parallel_state.is_pipeline_last_stage():
                    batch = next(dataloader_iter)
                    # Last pipeline stage needs the labels, loss_mask, and attention_mask
                    for k in batch.keys():
                        if self.get_attention_mask_from_fusion:
                            batch[k] = batch[k].cuda(non_blocking=True) if k in ['labels', 'loss_mask'] else None
                        else:
                            batch[k] = (
                                batch[k].cuda(non_blocking=True)
                                if k in ['labels', 'loss_mask', 'attention_mask']
                                else None
                            )
                else:
                    # Intermediate pipeline stage doesn't need any inputs
                    batch = {k: None for k in ['tokens', 'position_ids', 'attention_mask', 'labels']}

            output_tensor = model(
                batch['tokens'],
                batch['position_ids'],
                batch['attention_mask'],
                batch['labels'],
                checkpoint_activations_all_layers=checkpoint_activations_all_layers,
            )

            def loss_func(output_tensor):
                # Loss for a micro-batch (ub)
                loss_for_ub = self.loss_func(batch['loss_mask'], output_tensor)
                if validation_step and not self.cfg.data.get('validation_drop_last', True):
                    num_valid_tokens_in_ub = batch['loss_mask'].sum()
                    if loss_for_ub.isnan():
                        assert batch['loss_mask'].count_nonzero() == 0, 'Got NaN loss with non-empty input'
                        loss_sum_for_ub = torch.zeros_like(num_valid_tokens_in_ub)
                    else:
                        loss_sum_for_ub = num_valid_tokens_in_ub * loss_for_ub

                    loss_sum_and_ub_size_all_gpu = torch.cat(
                        [
                            loss_sum_for_ub.clone().detach().view(1),
                            torch.tensor([num_valid_tokens_in_ub]).cuda().clone().detach(),
                        ]
                    )
                    # Could potentially reduce num_valid_samples_in_microbatch and use that to aggregate instead of len(self._validation_ds)
                    torch.distributed.all_reduce(
                        loss_sum_and_ub_size_all_gpu, group=parallel_state.get_data_parallel_group()
                    )
                    return loss_for_ub, {'loss_sum_and_ub_size': loss_sum_and_ub_size_all_gpu}
                else:
                    reduced_loss = average_losses_across_data_parallel_group([loss_for_ub])
                    return loss_for_ub, {'avg': reduced_loss}

            return output_tensor, loss_func

        return fwd_output_and_loss_func

    def get_forward_output_only_func(self):
        def fwd_output_only_func(batch, model):
            extra_arg = {}
            if len(batch) == 3:
                batch = [x.cuda() for x in batch]
                tokens, attention_mask, position_ids = batch
                attention_mask = attention_mask[0:1]
            else:
                (
                    tokens,
                    attention_mask,
                    position_ids,
                    set_inference_key_value_memory,
                    inference_max_sequence_len,
                ) = batch
                tokens = tokens.cuda()
                attention_mask = attention_mask.cuda()
                position_ids = position_ids.cuda()
                attention_mask = attention_mask[0:1]
                extra_arg['set_inference_key_value_memory'] = set_inference_key_value_memory[0].item()
                extra_arg['inference_max_sequence_len'] = inference_max_sequence_len[0].item()
            output_tensor = model(tokens, position_ids, attention_mask, **extra_arg)

            def id_func(output_tensor):
                return output_tensor, {'logits': output_tensor}

            return output_tensor, id_func

        return fwd_output_only_func

    def validation_step(self, dataloader_iter, batch_idx):
        """
            Our dataloaders produce a micro-batch and then we fetch
            a number of microbatches depending on the global batch size and model parallel size
            from the dataloader to produce a list of microbatches.
            The list of microbatches is then piped through the pipeline using megatron-core fwd/bwd functions.
        """

        tensor_shape = [self.cfg.encoder_seq_length, self.cfg.micro_batch_size, self.cfg.hidden_size]

        # run forward passes for an entire global batch
        # we do this inside validation_step to support pipeline parallelism
        fwd_bwd_function = get_forward_backward_func()

        losses_reduced_per_micro_batch = fwd_bwd_function(
            forward_step_func=self.get_forward_output_and_loss_func(validation_step=True),
            data_iterator=dataloader_iter,
            model=[self.model],
            num_microbatches=get_num_microbatches(),
            forward_only=True,
            tensor_shape=tensor_shape,
            dtype=self.autocast_dtype,
            sequence_parallel=self.cfg.get('sequence_parallel', False),
            enable_autocast=True,
        )

        # only the last stage of the pipeline returns losses
        if losses_reduced_per_micro_batch:
            if self.cfg.data.get('validation_drop_last', True):
                # average loss across micro batches
                loss_tensors_list = [loss_reduced['avg'] for loss_reduced in losses_reduced_per_micro_batch]
                return torch.concat(loss_tensors_list).mean()
            else:
                # Get the total loss since micro batches sizes are not uniform
                loss_sum_tensors_list = [
                    loss_sum['loss_sum_and_ub_size']
                    for loss_sum in losses_reduced_per_micro_batch
                    if loss_sum['loss_sum_and_ub_size'][1] > 0
                ]
                loss_sum = (
                    torch.vstack(loss_sum_tensors_list).sum(axis=0)
                    if len(loss_sum_tensors_list) > 0
                    else torch.tensor([0.0, 0.0]).cuda()
                )
                return loss_sum
        else:
            # we're not on the last pipeline stage so no losses
            return []

    def validation_epoch_end(self, outputs):
        if parallel_state.is_pipeline_last_stage():
            # only the last pipeline parallel stages return loss with their batch size
            if self.cfg.data.get('validation_drop_last', True):
                averaged_loss = torch.stack(outputs).mean()
            else:
                # Compute the avg loss by total_loss across all samples / total number of samples
                total_loss_and_total_samples = torch.vstack(outputs).sum(axis=0)
                avg_loss = total_loss_and_total_samples[0] / total_loss_and_total_samples[1]
                averaged_loss = avg_loss.type(torch.float32).cuda()
        else:
            averaged_loss = torch.tensor(0.0, dtype=torch.float32).cuda()

        # we can only log on one rank if it is rank zero so we broadcast from last rank
        torch.distributed.broadcast(averaged_loss, get_last_rank())

        self.log('val_loss', averaged_loss, prog_bar=True, rank_zero_only=True, batch_size=1)

        return averaged_loss

    def test_step(self, batch, batch_idx):
        return self.validation_step(batch, batch_idx)

    def test_epoch_end(self, outputs):
        averaged_loss = average_losses_across_data_parallel_group(outputs)
        logging.info(f'test_loss: {averaged_loss[0]}')

    def loss_func(self, loss_mask, output_tensor):
        losses = output_tensor.float()
        loss_mask = loss_mask.view(-1).float()
        # TODO: add nemo version here
        loss = torch.sum(losses.view(-1) * loss_mask) / loss_mask.sum()  # sequence level nll
        return loss

    def build_train_valid_test_datasets(self):
        logging.info('Building GPT datasets.')
        if self.trainer.limit_val_batches > 1.0 and isinstance(self.trainer.limit_val_batches, float):
            raise ValueError("limit_val_batches must be an integer or float less than or equal to 1.0.")
        global_batch_size = self.cfg.global_batch_size
        max_train_steps = self.trainer.max_steps
        eval_iters = (max_train_steps // self.trainer.val_check_interval + 1) * self.trainer.limit_val_batches
        test_iters = self.trainer.limit_test_batches

        train_valid_test_num_samples = [
            max_train_steps * global_batch_size,
            eval_iters * global_batch_size,
            test_iters * global_batch_size,
        ]

        if self.trainer.limit_val_batches <= 1.0 and isinstance(self.trainer.limit_val_batches, float):
            train_valid_test_num_samples[
                1
            ] = 1  # This is to make sure we only have one epoch on every validation iteration

        self._train_ds, self._validation_ds, self._test_ds = build_train_valid_test_datasets(
            cfg=self.cfg,
            trainer=self.trainer,
            data_prefix=self.cfg.data.data_prefix,
            data_impl=self.cfg.data.data_impl,
            splits_string=self.cfg.data.splits_string,
            train_valid_test_num_samples=train_valid_test_num_samples,
            seq_length=self.cfg.data.seq_length,
            seed=self.cfg.seed,
            skip_warmup=self.cfg.data.get('skip_warmup', True),
            tokenizer=self.tokenizer,
        )
        if self._train_ds is not None:
            logging.info(f'Length of train dataset: {len(self._train_ds)}')
        if self._validation_ds is not None:
            logging.info(f'Length of val dataset: {len(self._validation_ds)}')
        if self._test_ds is not None:
            logging.info(f'Length of test dataset: {len(self._test_ds)}')
        logging.info(f'Finished building GPT datasets.')

        return self._train_ds, self._validation_ds, self._test_ds

    def build_pretraining_data_loader(
        self, dataset, consumed_samples, dataset_type=None, drop_last=True, pad_samples_to_global_batch_size=False
    ):
        """Buld dataloader given an input dataset."""

        logging.info(f'Building dataloader with consumed samples: {consumed_samples}')
        # Megatron sampler
        if hasattr(self.cfg.data, 'dataloader_type') and self.cfg.data.dataloader_type is not None:
            if self.cfg.data.dataloader_type == 'single':
                batch_sampler = MegatronPretrainingSampler(
                    total_samples=len(dataset),
                    consumed_samples=consumed_samples,
                    micro_batch_size=self.cfg.micro_batch_size,
                    data_parallel_rank=parallel_state.get_data_parallel_rank(),
                    data_parallel_size=parallel_state.get_data_parallel_world_size(),
                    drop_last=drop_last,
                    global_batch_size=self.cfg.global_batch_size,
                    pad_samples_to_global_batch_size=pad_samples_to_global_batch_size,
                )
            elif self.cfg.data.dataloader_type == 'cyclic':
                batch_sampler = MegatronPretrainingRandomSampler(
                    total_samples=len(dataset),
                    consumed_samples=consumed_samples,
                    micro_batch_size=self.cfg.micro_batch_size,
                    data_parallel_rank=parallel_state.get_data_parallel_rank(),
                    data_parallel_size=parallel_state.get_data_parallel_world_size(),
                    drop_last=self.cfg.get('drop_last', True),
                )
            else:
                raise ValueError('cfg.data.dataloader_type must be "single" or "cyclic"')
        else:
            raise ValueError('cfg.data.dataloader_type not found. Must be "single" or "cyclic"')

        return torch.utils.data.DataLoader(
            dataset,
            batch_sampler=batch_sampler,
            num_workers=self.cfg.data.num_workers,
            pin_memory=True,
            persistent_workers=True if self.cfg.data.num_workers > 0 else False,
        )

    def setup(self, stage=None):
        """ PTL hook that is executed after DDP spawns.
            We setup datasets here as megatron datasets require DDP to instantiate.
            See https://pytorch-lightning.readthedocs.io/en/latest/common/lightning_module.html#setup for more information.
        Args:
            stage (str, optional): Can be 'fit', 'validate', 'test' or 'predict'. Defaults to None.
        """
        num_parameters_on_device, total_num_parameters = self._get_total_params_across_model_parallel_groups_gpt_bert(
            self.model
        )

        logging.info(
            f'Pipeline model parallel rank: {parallel_state.get_pipeline_model_parallel_rank()}, '
            f'Tensor model parallel rank: {parallel_state.get_tensor_model_parallel_rank()}, '
            f'Number of model parameters on device: {num_parameters_on_device:.2e}. '
            f'Total number of model parameters: {total_num_parameters:.2e}.'
        )

        resume_checkpoint_path = self.trainer._checkpoint_connector.resume_from_checkpoint_fit_path
        if resume_checkpoint_path:
            init_consumed_samples = self._extract_consumed_samples_from_ckpt(resume_checkpoint_path)
        else:
            init_consumed_samples = 0
        self.init_consumed_samples = init_consumed_samples
        self.init_global_step = self.trainer.global_step

        rampup_batch_size = self.cfg.get('rampup_batch_size', None)
        if rampup_batch_size:
            start_batch_size = rampup_batch_size[0]
            batch_size_increment = rampup_batch_size[1]
            total_gpus_number = self.trainer.num_devices * self.trainer.num_nodes

            assert start_batch_size % (total_gpus_number) == 0, (
                'expected'
                ' start batch size ({}) to be divisible by total number of GPUs'
                ' ({})'.format(start_batch_size, total_gpus_number)
            )

            micro_batch_size = self.cfg.get('micro_batch_size', 1)
            tensor_model_parallel_size = self.cfg.get('tensor_model_parallel_size', 1)
            pipeline_model_parallel_size = self.cfg.get('pipeline_model_parallel_size', 1)
            total_data_parallel_size = total_gpus_number // (tensor_model_parallel_size * pipeline_model_parallel_size)

            assert batch_size_increment % (micro_batch_size * total_data_parallel_size) == 0, (
                'expected'
                ' batch size increment ({}) to be divisible by micro_batch_size ({}) times total data parallel size'
                ' ({})'.format(batch_size_increment, micro_batch_size, total_data_parallel_size)
            )

        if stage == 'predict':
            return
        else:
            # TODO: consider adding a ModelPT guard to check if model is being restored.
            # allowing restored models to optionally setup datasets
            self.build_train_valid_test_datasets()
            self.setup_training_data(self.cfg.data)
            self.setup_validation_data(self.cfg.data)
            self.setup_test_data(self.cfg.data)

        # when using pipeline model parallel the final stage need to initialize word embeddings
        if parallel_state.get_pipeline_model_parallel_world_size() > 1:
            if isinstance(self.model, list):
                for i, module in enumerate(self.model):
                    parallel_state.set_virtual_pipeline_model_parallel_rank(i)
                    if self.cfg.get('share_embeddings_and_output_weights', True):
                        module.sync_initial_word_embeddings()
                parallel_state.set_virtual_pipeline_model_parallel_rank(0)
            else:
                if self.cfg.get('share_embeddings_and_output_weights', True):
                    self.model.sync_initial_word_embeddings()

        if self.cfg.get('transformer_engine', False):
            self.setup_transformer_engine_tp_groups()

    def setup_training_data(self, cfg):
        if hasattr(self, '_train_ds'):
            consumed_samples = self.compute_consumed_samples(0)
            logging.info(
                f'Setting up train dataloader with len(len(self._train_ds)): {len(self._train_ds)} and consumed samples: {consumed_samples}'
            )
            self._train_dl = self.build_pretraining_data_loader(self._train_ds, consumed_samples)

    def setup_validation_data(self, cfg):
        if hasattr(self, '_validation_ds'):
            consumed_samples = 0
            logging.info(
                f'Setting up validation dataloader with len(len(self._validation_ds)): {len(self._validation_ds)} and consumed samples: {consumed_samples}'
            )

            drop_last = True
            if not self.cfg.data.get('validation_drop_last', True):
                logging.info(f'Drop last in validation dataset is set to False')
                drop_last = False
            pad_samples_to_global_batch_size = False
            if self.cfg.data.get('pad_samples_to_global_batch_size', False):
                logging.info('pad_samples_to_global_batch_size set to True')
                pad_samples_to_global_batch_size = True

            self._validation_dl = self.build_pretraining_data_loader(
                self._validation_ds, consumed_samples, "validation", drop_last, pad_samples_to_global_batch_size
            )

    def setup_test_data(self, cfg):
        if hasattr(self, '_test_ds'):
            consumed_samples = 0
            logging.info(
                f'Setting up test dataloader with len(len(self._test_ds)): {len(self._test_ds)} and consumed samples: {consumed_samples}'
            )
            self._test_dl = self.build_pretraining_data_loader(self._test_ds, consumed_samples)

    def generate(
        self,
        inputs: Union[List[str], torch.Tensor, List[dict]],
        length_params: LengthParam,
        sampling_params: SamplingParam = None,
    ) -> OutputType:

        # check whether the DDP is initialized
        if parallel_state.is_unitialized():

            def dummy():
                return

            if self.trainer.strategy.launcher is not None:
                self.trainer.strategy.launcher.launch(dummy, trainer=self.trainer)
            self.trainer.strategy.setup_environment()

            if self.cfg.get('transformer_engine', False):
                self.setup_transformer_engine_tp_groups()

        # set the default sampling params if it is None.
        # default do greedy sampling
        if sampling_params is None:
            sampling_params = get_default_sampling_params()

        # set the default length params if it is None.
        # default do greedy sampling
        if length_params is None:
            length_params = get_default_length_params()

        return megatron_gpt_generate(self.cuda(), inputs, self.tokenizer, length_params, sampling_params)

    def predict_step(self, batch: Any, batch_idx: int, dataloader_idx: Optional[int] = None) -> Any:
        inference_config = self.get_inference_config()
        if inference_config is None:
            return None
        else:
            # need to overwrite some configuration, make it immutable
            inference_config = inference_config.copy()
            compute_logprob = inference_config['compute_logprob']
            if compute_logprob:
                del inference_config['compute_logprob']
                inference_config['inputs'] = batch
                inference_config['tokens_to_generate'] = 1
                inference_config['all_probs'] = True
                inference_config["add_BOS"] = False
                inference_config['greedy'] = True
                response = generate(self, **inference_config)
                compute_prob_response = get_computeprob_response(self.tokenizer, response, batch)
                return compute_prob_response
            else:
                del inference_config['compute_logprob']
                inference_config['inputs'] = batch
                return generate(self, **inference_config)

    def list_available_models(self):
        return None

    def transfer_batch_to_device(self, batch: Any, device: torch.device, dataloader_idx: int) -> Any:
        """ PTL hook: https://pytorch-lightning.readthedocs.io/en/latest/common/lightning_module.html#transfer-batch-to-device
            When using pipeline parallelism, we need the global batch to remain on the CPU,
            since the memory overhead will be too high when using a large number of microbatches.
            Microbatches are transferred from CPU to GPU inside the pipeline.
        """
        return batch

    def _validate_trainer(self):
        """ Certain trainer configurations can break training.
            Here we try to catch them and raise an error.
        """
        if self.trainer.accumulate_grad_batches > 1:
            raise ValueError(
                f'Gradient accumulation is done within training_step. trainer.accumulate_grad_batches must equal 1'
            )

    @classmethod
    def list_available_models(cls) -> Optional[PretrainedModelInfo]:
        """
        This method returns a list of pre-trained model which can be instantiated directly from NVIDIA's NGC cloud.
        Returns:
            List of available pre-trained models.
        """
        result = []
        result.append(
            PretrainedModelInfo(
                pretrained_model_name="megatron_gpt_345m",
                location="https://api.ngc.nvidia.com/v2/models/nvidia/nemo/megatron_gpt_345m/versions/1/files/megatron_gpt_345m.nemo",
                description="345M parameter GPT generative Megatron model.",
            )
        )
        return result

    def _set_tp_groups(self, module):
        """ Helper method to set tp groups for transformer engine"""

        if self.cfg.get('transformer_engine', False):
            logging.info(f'Setting up transformer engine modules for tensor parallelism.')
            if self.cfg.get('megatron_amp_O2', 'False'):
                # when using O2 additional module key is added that casts the weights
                for layer in module.module.language_model.encoder.layers:
                    layer.set_tensor_parallel_group(parallel_state.get_tensor_model_parallel_group())

            else:
                for layer in module.language_model.encoder.layers:
                    layer.set_tensor_parallel_group(parallel_state.get_tensor_model_parallel_group())

    def setup_transformer_engine_tp_groups(self):
        """ This should be called after model parallel groups have been initialized
            and only needs to be called when using Transformer Engine.
        """
        if isinstance(self.model, list):
            for module in self.model:
                self._set_tp_groups(module)
        else:
            self._set_tp_groups(self.model)

    def on_save_checkpoint(self, checkpoint) -> None:
        """LightningModule hook:
        https://pytorch-lightning.readthedocs.io/en/stable/common/lightning_module.html#on-save-checkpoint
        """
        if isinstance(self.model, list):
            for i in range(len(self.model)):
                parallel_state.set_virtual_pipeline_model_parallel_rank(i)
                checkpoint[f'model{i}'] = self.model[i].module.state_dict_for_save_checkpoint()
            parallel_state.set_virtual_pipeline_model_parallel_rank(0)

    def on_load_checkpoint(self, checkpoint) -> None:
        """LightningModule hook:
        https://pytorch-lightning.readthedocs.io/en/stable/common/lightning_module.html#on-load-checkpoint
        """
        if isinstance(self.model, list):
            for i in range(len(self.model)):
                parallel_state.set_virtual_pipeline_model_parallel_rank(i)
                self.model[i].module.load_state_dict(checkpoint[f'model{i}'], strict=True)
            parallel_state.set_virtual_pipeline_model_parallel_rank(0)

    def parameters(self):
        if isinstance(self.model, list):
            return itertools.chain.from_iterable(module.parameters() for module in self.model)
        else:
            return self.model.parameters()

    def _reset_activation_checkpointing_args(self):
        """ Disables activation checkpointing completely and saves the values so that 
            _restore_activation_checkpointing_args can restore them later. This function must always be 
            called before _restore_activation_checkpointing_args.
        """
        # Store values to restore them later.
        self.last_activations_checkpoint_granularity = self.cfg.activations_checkpoint_granularity
        self.last_activations_checkpoint_method = self.cfg.activations_checkpoint_method
        self.last_activations_checkpoint_num_layers = self.cfg.activations_checkpoint_num_layers
        self.last_activations_checkpoint_layers_per_pipeline = self.cfg.activations_checkpoint_layers_per_pipeline

        # Reset config values. Needed for calling generate.
        self.cfg.activations_checkpoint_granularity = None
        self.cfg.activations_checkpoint_method = None
        self.cfg.activations_checkpoint_num_layers = None
        self.cfg.activations_checkpoint_layers_per_pipeline = None

        # Reset model parameters.
        for module in self.get_gpt_module_list():
            module.language_model.encoder.activations_checkpoint_granularity = None
            module.language_model.encoder.activations_checkpoint_method = None
            module.language_model.encoder.activations_checkpoint_num_layers = None
            module.language_model.encoder.activations_checkpoint_layers_per_pipeline = None

    def _restore_activation_checkpointing_args(self):
        """ Restores the activation checkpointing parameters using the values saved by  
            _reset_activation_checkpointing_args. This function must never be called before 
            _reset_activation_checkpointing_args.
        """
        # Restore config values.
        self.cfg.activations_checkpoint_granularity = self.last_checkpointing_granularity
        self.cfg.activations_checkpoint_method = self.last_checkpointing_method
        self.cfg.activations_checkpoint_num_layers = self.last_checkpointing_num_layers
        self.cfg.activations_checkpoint_layers_per_pipeline = self.last_activations_checkpoint_layers_per_pipeline

        # Restore model parameters.
        for module in self.get_gpt_module_list():
            module.language_model.encoder.activations_checkpoint_granularity = self.last_checkpointing_granularity
            module.language_model.encoder.activations_checkpoint_method = self.last_checkpointing_method
            module.language_model.encoder.activations_checkpoint_num_layers = self.last_checkpointing_num_layers
            module.language_model.encoder.activations_checkpoint_layers_per_pipeline = (
                self.last_activations_checkpoint_layers_per_pipeline
            )

    def _reset_sequence_parallelism_args(self):
        """ Disables sequence parallelism completely and saves the values so that 
            _restore_sequence_parallelism_args can restore them later. This function must always be 
            called before _restore_sequence_parallelism_args.
        """
        # Store values to restore them later.
        self.last_sequence_parallel = self.cfg.sequence_parallel

        # Reset config values. Needed for calling generate.
        self.cfg.sequence_parallel = None

        # Reset model parameters.

        for module in self.get_gpt_module_list():
            module.language_model.encoder.sequence_parallel = None

    def _restore_sequence_parallelism_args(self):
        """ Restores the sequence parallelism parameters using the values saved by  
            _reset_sequence_parallelism_args. This function must never be called before 
            _reset_sequence_parallelism_args.
        """
        # Restore config values.
        self.cfg.sequence_parallel = self.last_sequence_parallel

        # Restore model parameters.
        for module in self.get_gpt_module_list():
            module.language_model.encoder.sequence_parallel = self.last_sequence_parallel
