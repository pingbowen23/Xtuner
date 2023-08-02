from collections import OrderedDict

import torch
from mmengine.runner import load_checkpoint
from peft import PeftType, get_peft_model, prepare_model_for_kbit_training

from mmchat.registry import MODELS
from .sft import SupervisedFinetune


def find_all_linear_names(model):
    lora_module_names = set()
    for name, module in model.named_modules():
        if isinstance(module, torch.nn.Linear):
            names = name.split('.')
            lora_module_names.add(names[0] if len(names) == 1 else names[-1])

    if 'lm_head' in lora_module_names:  # needed for 16-bit
        lora_module_names.remove('lm_head')
    return list(lora_module_names)


class SupervisedLoraFinetune(SupervisedFinetune):

    def __init__(self,
                 llm,
                 lora,
                 data_preprocessor=None,
                 tokenizer=None,
                 peft_model=None):
        super().__init__(llm, data_preprocessor, tokenizer)

        self.llm = prepare_model_for_kbit_training(self.llm)

        lora = MODELS.build(lora)
        if lora.target_modules is None:
            modules = find_all_linear_names(self.llm)
            lora.target_modules = modules

        self.llm = get_peft_model(self.llm, lora)
        if peft_model is not None:
            _ = load_checkpoint(self, peft_model)

        self._is_init = True

    def init_weights(self):
        pass

    def state_dict(self, destination=None, prefix='', keep_vars=False):

        def get_peft_model_state_dict(model,
                                      state_dict=None,
                                      adapter_name='default'):
            # Modified from `https://github.com/huggingface/peft/blob/main/src
            # /peft/utils/save_and_load.py`

            config = model.peft_config[adapter_name]
            if state_dict is None:
                state_dict = model.state_dict()
            if config.peft_type == PeftType.LORA:
                # adapted from `https://github.com/microsoft/LoRA/blob/main/
                # loralib/utils.py`
                # to be used directly with the state dict which is necessary
                # when using DeepSpeed or FSDP
                bias = config.bias
                if bias == 'none':
                    to_return = {
                        k: state_dict[k]
                        for k in state_dict if 'lora_' in k
                    }
                elif bias == 'all':
                    to_return = {
                        k: state_dict[k]
                        for k in state_dict if 'lora_' in k or 'bias' in k
                    }
                elif bias == 'lora_only':
                    to_return = {}
                    for k in state_dict:
                        if 'lora_' in k:
                            to_return[k] = state_dict[k]
                            bias_name = k.split('lora_')[0] + 'bias'
                            if bias_name in state_dict:
                                to_return[bias_name] = state_dict[bias_name]
                else:
                    raise NotImplementedError
                to_return = {
                    k: v
                    for k, v in to_return.items()
                    if (('lora_' in k and adapter_name in k) or ('bias' in k))
                }
            else:
                # Currently we only support lora
                raise NotImplementedError
            if model.modules_to_save is not None:
                for key, value in state_dict.items():
                    if any(f'{module_name}.modules_to_save.{adapter_name}' in
                           key for module_name in model.modules_to_save):
                        to_return[key] = value

            return to_return

        state_dict = super().state_dict()
        to_return = get_peft_model_state_dict(self.llm, state_dict=state_dict)
        return OrderedDict(to_return)