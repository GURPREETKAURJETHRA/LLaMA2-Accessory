import random
import warnings
from time import sleep

import torch
import yaml
from torch.utils.data import Dataset
from PIL import Image
from ..data_reader import read_img_general
import json
import h5py
from accessory.model.tokenizer import Tokenizer
import os
from pathlib import Path

from . import lib as conversation_lib

import traceback

IGNORE_INDEX = -100

DEFAULT_IMAGE_TOKEN = "<image>"


class LabelAllZeroError(Exception):
    def __init__(self, message=None):
        self.message = message

    def __str__(self):
        return f'LabelAllZeroError: {self.message}'


class ConversationGenerator:
    def __init__(self, tokenizer):
        self.tokenizer = tokenizer
        self.header = f"{conversation_lib.default_conversation.system}\n\n"
        self._probe_tokenizer_style()

    def _probe_tokenizer_style(self):
        """
        Given a sentence, e.g. "My darling", some tokenizers will make the space a seperate token,
        while some others will merge the space into the next word, forming a token representing " darling".
        Knowing which style the tokenizer takes is necessary for correct ground-truth label masking.

        """
        probe = "Probe am I"
        sentence1 = self.tokenizer.encode(conversation_lib.default_conversation.roles[1] + ": " + probe,
                                          bos=False, eos=False)
        sentence2 = self.tokenizer.encode(probe,
                                          bos=False, eos=False)
        if sentence1[-len(sentence2):] == sentence2:
            self.space_before_to_predict = False
        else:
            sentence3 = self.tokenizer.encode(" " + probe,
                                              bos=False, eos=False)
            assert sentence1[-len(sentence3):] == sentence3
            self.space_before_to_predict = True

    def add_speaker_and_signal(self, source, get_conversation=True):
        """Add speaker and start/end signal on each round."""
        BEGIN_SIGNAL = "### "
        END_SIGNAL = "\n"
        conversation = self.header

        to_predict_list = []

        for sentence in source:
            from_str = sentence["from"]
            if from_str.lower() in ["human"]:
                from_str = conversation_lib.default_conversation.roles[0]
            elif from_str.lower() in ["gpt", "assistant"]:
                from_str = conversation_lib.default_conversation.roles[1]
            else:
                raise ValueError(f"unknown dialog role: {from_str.lower()}")

            value = sentence["value"]
            if DEFAULT_IMAGE_TOKEN in value:
                value = value.replace(DEFAULT_IMAGE_TOKEN, '').strip()

            sentence_value = BEGIN_SIGNAL + from_str + ": " + value + END_SIGNAL

            if from_str == conversation_lib.default_conversation.roles[1]:
                to_predict_value = value + END_SIGNAL + "###"
                if self.space_before_to_predict:
                    to_predict_value = " " + to_predict_value
                to_predict_list.append(to_predict_value)

            if get_conversation:
                conversation = conversation + sentence_value

        conversation = conversation + BEGIN_SIGNAL
        return conversation, to_predict_list


class FinetuneDialogDataset(Dataset):
    def __init__(self, config_path, transform, max_words=30, image_words=257, tokenizer_path=None,
                 cache_on_disk=False, rank=0):

        print(f"read dataset config from {config_path}")
        with open(config_path, 'r') as f:
            self.config = yaml.load(f, Loader=yaml.FullLoader)
        print("DATASET CONFIG:")
        print(self.config)

        self.cache_on_disk = cache_on_disk
        if cache_on_disk:
            # save data items on disk to avoid duplicating annotations in each rank,
            # which could cause a hugh waste of CPU memory
            config_identifier = config_path
            disallowed_chars = ['/', '\\', '.', '?', '!']
            for _ in disallowed_chars:
                config_identifier = config_identifier.replace(_, '-')
            self.cache_dir = f"./accessory_data_cache/{config_identifier}"
            if rank == 0:
                Path(self.cache_dir).mkdir(parents=True, exist_ok=True)
        else:
            self.cache_dir = None
            

        if not cache_on_disk or rank == 0:

            group_ann = {}
            for meta in self.config['META']:
                meta_path, meta_type = meta['path'], meta['type']
                meta_ext = os.path.splitext(meta_path)[-1]
                if meta_ext == ".json":
                    with open(meta_path) as f:
                        meta_l = json.load(f)
                elif meta_ext == ".jsonl":
                    meta_l = []
                    with open(meta_path) as f:
                        for i, line in enumerate(f):
                            try:
                                meta_l.append(json.loads(line))
                            except json.decoder.JSONDecodeError as e:
                                print(f"Error decoding the following jsonl line ({i}):\n{line.rstrip()}", force=True)
                                raise e
                else:
                    raise NotImplementedError(
                        f"Unknown meta file extension: \"{meta_ext}\". "
                        f"Currently, .json, .jsonl are supported. "
                        "If you are using a supported format, please set the file extension so that the proper parsing "
                        "routine can be called."
                    )
                if meta_type not in group_ann:
                    group_ann[meta_type] = []
                print(f"{meta_path}, type{meta_type}: len {len(meta_l)}")
                group_ann[meta_type] += meta_l

            # sort group_ann for higher efficiency (items in one global batch with similar length)
            for meta_type, meta_l in group_ann.items():
                meta_l.sort(key=lambda data_item: sum([len(_['value']) for _ in data_item['conversations']]))

            ann = sum(list(group_ann.values()), start=[])
            group_indice_range = {}
            start_pos = 0
            for meta_type, meta_l in group_ann.items():
                group_indice_range[meta_type] = [start_pos, start_pos + len(meta_l)]
                start_pos = start_pos + len(meta_l)

            if not cache_on_disk:
                self.ann = ann
                self.group_indices = {key: list(range(val[0], val[1])) for key, val in group_indice_range.items()}
            else:
                # when cache on disk, rank0 saves items to an h5 file
                if (Path(self.cache_dir)/'data.h5').exists() and (Path(self.cache_dir)/'ready').exists():
                    print(f"use existing h5 data cache: {Path(self.cache_dir)}")
                else:
                    serialized_ann = [json.dumps(_) for _ in ann]
                    print(f"start to build data cache to: {Path(self.cache_dir)}")
                    with h5py.File(Path(self.cache_dir)/'data.h5', 'w') as file:
                        dt = h5py.vlen_dtype(str)
                        h5_ann = file.create_dataset("ann", (len(serialized_ann),), dtype=dt)
                        h5_ann[:] = serialized_ann
                        file.create_dataset("group_indice_range", data=json.dumps(group_indice_range))
                    with open(Path(self.cache_dir)/'ready', 'w') as f:
                        f.write("ready")
                    print(f"data cache built")

        if self.cache_on_disk:
            while not (Path(self.cache_dir)/'ready').exists():
                # cache has not yet been completed by rank 0
                assert rank != 0
                sleep(1)
            cache_file = h5py.File(Path(self.cache_dir) / 'data.h5', 'r')
            self.ann = cache_file['ann']
            group_indice_range = json.loads(cache_file['group_indice_range'].asstr()[()])
            self.group_indices = {key: list(range(val[0], val[1])) for key, val in group_indice_range.items()}


        print(f"total length: {len(self)}")
        self.transform = transform
        print(f"transform:\n{self.transform}")
        self.max_words = max_words
        self.image_words = image_words
        self.tokenizer = Tokenizer(model_path=tokenizer_path)
        self.conversation_generator = ConversationGenerator(self.tokenizer)

    def __len__(self):
        return len(self.ann)

    def get_item_func(self, index):
        data_item = self.ann[index]
        if self.cache_on_disk:
            data_item = json.loads(data_item)

        if 'image' in data_item.keys():
            filename = data_item['image']
            image = read_img_general(filename)
            image = self.transform(image)
        else:
            image = None
            # warnings.warn("pure black image for examples without image")
            # image = torch.zeros(3, 224, 224)

        source = data_item["conversations"]
        conversation, to_predict_values = self.conversation_generator.add_speaker_and_signal(source)
        if len(to_predict_values) == 0:
            warnings.warn(f"see dialog data with nothing to predict, data: {data_item}")
            return self[index-1]

        tokenzed_conversation = self.tokenizer.encode(conversation, bos=True, eos=True)
        labels = [IGNORE_INDEX for _ in tokenzed_conversation]

        check_pos = 0
        for value in to_predict_values:
            tokenized_value = self.tokenizer.encode(value, bos=False, eos=False)
            value_pos = find_sublist(tokenzed_conversation[check_pos:], tokenized_value) + check_pos
            if value_pos == -1:
                print("a sentence mismatches the corresponding piece in the conversation")
                return self[index-1]
            labels[value_pos:value_pos+len(tokenized_value)] = tokenized_value
            assert labels[value_pos:value_pos+len(tokenized_value)] == tokenzed_conversation[value_pos:value_pos+len(tokenized_value)]
            check_pos = value_pos+len(tokenized_value)

        input2 = torch.tensor(tokenzed_conversation, dtype=torch.int64)
        labels = torch.tensor(labels, dtype=torch.int64)

        if image is not None:
            max_words = self.max_words - self.image_words
        else:
            max_words = self.max_words
        padding = max_words - input2.shape[0]
        if padding > 0:
            input2 = torch.cat((input2, torch.zeros(padding, dtype=torch.int64) - 1))
            labels = torch.cat((labels, torch.zeros(padding, dtype=torch.int64) - 1))
        elif padding < 0:
            input2 = input2[:max_words]
            labels = labels[:max_words]

        input2_mask = input2.ge(0)
        label_mask = labels.ge(0)
        input2[~input2_mask] = 0
        labels[~label_mask] = 0
        input2_mask = input2_mask.float()

        if torch.count_nonzero(labels) == 0:
            raise LabelAllZeroError()

        if image is None:
            return input2, labels, input2_mask
        else:
            return input2, labels, input2_mask, image

    def __getitem__(self, index):
        try:
            return self.get_item_func(index)
        except Exception as e:
            if not isinstance(e, LabelAllZeroError):
                print(f"Item {index} errored, annotation:\n"
                      f"{self.ann[index]}\n"
                      f"Error:\n"
                      f"{traceback.format_exc()}", force=True)
            for group_name, indices_this_group in self.group_indices.items():
                if indices_this_group[0] <= index <= indices_this_group[-1]:
                    if index == indices_this_group[0]:
                        new_index = indices_this_group[-1]
                    else:
                        new_index = index - 1
                    return self[new_index]

    def groups(self):
        return list(self.group_indices.values())

def find_sublist(a: list, b:list):
    len_a, len_b = len(a), len(b)
    for i in range(len_a - len_b + 1):
        if a[i:i+len_b] == b:
            return i
    return -1