# coding=utf-8
# Copyright 2019 HuggingFace Inc.
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
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import copy
import os
import shutil
import json
import random

import unittest
import logging

import torch

from pytorch_transformers import PretrainedConfig, PreTrainedModel
from pytorch_transformers.modeling_bert import BertModel, BertConfig, BERT_PRETRAINED_MODEL_ARCHIVE_MAP


def _config_zero_init(config):
    configs_no_init = copy.deepcopy(config)
    for key in configs_no_init.__dict__.keys():
        if '_range' in key or '_std' in key:
            setattr(configs_no_init, key, 0.0)
    return configs_no_init

def _create_and_check_torchscript_output_attentions(tester, model_classes, config, inputs_dict):
    config.output_attentions = True
    _create_and_check_torchscript(tester, model_classes, config, inputs_dict)

def _create_and_check_torchscript_output_hidden_state(tester, model_classes, config, inputs_dict):
    config.output_hidden_states = True
    _create_and_check_torchscript(tester, model_classes, config, inputs_dict)

def _create_and_check_torchscript(tester, model_classes, config, inputs_dict):
    configs_no_init = _config_zero_init(config)  # To be sure we have no Nan
    configs_no_init.torchscript = True
    for model_class in model_classes:
        model = model_class(config=configs_no_init)
        model.eval()
        inputs = inputs_dict['input_ids']  # Let's keep only input_ids

        try:
            torch.jit.trace(model, inputs)
        except RuntimeError:
            tester.parent.fail("Couldn't trace module.")

        try:
            traced_gpt2 = torch.jit.trace(model, inputs)
            torch.jit.save(traced_gpt2, "traced_model.pt")
        except RuntimeError:
            tester.parent.fail("Couldn't save module.")

        try:
            loaded_model = torch.jit.load("traced_model.pt")
            os.remove("traced_model.pt")
        except ValueError:
            tester.parent.fail("Couldn't load module.")

        model.eval()
        loaded_model.eval()

        model_params = model.parameters()
        loaded_model_params = loaded_model.parameters()

        models_equal = True
        for p1, p2 in zip(model_params, loaded_model_params):
            if p1.data.ne(p2.data).sum() > 0:
                models_equal = False

        tester.parent.assertTrue(models_equal)

def _create_and_check_initialization(tester, model_classes, config, inputs_dict):
    configs_no_init = _config_zero_init(config)
    for model_class in model_classes:
        model = model_class(config=configs_no_init)
        for name, param in model.named_parameters():
            if param.requires_grad:
                tester.parent.assertIn(param.data.mean().item(), [0.0, 1.0],
                                       msg="Parameter {} of model {} seems not properly initialized".format(name, model_class))

def _create_and_check_for_headmasking(tester, model_classes, config, inputs_dict):
    configs_no_init = _config_zero_init(config)  # To be sure we have no Nan
    for model_class in model_classes:
        config.output_attentions = True
        config.output_hidden_states = True
        model = model_class(config=configs_no_init)
        model.eval()

        # Prepare head_mask
        # Set require_grad after having prepared the tensor to avoid error (leaf variable has been moved into the graph interior) 
        head_mask = torch.ones(tester.num_hidden_layers, tester.num_attention_heads)
        head_mask[0, 0] = 0
        head_mask[-1, :-1] = 0
        head_mask.requires_grad_(requires_grad=True)
        inputs = inputs_dict.copy()
        inputs['head_mask'] = head_mask

        outputs = model(**inputs)

        # Test that we can get a gradient back for importance score computation
        output = sum(t.sum() for t in outputs[0])
        output = output.sum()
        output.backward()
        multihead_outputs = head_mask.grad

        attentions = outputs[-1]
        hidden_states = outputs[-2]

        # Remove Nan

        tester.parent.assertIsNotNone(multihead_outputs)
        tester.parent.assertEqual(len(multihead_outputs), tester.num_hidden_layers)
        tester.parent.assertAlmostEqual(
            attentions[0][..., 0, :, :].flatten().sum().item(), 0.0)
        tester.parent.assertNotEqual(
            attentions[0][..., -1, :, :].flatten().sum().item(), 0.0)
        tester.parent.assertNotEqual(
            attentions[1][..., 0, :, :].flatten().sum().item(), 0.0)
        tester.parent.assertAlmostEqual(
            attentions[-1][..., -2, :, :].flatten().sum().item(), 0.0)
        tester.parent.assertNotEqual(
            attentions[-1][..., -1, :, :].flatten().sum().item(), 0.0)


def _create_and_check_for_head_pruning(tester, model_classes, config, inputs_dict):
    for model_class in model_classes:
        config.output_attentions = True
        config.output_hidden_states = False
        model = model_class(config=config)
        model.eval()
        heads_to_prune = {0: list(range(1, tester.num_attention_heads)),
                          -1: [0]}
        model.prune_heads(heads_to_prune)
        outputs = model(**inputs_dict)

        attentions = outputs[-1]

        tester.parent.assertEqual(
            attentions[0].shape[-3], 1)
        tester.parent.assertEqual(
            attentions[1].shape[-3], tester.num_attention_heads)
        tester.parent.assertEqual(
            attentions[-1].shape[-3], tester.num_attention_heads - 1)


def _create_and_check_for_attentions(tester, model_classes, config, inputs_dict):
    for model_class in model_classes:
        config.output_attentions = True
        config.output_hidden_states = False
        model = model_class(config)
        model.eval()
        outputs = model(**inputs_dict)
        attentions = outputs[-1]
        tester.parent.assertEqual(model.config.output_attentions, True)
        tester.parent.assertEqual(model.config.output_hidden_states, False)
        tester.parent.assertEqual(len(attentions), tester.num_hidden_layers)
        tester.parent.assertListEqual(
            list(attentions[0].shape[-3:]),
            [tester.num_attention_heads,
             tester.seq_length,
             tester.key_len if hasattr(tester, 'key_len') else tester.seq_length])
        out_len = len(outputs)

        # Check attention is always last and order is fine
        config.output_attentions = True
        config.output_hidden_states = True
        model = model_class(config)
        model.eval()
        outputs = model(**inputs_dict)
        tester.parent.assertEqual(out_len+1, len(outputs))
        tester.parent.assertEqual(model.config.output_attentions, True)
        tester.parent.assertEqual(model.config.output_hidden_states, True)

        attentions = outputs[-1]
        tester.parent.assertEqual(len(attentions), tester.num_hidden_layers)
        tester.parent.assertListEqual(
            list(attentions[0].shape[-3:]),
            [tester.num_attention_heads,
             tester.seq_length,
             tester.key_len if hasattr(tester, 'key_len') else tester.seq_length])

def _create_and_check_for_hidden_states(tester, model_classes, config, inputs_dict):
    for model_class in model_classes:
        config.output_hidden_states = True
        config.output_attentions = False
        model = model_class(config)
        model.eval()
        outputs = model(**inputs_dict)
        hidden_states = outputs[-1]
        tester.parent.assertEqual(model.config.output_attentions, False)
        tester.parent.assertEqual(model.config.output_hidden_states, True)
        tester.parent.assertEqual(len(hidden_states), tester.num_hidden_layers + 1)
        tester.parent.assertListEqual(
            list(hidden_states[0].shape[-2:]),
            [tester.seq_length, tester.hidden_size])


def create_and_check_commons(tester, config, inputs_dict, test_pruning=True, test_torchscript=True):
    _create_and_check_initialization(tester, tester.all_model_classes, config, inputs_dict)
    _create_and_check_for_attentions(tester, tester.all_model_classes, config, inputs_dict)
    _create_and_check_for_headmasking(tester, tester.all_model_classes, config, inputs_dict)
    _create_and_check_for_hidden_states(tester, tester.all_model_classes, config, inputs_dict)

    if test_torchscript:
        _create_and_check_torchscript(tester, tester.all_model_classes, config, inputs_dict)
        _create_and_check_torchscript_output_attentions(tester, tester.all_model_classes, config, inputs_dict)
        _create_and_check_torchscript_output_hidden_state(tester, tester.all_model_classes, config, inputs_dict)

    if test_pruning:
        _create_and_check_for_head_pruning(tester, tester.all_model_classes, config, inputs_dict)


def ids_tensor(shape, vocab_size, rng=None, name=None):
    """Creates a random int32 tensor of the shape within the vocab size."""
    if rng is None:
        rng = random.Random()

    total_dims = 1
    for dim in shape:
        total_dims *= dim

    values = []
    for _ in range(total_dims):
        values.append(rng.randint(0, vocab_size - 1))

    return torch.tensor(data=values, dtype=torch.long).view(shape).contiguous()


class ConfigTester(object):
    def __init__(self, parent, config_class=None, **kwargs):
        self.parent = parent
        self.config_class = config_class
        self.inputs_dict = kwargs

    def create_and_test_config_common_properties(self):
        config = self.config_class(**self.inputs_dict)
        self.parent.assertTrue(hasattr(config, 'vocab_size'))
        self.parent.assertTrue(hasattr(config, 'hidden_size'))
        self.parent.assertTrue(hasattr(config, 'num_attention_heads'))
        self.parent.assertTrue(hasattr(config, 'num_hidden_layers'))

    def create_and_test_config_to_json_string(self):
        config = self.config_class(**self.inputs_dict)
        obj = json.loads(config.to_json_string())
        for key, value in self.inputs_dict.items():
            self.parent.assertEqual(obj[key], value)

    def create_and_test_config_to_json_file(self):
        config_first = self.config_class(**self.inputs_dict)
        json_file_path = "/tmp/config.json"
        config_first.to_json_file(json_file_path)
        config_second = self.config_class.from_json_file(json_file_path)
        os.remove(json_file_path)
        self.parent.assertEqual(config_second.to_dict(), config_first.to_dict())

    def run_common_tests(self):
        self.create_and_test_config_common_properties()
        self.create_and_test_config_to_json_string()
        self.create_and_test_config_to_json_file()


class GPTModelTester(object):
    def __init__(self,
                    parent,
                    batch_size=13,
                    seq_length=7,
                    is_training=True,
                    use_position_ids=True,
                    use_token_type_ids=True,
                    use_labels=True,
                    vocab_size=99,
                    n_positions=33,
                    hidden_size=32,
                    num_hidden_layers=5,
                    num_attention_heads=4,
                    n_choices=3,
                    type_sequence_label_size=2,
                    initializer_range=0.02,
                    num_labels=3,
                    scope=None,
                    config_class=None,
                    base_model_class=None,
                    lm_head_model_class=None,
                    double_head_model_class=None,
                    ):
        self.parent = parent
        self.batch_size = batch_size
        self.seq_length = seq_length
        self.is_training = is_training
        self.use_position_ids = use_position_ids
        self.use_token_type_ids = use_token_type_ids
        self.use_labels = use_labels
        self.vocab_size = vocab_size
        self.n_positions = n_positions
        self.hidden_size = hidden_size
        self.num_hidden_layers = num_hidden_layers
        self.num_attention_heads = num_attention_heads
        self.n_choices = n_choices
        self.type_sequence_label_size = type_sequence_label_size
        self.initializer_range = initializer_range
        self.num_labels = num_labels
        self.scope = scope
        self.config_class = config_class
        self.base_model_class = base_model_class
        self.lm_head_model_class = lm_head_model_class
        self.double_head_model_class = double_head_model_class
        self.all_model_classes = (base_model_class, lm_head_model_class, double_head_model_class)

    def prepare_config_and_inputs(self):
        total_num_tokens = self.vocab_size
        input_ids = ids_tensor([self.batch_size, self.n_choices, self.seq_length], total_num_tokens)

        position_ids = None
        if self.use_position_ids:
            position_ids = ids_tensor([self.batch_size, self.n_choices, self.seq_length], self.n_positions)

        token_type_ids = None
        if self.use_token_type_ids:
            total_voc = self.vocab_size
            token_type_ids = ids_tensor([self.batch_size, self.n_choices, self.seq_length], total_voc)

        mc_labels = None
        lm_labels = None
        mc_token_ids = None
        if self.use_labels:
            mc_labels = ids_tensor([self.batch_size], self.type_sequence_label_size)
            lm_labels = ids_tensor([self.batch_size, self.n_choices, self.seq_length], self.num_labels)
            mc_token_ids = ids_tensor([self.batch_size, self.n_choices], self.seq_length)

        config = self.config_class(
            vocab_size_or_config_json_file=self.vocab_size,
            n_positions=self.n_positions,
            n_embd=self.hidden_size,
            n_layer=self.num_hidden_layers,
            n_head=self.num_attention_heads,
            initializer_range=self.initializer_range)

        return (config, input_ids, token_type_ids, position_ids,
                mc_labels, lm_labels, mc_token_ids)

    def create_and_check_base_model(self, config, input_ids, token_type_ids, position_ids,
                            mc_labels, lm_labels, mc_token_ids):
        model = self.base_model_class(config)
        model.eval()

        outputs = model(input_ids, position_ids, token_type_ids)
        outputs = model(input_ids, position_ids)
        outputs = model(input_ids)

        hidden_state = outputs[0]
        self.parent.assertListEqual(
            list(hidden_state.size()),
            [self.batch_size, self.n_choices, self.seq_length, self.hidden_size])


    def create_and_check_lm_head(self, config, input_ids, token_type_ids, position_ids,
                                    mc_labels, lm_labels, mc_token_ids):
        model = self.lm_head_model_class(config)
        model.eval()
        outputs = model(input_ids, position_ids, token_type_ids, lm_labels)
        loss, lm_logits = outputs[:2]

        total_voc = self.vocab_size
        self.parent.assertListEqual(
            list(lm_logits.size()),
            [self.batch_size, self.n_choices, self.seq_length, total_voc])
        self.parent.assertListEqual(
            list(loss.size()),
            [])

    def create_and_check_presents(self, config, input_ids, token_type_ids, position_ids,
                                    mc_labels, lm_labels, mc_token_ids):
        for model_class in self.all_model_classes:
            model = model_class(config)
            model.eval()
            outputs = model(input_ids)
            presents = outputs[-1]
            self.parent.assertEqual(self.num_hidden_layers, len(presents))
            self.parent.assertListEqual(
                list(presents[0].size()),
                [2, self.batch_size * self.n_choices, self.num_attention_heads,
                    self.seq_length, self.hidden_size // self.num_attention_heads])

    def create_and_check_double_heads(self, config, input_ids, token_type_ids, position_ids,
                                    mc_labels, lm_labels, mc_token_ids):
        model = self.double_head_model_class(config)
        model.eval()
        outputs = model(input_ids, mc_token_ids, lm_labels=lm_labels, mc_labels=mc_labels,
                        token_type_ids=token_type_ids, position_ids=position_ids)
        lm_loss, mc_loss, lm_logits, mc_logits = outputs[:4]
        loss = [lm_loss, mc_loss]

        total_voc = self.vocab_size
        self.parent.assertListEqual(
            list(lm_logits.size()),
            [self.batch_size, self.n_choices, self.seq_length, total_voc])
        self.parent.assertListEqual(
            list(mc_logits.size()),
            [self.batch_size, self.n_choices])
        self.parent.assertListEqual(
            [list(l.size()) for l in loss],
            [[], []])

    def create_and_check_model_from_pretrained(self):
        cache_dir = "/tmp/pytorch_transformers_test/"
        for model_name in list(self.base_model_class.pretrained_model_archive_map.keys())[:1]:
            model = self.base_model_class.from_pretrained(model_name, cache_dir=cache_dir)
            shutil.rmtree(cache_dir)
            self.parent.assertIsNotNone(model)

    def create_and_check_commons(self, config, input_ids, token_type_ids, position_ids,
                                    mc_labels, lm_labels, mc_token_ids):
        inputs_dict = {'input_ids': input_ids}
        create_and_check_commons(self, config, inputs_dict)

    def run_common_tests(self, test_presents=False):
        config_and_inputs = self.prepare_config_and_inputs()
        self.create_and_check_base_model(*config_and_inputs)

        config_and_inputs = self.prepare_config_and_inputs()
        self.create_and_check_lm_head(*config_and_inputs)

        config_and_inputs = self.prepare_config_and_inputs()
        self.create_and_check_double_heads(*config_and_inputs)

        if test_presents:
            config_and_inputs = self.prepare_config_and_inputs()
            self.create_and_check_presents(*config_and_inputs)

        config_and_inputs = self.prepare_config_and_inputs()
        self.create_and_check_commons(*config_and_inputs)

    def run_slow_tests(self):
        self.create_and_check_model_from_pretrained()


class ModelUtilsTest(unittest.TestCase):
    def test_model_from_pretrained(self):
        logging.basicConfig(level=logging.INFO)
        for model_name in list(BERT_PRETRAINED_MODEL_ARCHIVE_MAP.keys())[:1]:
            config = BertConfig.from_pretrained(model_name)
            self.assertIsNotNone(config)
            self.assertIsInstance(config, PretrainedConfig)

            model = BertModel.from_pretrained(model_name)
            model, loading_info = BertModel.from_pretrained(model_name, output_loading_info=True)
            self.assertIsNotNone(model)
            self.assertIsInstance(model, PreTrainedModel)
            for value in loading_info.values():
                self.assertEqual(len(value), 0)

            config = BertConfig.from_pretrained(model_name, output_attentions=True, output_hidden_states=True)
            model = BertModel.from_pretrained(model_name, output_attentions=True, output_hidden_states=True)
            self.assertEqual(model.config.output_attentions, True)
            self.assertEqual(model.config.output_hidden_states, True)
            self.assertEqual(model.config, config)


if __name__ == "__main__":
    unittest.main()