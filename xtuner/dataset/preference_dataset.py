import copy
import json
import os
from datetime import timedelta
from functools import partial
from multiprocessing import Process, Queue
from typing import Callable, Dict, List

import numpy as np
import torch.distributed as dist
import tqdm
from datasets import Dataset as HFDataset
from datasets import concatenate_datasets
from mmengine.config import Config, ConfigDict
from mmengine.logging import print_log
from mmengine.utils.misc import get_object_from_string
from torch.utils.data import Dataset
from transformers import AutoTokenizer
from xtuner.registry import BUILDER, MAP_FUNC
from .huggingface import build_origin_dataset


def _worker(
    tokenize_fun: Callable,
    data_queue: Queue,
    out_queue: Queue,
):
    while True:
        data_chunk = data_queue.get()

        if data_chunk is None:
            out_queue.put(None)
            break
        chunk_results = []
        for idx, data in data_chunk:
            chunk_results.append([idx, tokenize_fun(data)])
        out_queue.put(chunk_results)


def _chunk_data_to_queue(data_queue: Queue, data: List[Dict], chunk_size: int,
                         nproc):
    data_iter = iter(data)
    chunk_data = []
    while True:
        try:
            item = next(data_iter)
        except StopIteration:
            break
        chunk_data.append(item)
        if len(chunk_data) == chunk_size:
            data_queue.put(chunk_data)
            chunk_data = []
    if chunk_data:
        data_queue.put(chunk_data)

    for _ in range(nproc):
        data_queue.put(None)


def _multi_progress(tokenize_fun_p, dataset, nproc, task_num, chunksize,
                    description):
    processes = []
    data_queue = Queue()
    output_queue = Queue()
    bar = tqdm.tqdm(total=task_num, desc=description)
    # task_id = bar.add_task(total=task_num, description=description)
    dataset = enumerate(dataset)
    _chunk_data_to_queue(data_queue, dataset, chunksize, nproc)
    for _ in range(nproc):
        process = Process(
            target=_worker, args=(tokenize_fun_p, data_queue, output_queue))
        process.start()
        processes.append(process)

    results = []
    finished_process = 0
    while finished_process < nproc:
        chunk_results = output_queue.get()
        if chunk_results is None:
            finished_process += 1
            continue
        results.extend(chunk_results)
        bar.update(len(chunk_results))
        bar.refresh()
    results = map(lambda x: x[1], sorted(results, key=lambda x: x[0]))
    return results


def load_jsonl_dataset(data_files=None, data_dir=None, suffix=None):
    assert (data_files is not None) != (data_dir is not None)
    if data_dir is not None:
        data_files = os.listdir(data_dir)
        data_files = [os.path.join(data_dir, fn) for fn in data_files]
        if suffix is not None:
            data_files = [fp for fp in data_files if fp.endswith(suffix)]
    elif isinstance(data_files, str):
        data_files = [data_files]

    dataset_list = []
    for fp in data_files:
        with open(fp, encoding='utf-8') as file:
            data = [json.loads(line) for line in file]
        ds = HFDataset.from_list(data)
        dataset_list.append(ds)
    dataset = concatenate_datasets(dataset_list)
    return dataset


def tokenize(pair: str,
             tokenizer: AutoTokenizer,
             max_length: int,
             is_reward: bool = False,
             reward_token_id: int = -1):

    # if tokenizer.chat_template is None:
    #     tokenizer.chat_template = '''
    #     {%- for message in messages %}
    #         {%- if message['role'] == 'user' %}
    #             {{- '[INST]' + message['content'] + '[/INST]' }}
    #         {%- elif message['role'] == 'added_user' %}
    #             {{- message['content'] }}
    #         {%- elif message['role'] == 'system' %}
    #             {{- '<<SYS>>\\n' + message['content'] + '\\n<</SYS>>\\n\\n' }}
    #         {%- elif message['role'] == 'added_assistant' %}
    #             {{- " " + message['content']}}
    #         {%- elif message['role'] == 'assistant' %}
    #             {{- ' '  + message['content'] + ' ' + eos_token }}
    #         {%- endif %}
    #     {%- endfor %}
    #     '''
            
    # prompt = tokenizer.apply_chat_template(
    #         pair['prompt'], tokenize=False, add_generation_prompt=True)    
    # chosen = tokenizer.apply_chat_template(
    #     pair['prompt'] + pair['chosen'],
    #     tokenize=False,
    #     add_generation_prompt=False)
    # rejected = tokenizer.apply_chat_template(
    #     pair['prompt'] + pair['rejected'],
    #     tokenize=False,
    #     add_generation_prompt=False)
    
    def process_message(messages):
        prompt = ''
        for message in messages:
            if message['role'] == 'user':
                prompt += '[INST]' + message['content'] + '[/INST]'
            elif message['role'] == 'added_user':
                prompt += message['content']
            elif message['role'] =='system':
                prompt += '<<SYS>>\\n' + message['content'] + '\\n<</SYS>>\\n\\n'
            elif message['role'] == 'added_assistant':
                prompt += message['content']
            elif message['role'] == 'assistant':
                prompt += message['content'] + tokenizer.eos_token
        return prompt 
    
    prompt = pair['prompt'][0]['content'] if pair['prompt'][0]['role'] != "user" else process_message(pair['prompt'])
    chosen = process_message(pair['prompt'] + pair['chosen'])
    rejected = process_message(pair['prompt'] + pair['rejected'])
    
    prompt_ids = tokenizer.encode(prompt, add_special_tokens=False)
    chosen_ids = tokenizer.encode(chosen, add_special_tokens=False)
    rejected_ids = tokenizer.encode(rejected, add_special_tokens=False)
    
    if len(chosen_ids) > max_length:
        chosen_ids = chosen_ids[:max_length]
    if len(rejected_ids) > max_length:
        rejected_ids = rejected_ids[:max_length]

    if is_reward:
        # reward label
        chosen_ids = chosen_ids + [reward_token_id]
        rejected_ids = rejected_ids + [reward_token_id]
        chosen_labels = [-100] * len(chosen_ids[:-1]) + [0]
        rejected_labels = [-100] * len(rejected_ids[:-1]) + [1]
    else:
        # dpo label
        prompt_len = min(len(prompt_ids), max_length)
        chosen_labels = [-100] * prompt_len + copy.deepcopy(
            chosen_ids[prompt_len:])
        rejected_labels = [-100] * prompt_len + copy.deepcopy(
            rejected_ids[prompt_len:])

    return {
        'chosen_ids': chosen_ids,
        'rejected_ids': rejected_ids,
        'chosen_labels': chosen_labels,
        'rejected_labels': rejected_labels,
        'group_id': pair.get('group_id', None),
        "seq_num": pair.get("depth", None)
    }


class PreferenceDataset(Dataset):

    def __init__(
        self,
        dataset: HFDataset,
        tokenizer: AutoTokenizer,
        max_length: int,
        is_dpo: bool = True,
        is_reward: bool = False,
        reward_token_id: int = -1,
        num_proc: int = 32,
    ) -> None:
        self.max_length = max_length
        assert is_dpo != is_reward, \
            'Only one of is_dpo and is_reward can be True'
        if is_reward:
            assert reward_token_id != -1, \
                'reward_token_id should be set if is_reward is True'

        self.is_dpo = is_dpo
        self.is_reward = is_reward
        self.reward_token_id = reward_token_id
        self.tokenized_pairs = []
        
        for tokenized_pair in _multi_progress(
                partial(
                    tokenize,
                    tokenizer=tokenizer,
                    max_length=max_length,
                    is_reward=is_reward,
                    reward_token_id=reward_token_id),
                dataset,
                nproc=num_proc,
                task_num=len(dataset),
                chunksize=num_proc,
                description='Tokenizing dataset'):
            self.tokenized_pairs.append(tokenized_pair)
        
    def __len__(self):
        return len(self.tokenized_pairs)

    def __getitem__(self, idx):
        return self.tokenized_pairs[idx]


class PackedDatasetWrapper(Dataset):

    def __init__(self,
                 dataset,
                 max_packed_length=16384,
                 shuffle_before_pack=True) -> None:
        super().__init__()
        self.max_packed_length = max_packed_length
        self.lengths = []
        self.data = []
        
        # dataset = self.post_process(dataset)
        dataset_group_none , dataset_group_sorted = self.split_data(dataset)
        packed_dataset = self.pack_dataset(dataset_group_sorted, dataset_group_none, max_packed_length=self.max_packed_length)

    def split_data(self, dataset):
        # 转换为 Pandas DataFrame
        import pandas as pd
        df = pd.DataFrame(dataset[:])

        # 拆分为两部分
        df_group_none = df[df['group_id'].isna()]  # group_id 为 None 的部分
        df_group_sorted = df[df['group_id'].notna()]  # group_id 不为 None 的部分

        # 对 group_id 不为 None 的部分进行排序
        df_group_sorted = df_group_sorted.sort_values(by=['group_id', 'seq_num'], ascending=[True, True])

        # 将两部分转换回列表形式
        dataset_group_none = df_group_none.to_dict(orient='records')
        dataset_group_sorted = df_group_sorted.to_dict(orient='records')
        return dataset_group_none , dataset_group_sorted
        
    def pack_dataset(self,dataset_group_sorted, dataset_group_none, max_packed_length=32768):
        
        def process_group(group_id,last_index):
            data_bin = []
            bin_seq_len = 0
            removed = 0
            
            for i in range(last_index, len(dataset_group_sorted)):
                data = dataset_group_sorted[i]
                if dataset_group_sorted[i]['group_id'] > group_id:
                    break
                
                if data['group_id'] == group_id:
                    cur_len = len(data['chosen_ids']) + len(data['rejected_ids'])
                    if cur_len > max_packed_length:
                        removed += 1
                        continue
                    
                    if (bin_seq_len +
                    cur_len) > max_packed_length and len(data_bin) > 0:
                        self.data.append(data_bin)
                        self.lengths.append(bin_seq_len)
                        data_bin = []
                        bin_seq_len = 0
                    data_bin.append(data)
                    bin_seq_len += cur_len
            last_index = i
            return data_bin , bin_seq_len , last_index
        
        def process_short_data(data_bin, bin_seq_len, cur_index):
            while cur_index < max_index:
                cur_len = len(dataset_group_none[cur_index]['chosen_ids']) + len(dataset_group_none[cur_index]['rejected_ids'])
                if cur_len > max_packed_length:
                    continue
                
                if (bin_seq_len +
                        cur_len) > max_packed_length and len(data_bin) > 0:
                    break
                
                data_bin.append(dataset_group_none[cur_index])
                bin_seq_len += cur_len
                cur_index += 1
            return data_bin , bin_seq_len , cur_index
            
        
        packed = []  # 保存每次打包的结果

        # 将 dataset_group_sorted 按 group_id 分组
        groups = {}
        for item in dataset_group_sorted:
            group_id = item['group_id']
            if group_id not in groups:
                groups[group_id] = []
            groups[group_id].append(item)

        # 转换为按 group_id 的列表，确保顺序一致
        sorted_groups = list(groups.values())

        # 打包过程
        group_ids , last_index = sorted({data['group_id'] for data in dataset_group_sorted}), 0
        cur_index , max_index = 0, len(dataset_group_none)
        
        for group_id in group_ids:
            data_bin , bin_seq_len ,last_index = process_group(group_id,last_index)
            
            if cur_index < max_index:
                data_bin , bin_seq_len , cur_index = process_short_data(data_bin, bin_seq_len, cur_index)
                self.data.append(data_bin)
                self.lengths.append(bin_seq_len)
            else:
                self.data.append(data_bin)
                self.lengths.append(bin_seq_len)
        
        while cur_index < max_index:
            data_bin, bin_seq_len = [], 0
            data_bin , bin_seq_len , cur_index = process_short_data(data_bin, bin_seq_len, cur_index)
            self.data.append(data_bin)
            self.lengths.append(bin_seq_len)
        
            
        if len(data_bin) > 0:
            self.data.append(data_bin)
            self.lengths.append(bin_seq_len)

        print_log(
            f'The batch numbers of dataset is changed '
            f'to {len(self)} after'
            ' using var len attention.',
            logger='current')
        
        return packed
        
    
    def post_process(self, dataset):
        
        def merge_data(indices, dataset):
            keys = ['chosen_ids', 'rejected_ids', 'chosen_labels', 'rejected_labels'] # 'position_ids','cumulative_len','concated'
            merged_data = {key: (True if key == 'concated' else []) for key in keys}
            position_ids , seq_len = [], []
            
            for index in indices:
                data = dataset[index]
                for key in keys:
                    merged_data[key].extend(data[key])
                    
                    if "ids" in key:
                        position_ids.extend(list(range(len(data[key]))))
                        seq_len.append(len(list(range(len(data[key])))))         
            
            merged_data["position_ids"] = position_ids
            merged_data["seq_len"] = seq_len
            merged_data["concated"] = True
            return merged_data
                
        from collections import defaultdict
        grouped_indices = defaultdict(list)

        # 遍历数据集并按 group_id 分组索引
        for index, item in enumerate(dataset):
            if item["group_id"] is not None:
                grouped_indices[item["group_id"]].append((item["seq_num"], index))
        
        for group_id in grouped_indices:
            # 排序仅按 seq_num 排列，并提取 index
            grouped_indices[group_id] = [index for seq_num, index in sorted(grouped_indices[group_id])]
        
        grouped_indices = dict(grouped_indices)
        selected_indices = {index for indices in grouped_indices.values() for index in indices}
        merged_data = [merge_data(v, dataset) for k,v in grouped_indices.items()]
        filtered_dataset = [item for index, item in enumerate(dataset) if index not in selected_indices]
        return merged_data + filtered_dataset  
        
    
    def __len__(self):
        return len(self.data)

    def __getitem__(self, index):
        pairs = self.data[index]
        input_ids, cu_seqlens, position_ids, labels = [], [0], [], []
        for pair in pairs:
            if not pair.get('concated', False):
                input_ids.extend(pair['chosen_ids'])
                input_ids.extend(pair['rejected_ids'])

                position_ids.extend(list(range(len(pair['chosen_ids']))))
                position_ids.extend(list(range(len(pair['rejected_ids']))))

                labels.extend(pair['chosen_labels'])
                labels.extend(pair['rejected_labels'])

                cu_seqlens.append(cu_seqlens[-1] + len(pair['chosen_ids']))
                cu_seqlens.append(cu_seqlens[-1] + len(pair['rejected_ids']))
            else:
                input_ids.extend(pair['chosen_ids'])
                input_ids.extend(pair['rejected_ids'])

                labels.extend(pair['chosen_labels'])
                labels.extend(pair['rejected_labels'])
                
                position_ids.extend(pair.get('position_ids',None))
                seq_lens = pair.get('seq_len',None)
                
                for seq_len in seq_lens:
                    cu_seqlens.append(cu_seqlens[-1] + seq_len)

                
        return {
            'input_ids': input_ids,
            'labels': labels,
            'position_ids': position_ids,
            'cumulative_len': cu_seqlens
        }


def unpack_seq(seq, cu_seqlens):
    """Unpack a packed sequence to a list of sequences with different
    lengths."""
    seqlens = (cu_seqlens[1:] - cu_seqlens[:-1]).tolist()
    subseqs = seq.split(seqlens)
    return subseqs


def broad_cast_dataset(dataset):
    xtuner_dataset_timeout = timedelta(
        minutes=int(os.getenv('XTUNER_DATASET_TIMEOUT', default=60)))
    print_log(
        f'xtuner_dataset_timeout = {xtuner_dataset_timeout}', logger='current')
    using_dist = dist.is_available() and dist.is_initialized()
    if using_dist:
        # monitored barrier requires gloo process group to perform host-side sync.  # noqa
        group_gloo = dist.new_group(
            backend='gloo', timeout=xtuner_dataset_timeout)
    if not using_dist or dist.get_rank() == 0:
        objects = [dataset]
    else:
        objects = [None]
    if using_dist:
        dist.monitored_barrier(
            group=group_gloo, timeout=xtuner_dataset_timeout)
        dist.broadcast_object_list(objects, src=0)
    return objects[0]


def map_dataset(dataset, dataset_map_fn, map_num_proc):
    if isinstance(dataset_map_fn, str):
        map_fn_obj = MAP_FUNC.get(dataset_map_fn) or get_object_from_string(
            dataset_map_fn)
        if map_fn_obj is not None:
            dataset_map_fn = map_fn_obj
        else:
            raise TypeError('dataset_map_fn must be a function or a '
                            "registered function's string in MAP_FUNC, "
                            f"but got a string of '{dataset_map_fn}'")
    dataset = dataset.map(dataset_map_fn, num_proc=map_num_proc)
    return dataset


def build_preference_dataset(
    dataset: str,
    tokenizer: AutoTokenizer,
    max_length: int,
    dataset_map_fn: Callable = None,
    is_dpo: bool = True,
    is_reward: bool = False,
    reward_token_id: int = -1,
    num_proc: int = 32,
    use_varlen_attn: bool = False,
    max_packed_length: int = 16384,
    shuffle_before_pack: bool = True,
) -> Dataset:
    using_dist = dist.is_available() and dist.is_initialized()
    tokenized_ds = None
    if not using_dist or dist.get_rank() == 0:
        if isinstance(tokenizer, dict) or isinstance(
                tokenizer, Config) or isinstance(tokenizer, ConfigDict):
            tokenizer = BUILDER.build(tokenizer)

        dataset = build_origin_dataset(dataset, split='train')
        if dataset_map_fn is not None:
            dataset = map_dataset(
                dataset, dataset_map_fn, map_num_proc=num_proc)

        # if dist.get_rank() == 0:
        #     import pdb; pdb.set_trace()

        tokenized_ds = PreferenceDataset(
            dataset=dataset,
            tokenizer=tokenizer,
            max_length=max_length,
            is_dpo=is_dpo,
            is_reward=is_reward,
            reward_token_id=reward_token_id,
            num_proc=num_proc,
        )
        if use_varlen_attn:
            tokenized_ds = PackedDatasetWrapper(
                dataset=tokenized_ds,
                max_packed_length=max_packed_length,
                shuffle_before_pack=shuffle_before_pack,
            )    
    tokenized_ds = broad_cast_dataset(tokenized_ds)
    return tokenized_ds


def intel_orca_dpo_map_fn(example):
    prompt = [{
        'role': 'system',
        'content': example['system']
    }, {
        'role': 'user',
        'content': example['question']
    }]
    chosen = [{'role': 'assistant', 'content': example['chosen']}]
    rejected = [{'role': 'assistant', 'content': example['rejected']}]
    return {'prompt': prompt, 'chosen': chosen, 'rejected': rejected}

def ultrafeedback_dpo_map_fn(example):
    prompt_role = example.get('prompt_role') if example.get('prompt_role') is not None else "user"
    answer_role = example.get('answer_role') if example.get('answer_role') is not None else "assistant"
    
    prompt = [{
        'role': prompt_role,
        'content': example['instruction']
    }]
    chosen = [{'role': answer_role, 'content': example['chosen']}]
    rejected = [{'role': answer_role, 'content': example['rejected']}]
    return {'prompt': prompt, 'chosen': chosen, 'rejected': rejected}

def orpo_dpo_mix_40k_map_fn(example):
    assert len(example['chosen']) == len(example['rejected'])
    prompt = example['chosen'][:-1]
    chosen = example['chosen'][-1:]
    rejected = example['rejected'][-1:]
    return {'prompt': prompt, 'chosen': chosen, 'rejected': rejected}
