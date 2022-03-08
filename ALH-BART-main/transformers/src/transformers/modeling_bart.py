# coding=utf-8
# Copyright 2020 The Facebook AI Research Team Authors and The HuggingFace Inc. team.
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
"""PyTorch BART model, ported from the fairseq repo."""
import math
import random
import warnings
from typing import Dict, List, Optional, Tuple

from torch.nn.utils.rnn import pad_sequence

import numpy as np
import torch
import torch.nn.functional as F
from torch import Tensor, nn
from torch.nn import CrossEntropyLoss

from .activations import ACT2FN
from .configuration_bart import BartConfig
from .file_utils import (
    add_code_sample_docstrings,
    add_end_docstrings,
    add_start_docstrings,
    add_start_docstrings_to_callable,
    replace_return_docstrings,
)
from .modeling_outputs import (
    BaseModelOutput,
    BaseModelOutputWithPast,
    Seq2SeqLMOutput,
    Seq2SeqModelOutput,
    Seq2SeqQuestionAnsweringModelOutput,
    Seq2SeqSequenceClassifierOutput,
)
from .modeling_utils import PreTrainedModel
from .utils import logging

logger = logging.get_logger(__name__)

_CONFIG_FOR_DOC = "BartConfig"
_TOKENIZER_FOR_DOC = "BartTokenizer"

BART_PRETRAINED_MODEL_ARCHIVE_LIST = [
    "facebook/bart-base",
    "facebook/bart-large",
    "facebook/bart-large-mnli",
    "facebook/bart-large-cnn",
    "facebook/bart-large-xsum",
    "facebook/mbart-large-en-ro",
]
# This list is incomplete. See all BART models at https://huggingface.co/models?filter=bart


BART_START_DOCSTRING = r"""

    This model is a PyTorch `torch.nn.Module <https://pytorch.org/docs/stable/nn.html#torch.nn.Module>`_ sub-class. Use it as a regular PyTorch Module and
    refer to the PyTorch documentation for all matters related to general usage and behavior.

    Parameters:
        config (:class:`~transformers.BartConfig`): Model configuration class with all the parameters of the model.
            Initializing with a config file does not load the weights associated with the model, only the configuration.
            Check out the :meth:`~transformers.PreTrainedModel.from_pretrained` method to load the model weights.

"""
BART_GENERATION_EXAMPLE = r"""
    Summarization example::

        >>> from transformers import BartTokenizer, BartForConditionalGeneration, BartConfig

        >>> # see ``examples/summarization/bart/run_eval.py`` for a longer example
        >>> model = BartForConditionalGeneration.from_pretrained('facebook/bart-large-cnn')
        >>> tokenizer = BartTokenizer.from_pretrained('facebook/bart-large-cnn')

        >>> ARTICLE_TO_SUMMARIZE = "My friends are cool but they eat too many carbs."
        >>> inputs = tokenizer([ARTICLE_TO_SUMMARIZE], max_length=1024, return_tensors='pt')

        >>> # Generate Summary
        >>> summary_ids = model.generate(inputs['input_ids'], num_beams=4, max_length=5, early_stopping=True)
        >>> print([tokenizer.decode(g, skip_special_tokens=True, clean_up_tokenization_spaces=False) for g in summary_ids])

"""

BART_INPUTS_DOCSTRING = r"""
    Args:
        input_ids (:obj:`torch.LongTensor` of shape :obj:`(batch_size, sequence_length)`):
               Indices of input sequence tokens in the vocabulary. Use BartTokenizer.encode to produce them.
            Padding will be ignored by default should you provide it.
            Indices can be obtained using :class:`transformers.BartTokenizer.encode(text)`.
        attention_mask (:obj:`torch.Tensor` of shape :obj:`(batch_size, sequence_length)`, `optional`):
            Mask to avoid performing attention on padding token indices in input_ids.
            Mask values selected in ``[0, 1]``:
            ``1`` for tokens that are NOT MASKED, ``0`` for MASKED tokens.
        encoder_outputs (:obj:`tuple(tuple(torch.FloatTensor)`, `optional`):
            Tuple consists of (`last_hidden_state`, `optional`: `hidden_states`, `optional`: `attentions`)
            `last_hidden_state` of shape :obj:`(batch_size, sequence_length, hidden_size)`, `optional`) is a sequence of hidden-states at the output of the last layer of the encoder.
            Used in the cross-attention of the decoder.
        decoder_input_ids (:obj:`torch.LongTensor` of shape :obj:`(batch_size, target_sequence_length)`, `optional`):
            Provide for translation and summarization training. By default, the model will create this tensor by shifting the input_ids right, following the paper.
        decoder_attention_mask (:obj:`torch.BoolTensor` of shape :obj:`(batch_size, tgt_seq_len)`, `optional`):
            Default behavior: generate a tensor that ignores pad tokens in decoder_input_ids. Causal mask will also be used by default.
            If you want to change padding behavior, you should read :func:`~transformers.modeling_bart._prepare_decoder_inputs` and modify.
            See diagram 1 in the paper for more info on the default strategy
        past_key_values (:obj:`tuple(tuple(torch.FloatTensor))` of length :obj:`config.n_layers` with each tuple having 4 tensors of shape :obj:`(batch_size, num_heads, sequence_length - 1, embed_size_per_head)`):
            Contains pre-computed key and value hidden-states of the attention blocks.
            Can be used to speed up decoding.
            If ``past_key_values`` are used, the user can optionally input only the last
            ``decoder_input_ids`` (those that don't have their past key value states given to this model) of shape
            :obj:`(batch_size, 1)` instead of all ``decoder_input_ids`` of shape :obj:`(batch_size, sequence_length)`.
        use_cache (:obj:`bool`, `optional`, defaults to :obj:`True`):
            If `use_cache` is True, ``past_key_values`` are returned and can be used to speed up decoding (see
            ``past_key_values``).
        output_attentions (:obj:`bool`, `optional`):
            If set to ``True``, the attentions tensors of all attention layers are returned. See ``attentions`` under returned tensors for more detail.
        output_hidden_states (:obj:`bool`, `optional`):
            If set to ``True``, the hidden states of all layers are returned. See ``hidden_states`` under returned tensors for more detail.
        return_dict (:obj:`bool`, `optional`):
            If set to ``True``, the model will return a :class:`~transformers.file_utils.ModelOutput` instead of a
            plain tuple.
"""


def invert_mask(attention_mask):
    """Turns 1->0, 0->1, False->True, True-> False"""
    assert attention_mask.dim() == 2
    return attention_mask.eq(0)


def _prepare_bart_decoder_inputs(
        config, input_ids, decoder_input_ids=None, decoder_padding_mask=None, causal_mask_dtype=torch.float32
):
    """Prepare masks that ignore padding tokens in the decoder and a causal mask for the decoder if
    none are provided. This mimics the default behavior in fairseq. To override it pass in masks.
    Note: this is not called during generation
    """
    pad_token_id = config.pad_token_id
    if decoder_input_ids is None:
        decoder_input_ids = shift_tokens_right(input_ids, pad_token_id)
    bsz, tgt_len = decoder_input_ids.size()
    if decoder_padding_mask is None:
        decoder_padding_mask = make_padding_mask(decoder_input_ids, pad_token_id)
    else:
        decoder_padding_mask = invert_mask(decoder_padding_mask)
    if decoder_padding_mask is not None and decoder_padding_mask.shape[1] > 1:
        # never mask leading token, even if it is pad
        decoder_padding_mask[:, 0] = decoder_padding_mask[:, 1]
    tmp = fill_with_neg_inf(torch.zeros(tgt_len, tgt_len))
    mask = torch.arange(tmp.size(-1))
    tmp.masked_fill_(mask < (mask + 1).view(tmp.size(-1), 1), 0)
    causal_mask = tmp.to(dtype=causal_mask_dtype, device=decoder_input_ids.device)
    return decoder_input_ids, decoder_padding_mask, causal_mask


class PretrainedBartModel(PreTrainedModel):
    config_class = BartConfig
    base_model_prefix = "model"

    def _init_weights(self, module):
        std = self.config.init_std
        if isinstance(module, nn.Linear):
            module.weight.data.normal_(mean=0.0, std=std)
            if module.bias is not None:
                module.bias.data.zero_()
        elif isinstance(module, SinusoidalPositionalEmbedding):
            pass
        elif isinstance(module, nn.Embedding):
            module.weight.data.normal_(mean=0.0, std=std)
            if module.padding_idx is not None:
                module.weight.data[module.padding_idx].zero_()

    @property
    def dummy_inputs(self):
        pad_token = self.config.pad_token_id
        input_ids = torch.tensor([[0, 6, 10, 4, 2], [0, 8, 12, 2, pad_token]], device=self.device)
        dummy_inputs = {
            "attention_mask": input_ids.ne(pad_token),
            "input_ids": input_ids,
        }
        return dummy_inputs


def _make_linear_from_emb(emb):
    vocab_size, emb_size = emb.weight.shape
    lin_layer = nn.Linear(vocab_size, emb_size, bias=False)
    lin_layer.weight.data = emb.weight.data
    return lin_layer


# Helper Functions, mostly for making masks
def _check_shapes(shape_1, shape2):
    if shape_1 != shape2:
        raise AssertionError("shape mismatch: {} != {}".format(shape_1, shape2))


def shift_tokens_right(input_ids, pad_token_id):
    """Shift input ids one token to the right, and wrap the last non pad token (usually <eos>)."""
    prev_output_tokens = input_ids.clone()
    index_of_eos = (input_ids.ne(pad_token_id).sum(dim=1) - 1).unsqueeze(-1)
    prev_output_tokens[:, 0] = input_ids.gather(1, index_of_eos).squeeze()
    prev_output_tokens[:, 1:] = input_ids[:, :-1]
    return prev_output_tokens


def make_padding_mask(input_ids, padding_idx=1):
    """True for pad tokens"""
    padding_mask = input_ids.eq(padding_idx)
    if not padding_mask.any():
        padding_mask = None
    return padding_mask


# Helper Modules


class EncoderLayer(nn.Module):
    def __init__(self, config: BartConfig, self_attn):
        super().__init__()
        self.embed_dim = config.d_model
        if self_attn is not None:
            self.self_attn = self_attn
        else:
            self.self_attn = Attention(self.embed_dim, config.encoder_attention_heads, dropout=config.attention_dropout)

        self.normalize_before = config.normalize_before
        self.self_attn_layer_norm = LayerNorm(self.embed_dim)
        self.dropout = config.dropout
        self.activation_fn = ACT2FN[config.activation_function]
        self.activation_dropout = config.activation_dropout
        self.fc1 = nn.Linear(self.embed_dim, config.encoder_ffn_dim)
        self.fc2 = nn.Linear(config.encoder_ffn_dim, self.embed_dim)
        self.final_layer_norm = LayerNorm(self.embed_dim)

    def forward(self, x, encoder_padding_mask, output_attentions=False):
        """
        Args:
            x (Tensor): input to the layer of shape `(seq_len, batch, embed_dim)`
            encoder_padding_mask (ByteTensor): binary ByteTensor of shape
                `(batch, src_len)` where padding elements are indicated by ``1``.
            for t_tgt, t_src is excluded (or masked out), =0 means it is
            included in attention

        Returns:
            encoded output of shape `(seq_len, batch, embed_dim)`
        """
        residual = x
        if self.normalize_before:
            x = self.self_attn_layer_norm(x)
        x, attn_weights = self.self_attn(
            query=x, key=x, key_padding_mask=encoder_padding_mask, output_attentions=output_attentions
        )
        x = F.dropout(x, p=self.dropout, training=self.training)
        x = residual + x
        if not self.normalize_before:
            x = self.self_attn_layer_norm(x)

        residual = x
        if self.normalize_before:
            x = self.final_layer_norm(x)
        x = self.activation_fn(self.fc1(x))
        x = F.dropout(x, p=self.activation_dropout, training=self.training)
        x = self.fc2(x)
        x = F.dropout(x, p=self.dropout, training=self.training)
        x = residual + x
        if not self.normalize_before:
            x = self.final_layer_norm(x)
        return x, attn_weights


class BartEncoder(nn.Module):
    """
    Transformer encoder consisting of *config.encoder_layers* self attention layers. Each layer
    is a :class:`EncoderLayer`.

    Args:
        config: BartConfig
    """

    def __init__(self, config: BartConfig, embed_tokens):
        super().__init__()

        self.dropout = config.dropout
        self.layerdrop = config.encoder_layerdrop

        embed_dim = embed_tokens.embedding_dim
        self.embed_dim = config.d_model
        self.embed_scale = math.sqrt(embed_dim) if config.scale_embedding else 1.0
        self.padding_idx = embed_tokens.padding_idx
        self.max_source_positions = config.max_position_embeddings

        self.embed_tokens = embed_tokens
        if config.static_position_embeddings:
            self.embed_positions = SinusoidalPositionalEmbedding(
                config.max_position_embeddings, embed_dim, self.padding_idx
            )
        else:
            self.embed_positions = LearnedPositionalEmbedding(
                config.max_position_embeddings,
                embed_dim,
                self.padding_idx,
                config.extra_pos_embeddings,
            )
        if config.attention_group == 0:
            self.layers = nn.ModuleList([EncoderLayer(config, None) for _ in range(config.encoder_layers)])
        else:
            self.self_attn1 = Attention(self.embed_dim, config.encoder_attention_heads, dropout=config.attention_dropout)
            self.self_attn2 = Attention(self.embed_dim, config.encoder_attention_heads, dropout=config.attention_dropout)
            self.layers = nn.ModuleList([EncoderLayer(config,self.self_attn1) for _ in range(config.encoder_layers-config.attention_group)])
            self.layers.extend(nn.ModuleList([EncoderLayer(config, self.self_attn2) for _ in range(config.attention_group)]))
        self.layernorm_embedding = LayerNorm(embed_dim) if config.normalize_embedding else nn.Identity()
        # mbart has one extra layer_norm
        self.layer_norm = LayerNorm(config.d_model) if config.add_final_layer_norm else None

    def transform_format(self, input_ids):

        divide_convs = []
        utterances = []

        for i in range(0, input_ids.shape[0]):

            seg_start = torch.where(input_ids[i] == 0)[0]
            seg_end = torch.where(input_ids[i] == 2)[0]

            # print('start', seg_start)
            # print('end', seg_end)
            count = 0
            for j in range(0, seg_end.shape[0]):
                uttr = input_ids[i][seg_start[j]: seg_end[j] + 1]
                utterances.append(uttr)
                count += 1
            divide_convs.append(count)

        utterances = pad_sequence(utterances, batch_first=True, padding_value=1)
        # utterance_num * max_len

        utterances_padding = utterances.eq(1)
        # print(utterances_padding)
        utterances_padding_mask = 1 - utterances_padding.type(torch.long)
        # print(utterances_padding_mask)
        return utterances, utterances_padding_mask, divide_convs

    def transform_back(self, x, count, input_ids, original_input_ids):

        convs = [torch.tensor(torch.zeros([original_input_ids.shape[1], x.shape[-1]]), requires_grad=True).to(x.device)]
        # print(count)
        for i in range(0, len(count)):
            num_utter = count[i]
            if i == 0:
                start_ids = 0
            else:
                start_ids += count[i - 1]

            temp_conv = []
            # print(input_ids)
            for j in range(0, num_utter):
                seg_start = torch.where(input_ids[start_ids + j] == 0)[0][0]
                seg_end = torch.where(input_ids[start_ids + j] == 2)[0][0]
                # print(seg_start, seg_end)
                temp_conv.append(x[start_ids + j][seg_start:seg_end + 1, :])

            # print(torch.cat(temp_conv, dim = 0).shape)
            convs.append(torch.cat(temp_conv, dim=0))

        convs = pad_sequence(convs, batch_first=True)
        return convs[1:, :, :]

    def forward(
            self, input_ids, attention_mask=None, output_attentions=False, output_hidden_states=False,
            return_dict=False, segmented_encoder=False, sent_encoder=False,
    ):
        """
        Args:
            input_ids (LongTensor): tokens in the source language of shape
                `(batch, src_len)`
            attention_mask (torch.LongTensor): indicating which indices are padding tokens.
        Returns:
            BaseModelOutput or Tuple comprised of:
                - **x** (Tensor): the last encoder layer's output of
                  shape `(src_len, batch, embed_dim)`
                - **encoder_states** (tuple(torch.FloatTensor)): all intermediate
                  hidden states of shape `(src_len, batch, embed_dim)`.
                  Only populated if *output_hidden_states:* is True.
                - **all_attentions** (tuple(torch.FloatTensor)): Attention weights for each layer.
                During training might not be of length n_layers because of layer dropout.
        """

        # batch * src_len ---> (batch * sentence_num) * src_len
        original_attn_mask = attention_mask
        # print(attention_mask.shape)
        original_input_ids = input_ids
        x = original_input_ids
        if segmented_encoder:
            # print('before ids', input_ids.shape)
            input_ids, attention_mask, count = self.transform_format(input_ids)
            # print('after ids', input_ids.shape)

        # check attention mask and invert
        if attention_mask is not None:
            attention_mask = invert_mask(attention_mask)
        if not sent_encoder:
            inputs_embeds = self.embed_tokens(input_ids) * self.embed_scale
            embed_pos = self.embed_positions(input_ids)
            x = inputs_embeds + embed_pos
            x = self.layernorm_embedding(x)
            x = F.dropout(x, p=self.dropout, training=self.training)

        # B x T x C -> T x B x C
        x = x.transpose(0, 1)

        encoder_states = [] if output_hidden_states else None
        all_attentions = () if output_attentions else None
        for encoder_layer in self.layers:
            if output_hidden_states:
                encoder_states.append(x)
            # add LayerDrop (see https://arxiv.org/abs/1909.11556 for description)
            dropout_probability = random.uniform(0, 1)
            if self.training and (dropout_probability < self.layerdrop):  # skip the layer
                attn = None
            else:
                x, attn = encoder_layer(x, attention_mask, output_attentions=output_attentions)

            if output_attentions:
                all_attentions = all_attentions + (attn,)

        if self.layer_norm:
            x = self.layer_norm(x)
        if output_hidden_states:
            encoder_states.append(x)
            # T x B x C -> B x T x C
            encoder_states = tuple(hidden_state.transpose(0, 1) for hidden_state in encoder_states)

        # T x B x C -> B x T x C
        x = x.transpose(0, 1)

        if segmented_encoder:
            # print('before x', x.shape)
            x = self.transform_back(x, count, input_ids, original_input_ids)
            attention_mask = original_attn_mask
            input_ids = original_input_ids

            # print('after x', x.shape)

        if not return_dict:
            return tuple(v for v in [x, encoder_states, all_attentions] if v is not None)
        return BaseModelOutput(last_hidden_state=x, hidden_states=encoder_states, attentions=all_attentions)


class DecoderLayer(nn.Module):
    def __init__(self, config: BartConfig):
        super().__init__()
        self.embed_dim = config.d_model
        self.discourse_graph = config.discourse_graph
        self.action_graph = config.action_graph
        self.composit = config.composit
        self.no_rezero = config.no_rezero

        self.self_attn = Attention(
            embed_dim=self.embed_dim,
            num_heads=config.decoder_attention_heads,
            dropout=config.attention_dropout,
        )
        self.dropout = config.dropout
        self.activation_fn = ACT2FN[config.activation_function]
        self.activation_dropout = config.activation_dropout
        self.normalize_before = config.normalize_before

        self.self_attn_layer_norm = LayerNorm(self.embed_dim)
        self.encoder_attn = Attention(
            self.embed_dim,
            config.decoder_attention_heads,
            dropout=config.attention_dropout,
            encoder_decoder_attention=True,
        )
        self.encoder_attn_layer_norm = LayerNorm(self.embed_dim)
        self.resweight = nn.Parameter(torch.Tensor([1]))
        self.sent_encoder = config.sent_encoder

        # TODO: added part
        if config.sent_encoder:
            
            self.sent_attn = Attention(
                self.embed_dim,
                config.decoder_attention_heads,
                dropout=config.attention_dropout,
                sent_decoder_attention=True,
            )
            self.sent_attn_layer_norm = LayerNorm(self.embed_dim)
            self.composit_layer = nn.Linear(self.embed_dim * 2, self.embed_dim)

        if config.discourse_graph:

            if config.discourse_attn_head == 0:
                self.discourse_attn = Attention(
                    self.embed_dim,
                    config.decoder_attention_heads,
                    dropout=config.attention_dropout,
                    graph_decoder_attention=True,
                )
            else:
                self.discourse_attn = Attention(
                    self.embed_dim,
                    config.discourse_attn_head,
                    dropout=config.attention_dropout,
                    graph_decoder_attention=True,
                )
            self.discourse_attn_layer_norm = LayerNorm(self.embed_dim)

            # if config.no_rezero:
            #    self.combine_discourse_linear = nn.Linear(self.embed_dim * 2, self.embed_dim)
            # if config.composit:
            #    self.resweight = nn.Parameter(torch.Tensor([0]))
            # else:
            self.resweight = nn.Parameter(torch.Tensor([1]))

        if config.action_graph:
            self.action_attn = Attention(
                self.embed_dim,
                config.discourse_attn_head,
                dropout=config.attention_dropout,
                action_decoder_attention=True,
            )
            self.action_attn_layer_norm = LayerNorm(self.embed_dim)
            self.resweight_2 = nn.Parameter(torch.Tensor([1]))

        if config.composit:
            self.composit_layer = nn.Linear(self.embed_dim * 2, self.embed_dim)
            self.composit_layer_norm = LayerNorm(self.embed_dim)

        self.fc1 = nn.Linear(self.embed_dim, config.decoder_ffn_dim)
        self.fc2 = nn.Linear(config.decoder_ffn_dim, self.embed_dim)
        self.final_layer_norm = LayerNorm(self.embed_dim)

    def forward(
            self,
            x,
            encoder_hidden_states,
            encoder_attn_mask=None,
            layer_state=None,
            causal_mask=None,
            decoder_padding_mask=None,
            output_attentions=False,
            graph_outputs=None,
            section_padding_mask=None,
            action_output=None,
            action_padding_mask=None,
            sent_outputs=None,
            sent_padding_mask=None,
    ):
        residual = x

        if layer_state is None:
            layer_state = {}
        if self.normalize_before:
            x = self.self_attn_layer_norm(x)
        # Self Attention
        # print("!!!!")
        x, self_attn_weights = self.self_attn(
            query=x,
            key=x,
            layer_state=layer_state,  # adds keys to layer state
            key_padding_mask=decoder_padding_mask,
            attn_mask=causal_mask,
            output_attentions=output_attentions,
        )
        x = F.dropout(x, p=self.dropout, training=self.training)
        x = residual + x
        # x = residual + self.resweight * x
        if not self.normalize_before:
            x = self.self_attn_layer_norm(x)

        # Cross attention
        residual = x
        sent_x = x
        assert self.encoder_attn.cache_key != self.self_attn.cache_key
        if self.normalize_before:
            x = self.encoder_attn_layer_norm(x)
        # print(encoder_hidden_states.shape)
        x, _ = self.encoder_attn(
            query=x,
            key=encoder_hidden_states,
            key_padding_mask=encoder_attn_mask,
            layer_state=layer_state,  # mutates layer state
        )
        x = F.dropout(x, p=self.dropout, training=self.training)
        if self.sent_encoder:
            
            assert self.sent_attn.cache_key != self.self_attn.cache_key
            if self.normalize_before:
                sent_x = self.sent_attn_layer_norm(sent_x)
            sent_x, _ = self.sent_attn(
                query=sent_x,
                key=sent_outputs,
                key_padding_mask=sent_padding_mask,
                layer_state=layer_state,  # mutates layer state
            )
            sent_x = F.dropout(sent_x, p=self.dropout, training=self.training)
            sent_x = self.resweight * sent_x
            x = torch.cat([x, sent_x], dim=-1)
            x = self.composit_layer(x)
            x = F.dropout(x, p=self.dropout, training=self.training)
            # if self.no_rezero:
            #     x = residual + x
            # else:
            #     x = residual + self.resweight * x
            x = residual +  x
            if not self.normalize_before:
                x = self.sent_attn_layer_norm(x)
        else:
            x = residual + x
            # x = residual + self.resweight * x
            if not self.normalize_before:
                x = self.encoder_attn_layer_norm(x)

        if not self.composit:
            # print("...")
            # Cross graph attention

            if self.discourse_graph:
                residual = x
                assert self.discourse_attn.cache_key != self.self_attn.cache_key
                if self.normalize_before:
                    x = self.discourse_attn_layer_norm(x)

                # print(graph_outputs.shape)
                # print(graph_outputs.shape, section_padding_mask.shape)
                x, _ = self.discourse_attn(
                    query=x,
                    key=graph_outputs,
                    key_padding_mask=section_padding_mask,
                    layer_state=layer_state,  # mutates layer state
                )

                x = F.dropout(x, p=self.dropout, training=self.training)

                if self.no_rezero:
                    x = residual + x
                else:
                    x = residual + self.resweight * x
                if not self.normalize_before:
                    x = self.discourse_attn_layer_norm(x)

            if self.action_graph:
                residual = x
                assert self.action_attn.cache_key != self.self_attn.cache_key
                if self.normalize_before:
                    x = self.action_attn_layer_norm(x)

                x, _ = self.action_attn(
                    query=x,
                    key=action_output,
                    key_padding_mask=action_padding_mask,
                    layer_state=layer_state,  # mutates layer state
                )

                x = F.dropout(x, p=self.dropout, training=self.training)

                if self.no_rezero:
                    x = residual + x
                else:
                    x = residual + self.resweight_2 * x
                if not self.normalize_before:
                    x = self.action_attn_layer_norm(x)

        elif self.composit:
            residual = x
            action_x = x
            discourse_x = x
            if self.action_graph:
                # residual = x
                assert self.action_attn.cache_key != self.self_attn.cache_key
                if self.normalize_before:
                    action_x = self.action_attn_layer_norm(action_x)

                # print(graph_outputs.shape)
                # print(action_output.shape, action_padding_mask.shape)
                action_x, _ = self.action_attn(
                    query=action_x,
                    key=action_output,
                    key_padding_mask=action_padding_mask,
                    layer_state=layer_state,  # mutates layer state
                )

                action_x = F.dropout(action_x, p=0.1, training=self.training)
                # if not self.normalize_before:
                #    action_x = self.action_attn_layer_norm(action_x)

            if self.discourse_graph:
                assert self.discourse_attn.cache_key != self.self_attn.cache_key
                if self.normalize_before:
                    discourse_x = self.discourse_attn_layer_norm(discourse_x)

                # print(graph_outputs.shape)
                # print(graph_outputs.shape, section_padding_mask.shape)
                discourse_x, _ = self.discourse_attn(
                    query=discourse_x,
                    key=graph_outputs,
                    key_padding_mask=section_padding_mask,
                    layer_state=layer_state,  # mutates layer state
                )

                discourse_x = F.dropout(discourse_x, p=0.1, training=self.training)
                # x = residual + self.resweight * x
                # if not self.normalize_before:
                #    discourse_x = self.discourse_attn_layer_norm(discourse_x)

            x = torch.cat([action_x, discourse_x], dim=-1)
            x = self.composit_layer(x)
            x = F.dropout(x, p=0.1, training=self.training)
            if self.no_rezero:
                x = residual + x
            else:
                x = residual + self.resweight * x
            # x = residual +  x
            if not self.normalize_before:
                x = self.composit_layer_norm(x)

        # Fully Connected
        residual = x
        if self.normalize_before:
            x = self.final_layer_norm(x)
        x = self.activation_fn(self.fc1(x))
        x = F.dropout(x, p=self.activation_dropout, training=self.training)
        x = self.fc2(x)
        x = F.dropout(x, p=self.dropout, training=self.training)
        x = residual + x
        if not self.normalize_before:
            x = self.final_layer_norm(x)
        return (
            x,
            self_attn_weights,
            layer_state,
        )  # just self_attn weights for now, following t5, layer_state = cache for decoding


class BartDecoder(nn.Module):
    """
    Transformer decoder consisting of *config.decoder_layers* layers. Each layer
    is a :class:`DecoderLayer`.
    Args:
        config: BartConfig
        embed_tokens (torch.nn.Embedding): output embedding
    """

    def __init__(self, config: BartConfig, embed_tokens: nn.Embedding):
        super().__init__()
        self.dropout = config.dropout
        self.layerdrop = config.decoder_layerdrop
        self.padding_idx = embed_tokens.padding_idx
        self.max_target_positions = config.max_position_embeddings
        self.embed_scale = math.sqrt(config.d_model) if config.scale_embedding else 1.0
        self.embed_tokens = embed_tokens
        if config.static_position_embeddings:
            self.embed_positions = SinusoidalPositionalEmbedding(
                config.max_position_embeddings, config.d_model, config.pad_token_id
            )
        else:
            self.embed_positions = LearnedPositionalEmbedding(
                config.max_position_embeddings,
                config.d_model,
                self.padding_idx,
                config.extra_pos_embeddings,
            )
        self.layers = nn.ModuleList(
            [DecoderLayer(config) for _ in range(config.decoder_layers)]
        )  # type: List[DecoderLayer]
        self.layernorm_embedding = LayerNorm(config.d_model) if config.normalize_embedding else nn.Identity()
        self.layer_norm = LayerNorm(config.d_model) if config.add_final_layer_norm else None

    def forward(
            self,
            input_ids,
            encoder_hidden_states,
            encoder_padding_mask,
            decoder_padding_mask,
            decoder_causal_mask,
            past_key_values=None,
            use_cache=False,
            output_attentions=False,
            output_hidden_states=False,
            return_dict=False,
            graph_outputs=None,
            section_padding_mask=None,
            action_output=None,
            action_padding_mask=None,
            sent_outputs=None,
            sent_padding_mask=None,
            **unused,
    ):
        """
        Includes several features from "Jointly Learning to Align and
        Translate with Transformer Models" (Garg et al., EMNLP 2019).

        Args:
            input_ids (LongTensor): previous decoder outputs of shape
                `(batch, tgt_len)`, for teacher forcing
            encoder_hidden_states: output from the encoder, used for
                encoder-side attention
            encoder_padding_mask: for ignoring pad tokens
            past_key_values (dict or None): dictionary used for storing state during generation

        Returns:
            BaseModelOutputWithPast or tuple:
                - the decoder's features of shape `(batch, tgt_len, embed_dim)`
                - the cache
                - hidden states
                - attentions
        """
        if "decoder_cached_states" in unused:
            warnings.warn(
                "The `decoder_cached_states` argument is deprecated and will be removed in a future version, use `past_key_values` instead.",
                FutureWarning,
            )
            past_key_values = unused.pop("decoder_cached_states")
        if "decoder_past_key_values" in unused:
            warnings.warn(
                "The `decoder_past_key_values` argument is deprecated and will be removed in a future version, use `past_key_values` instead.",
                FutureWarning,
            )
            past_key_values = unused.pop("decoder_past_key_values")

        # check attention mask and invert
        if encoder_padding_mask is not None:
            encoder_padding_mask = invert_mask(encoder_padding_mask)

        # print(encoder_padding_mask)
        if sent_padding_mask is not None:
            sent_padding_mask = invert_mask(sent_padding_mask)

        if section_padding_mask is not None:
            section_padding_mask = invert_mask(section_padding_mask)

        if action_padding_mask is not None:
            action_padding_mask = invert_mask(action_padding_mask)

        # print(section_padding_mask)
        # embed positions
        positions = self.embed_positions(input_ids, use_cache=use_cache)

        if use_cache:
            input_ids = input_ids[:, -1:]
            positions = positions[:, -1:]

        x = self.embed_tokens(input_ids) * self.embed_scale
        x += positions
        x = self.layernorm_embedding(x)
        x = F.dropout(x, p=self.dropout, training=self.training)

        # Convert to Bart output format: (seq_len, BS, model_dim) -> (BS, seq_len, model_dim)
        x = x.transpose(0, 1)
        encoder_hidden_states = encoder_hidden_states.transpose(0, 1)

        if sent_outputs is not None:
            sent_outputs = sent_outputs.transpose(0, 1)

        if graph_outputs is not None:
            graph_outputs = graph_outputs.transpose(0, 1)

        if action_output is not None:
            action_output = action_output.transpose(0, 1)
        # decoder layers
        all_hidden_states = () if output_hidden_states else None
        all_self_attns = () if output_attentions else None
        next_decoder_cache = []
        for idx, decoder_layer in enumerate(self.layers):
            # add LayerDrop (see https://arxiv.org/abs/1909.11556 for description)
            if output_hidden_states:
                all_hidden_states += (x,)
            dropout_probability = random.uniform(0, 1)
            if self.training and (dropout_probability < self.layerdrop):
                continue

            layer_state = past_key_values[idx] if past_key_values is not None else None

            # print(graph_outputs.shape)
            # print(encoder_hidden_states.shape)
            x, layer_self_attn, layer_past = decoder_layer(
                x,
                encoder_hidden_states,
                encoder_attn_mask=encoder_padding_mask,
                decoder_padding_mask=decoder_padding_mask,
                layer_state=layer_state,
                causal_mask=decoder_causal_mask,
                output_attentions=output_attentions,
                graph_outputs=graph_outputs,
                section_padding_mask=section_padding_mask,
                action_output=action_output,
                action_padding_mask=action_padding_mask,
                sent_outputs=sent_outputs,
                sent_padding_mask=sent_padding_mask,
            )

            if use_cache:
                next_decoder_cache.append(layer_past.copy())

            if output_attentions:
                all_self_attns += (layer_self_attn,)

        if self.layer_norm:  # if config.add_final_layer_norm (mBART)
            x = self.layer_norm(x)

        # Convert to standard output format: (seq_len, BS, model_dim) -> (BS, seq_len, model_dim)
        if output_hidden_states:
            all_hidden_states = tuple(hidden_state.transpose(0, 1) for hidden_state in all_hidden_states)
        x = x.transpose(0, 1)
        encoder_hidden_states = encoder_hidden_states.transpose(0, 1)

        next_cache = next_decoder_cache if use_cache else None

        if not return_dict:
            return tuple(v for v in [x, next_cache, all_hidden_states, all_self_attns] if v is not None)
        return BaseModelOutputWithPast(
            last_hidden_state=x, past_key_values=next_cache, hidden_states=all_hidden_states, attentions=all_self_attns
        )


def _reorder_buffer(attn_cache, new_order):
    for k, input_buffer_k in attn_cache.items():
        if input_buffer_k is not None:
            attn_cache[k] = input_buffer_k.index_select(0, new_order)
    return attn_cache


class Attention(nn.Module):
    """Multi-headed attention from 'Attention Is All You Need' paper"""

    def __init__(
            self,
            embed_dim,
            num_heads,
            dropout=0.0,
            bias=True,
            encoder_decoder_attention=False,  # otherwise self_attention
            sent_decoder_attention=False,
            graph_decoder_attention=False,
            action_decoder_attention=False,
    ):
        super().__init__()
        self.embed_dim = embed_dim
        self.num_heads = num_heads
        self.dropout = dropout
        self.head_dim = embed_dim // num_heads
        assert self.head_dim * num_heads == self.embed_dim, "embed_dim must be divisible by num_heads"
        self.scaling = self.head_dim ** -0.5

        self.encoder_decoder_attention = encoder_decoder_attention

        self.graph_decoder_attention = graph_decoder_attention
        self.sent_decoder_attention = sent_decoder_attention
        self.action_decoder_attention = action_decoder_attention

        self.k_proj = nn.Linear(embed_dim, embed_dim, bias=bias)
        self.v_proj = nn.Linear(embed_dim, embed_dim, bias=bias)
        self.q_proj = nn.Linear(embed_dim, embed_dim, bias=bias)
        self.out_proj = nn.Linear(embed_dim, embed_dim, bias=bias)

        if self.encoder_decoder_attention:
            self.cache_key = "encoder_decoder"
        elif self.sent_decoder_attention:
             self.cache_key = "sent_decoder"
        elif self.graph_decoder_attention:
            self.cache_key = "graph_decoder"
        elif self.action_decoder_attention:
            self.cache_key = 'action_decoder'
        else:
            self.cache_key = "self"

            # self.cache_key = "encoder_decoder" if self.encoder_decoder_attention else "self"

    def _shape(self, tensor, seq_len, bsz):
        return tensor.contiguous().view(seq_len, bsz * self.num_heads, self.head_dim).transpose(0, 1)

    def forward(
            self,
            query,
            key: Optional[Tensor],
            key_padding_mask: Optional[Tensor] = None,
            layer_state: Optional[Dict[str, Optional[Tensor]]] = None,
            attn_mask: Optional[Tensor] = None,
            output_attentions=False,
    ) -> Tuple[Tensor, Optional[Tensor]]:
        """Input shape: Time(SeqLen) x Batch x Channel"""
        static_kv: bool = self.encoder_decoder_attention or self.graph_decoder_attention or self.action_decoder_attention or self.sent_decoder_attention
        tgt_len, bsz, embed_dim = query.size()
        assert embed_dim == self.embed_dim
        assert list(query.size()) == [tgt_len, bsz, embed_dim]
        # print('cache_key1', self.cache_key)
        # get here for encoder decoder cause of static_kv
        if layer_state is not None:  # reuse k,v and encoder_padding_mask
            saved_state = layer_state.get(self.cache_key, {})
            # print('cache_key2', self.cache_key)
            
            if "prev_key" in saved_state and static_kv:
                # previous time steps are cached - no need to recompute key and value if they are static
                key = None
        else:
            saved_state = None
            layer_state = {}

        q = self.q_proj(query) * self.scaling
        
        if static_kv:
            if key is None:
                k = v = None
            else:
                k = self.k_proj(key)
                v = self.v_proj(key)
        else:
            k = self.k_proj(query)
            v = self.v_proj(query)

        q = self._shape(q, tgt_len, bsz)

        # print("k1", k.shape)

        if k is not None:
            k = self._shape(k, -1, bsz)
        if v is not None:
            v = self._shape(v, -1, bsz)

        # print("k2", k.shape)
        if saved_state is not None:
            # print(k, v)
            k, v, key_padding_mask = self._use_saved_state(k, v, saved_state, key_padding_mask, static_kv, bsz)

        # Update cache
        layer_state[self.cache_key] = {
            "prev_key": k.view(bsz, self.num_heads, -1, self.head_dim),
            "prev_value": v.view(bsz, self.num_heads, -1, self.head_dim),
            "prev_key_padding_mask": key_padding_mask if not static_kv else None,
        }

        assert k is not None

        # print("k", k.shape)

        src_len = k.size(1)
        attn_weights = torch.bmm(q, k.transpose(1, 2))
        assert attn_weights.size() == (bsz * self.num_heads, tgt_len, src_len)

        if attn_mask is not None:
            attn_weights = attn_weights.view(bsz, self.num_heads, tgt_len, src_len) + attn_mask
            attn_weights = attn_weights.view(bsz * self.num_heads, tgt_len, src_len)

        # This is part of a workaround to get around fork/join parallelism not supporting Optional types.
        if key_padding_mask is not None and key_padding_mask.dim() == 0:
            key_padding_mask = None
        # if key_padding_mask is not None:
        #    print(key_padding_mask.shape)
        #    print(bsz,src_len)

        # if self.action_decoder_attention or self.graph_decoder_attention:
        #    print(key_padding_mask)
        # print(None.shape)
        # if key_padding_mask is not None:
        #     print(key_padding_mask.shape)
        # print(bsz, src_len)
        # print(None.shape)
        assert key_padding_mask is None or key_padding_mask.size()[:2] == (
            bsz,
            src_len,
        )

        if key_padding_mask is not None:  # don't attend to padding symbols
            attn_weights = attn_weights.view(bsz, self.num_heads, tgt_len, src_len)
            reshaped = key_padding_mask.unsqueeze(1).unsqueeze(2)
            attn_weights = attn_weights.masked_fill(reshaped, float("-inf"))
            attn_weights = attn_weights.view(bsz * self.num_heads, tgt_len, src_len)
        attn_weights = F.softmax(attn_weights, dim=-1)
        attn_probs = F.dropout(
            attn_weights,
            p=self.dropout,
            training=self.training,
        )

        assert v is not None
        attn_output = torch.bmm(attn_probs, v)
        assert attn_output.size() == (bsz * self.num_heads, tgt_len, self.head_dim)
        attn_output = attn_output.transpose(0, 1).contiguous().view(tgt_len, bsz, embed_dim)
        attn_output = self.out_proj(attn_output)
        if output_attentions:
            attn_weights = attn_weights.view(bsz, self.num_heads, tgt_len, src_len)
        else:
            attn_weights = None
        return attn_output, attn_weights

    def _use_saved_state(self, k, v, saved_state, key_padding_mask, static_kv, bsz):
        # saved states are stored with shape (bsz, num_heads, seq_len, head_dim)
        if "prev_key" in saved_state:
            _prev_key = saved_state["prev_key"]
            assert _prev_key is not None

            prev_key = _prev_key.view(bsz * self.num_heads, -1, self.head_dim)
            if static_kv:
                k = prev_key
            else:
                assert k is not None
                k = torch.cat([prev_key, k], dim=1)
        if "prev_value" in saved_state:
            _prev_value = saved_state["prev_value"]
            assert _prev_value is not None
            prev_value = _prev_value.view(bsz * self.num_heads, -1, self.head_dim)
            if static_kv:
                v = prev_value
            else:
                assert v is not None
                v = torch.cat([prev_value, v], dim=1)
        assert k is not None and v is not None
        prev_key_padding_mask: Optional[Tensor] = saved_state.get("prev_key_padding_mask", None)
        if prev_key_padding_mask is not None:
            if static_kv:
                new_key_padding_mask = prev_key_padding_mask
            else:
                new_key_padding_mask = torch.cat([prev_key_padding_mask, key_padding_mask], dim=1)
        else:
            new_key_padding_mask = key_padding_mask
        return k, v, new_key_padding_mask


class BartClassificationHead(nn.Module):
    """Head for sentence-level classification tasks."""

    # This can trivially be shared with RobertaClassificationHead

    def __init__(
            self,
            input_dim,
            inner_dim,
            num_classes,
            pooler_dropout,
    ):
        super().__init__()
        self.dense = nn.Linear(input_dim, inner_dim)
        self.dropout = nn.Dropout(p=pooler_dropout)
        self.out_proj = nn.Linear(inner_dim, num_classes)

    def forward(self, x):
        x = self.dropout(x)
        x = self.dense(x)
        x = torch.tanh(x)
        x = self.dropout(x)
        x = self.out_proj(x)
        return x


class LearnedPositionalEmbedding(nn.Embedding):
    """
    This module learns positional embeddings up to a fixed maximum size.
    Padding ids are ignored by either offsetting based on padding_idx
    or by setting padding_idx to None and ensuring that the appropriate
    position ids are passed to the forward function.
    """

    def __init__(self, num_embeddings: int, embedding_dim: int, padding_idx: int, offset):
        # Bart is set up so that if padding_idx is specified then offset the embedding ids by 2
        # and adjust num_embeddings appropriately. Other models dont have this hack
        self.offset = offset
        assert padding_idx is not None
        num_embeddings += offset
        super().__init__(num_embeddings, embedding_dim, padding_idx=padding_idx)

    def forward(self, input_ids, use_cache=False):
        """Input is expected to be of size [bsz x seqlen]."""
        bsz, seq_len = input_ids.shape[:2]
        if use_cache:
            positions = input_ids.data.new(1, 1).fill_(seq_len - 1)  # called before slicing
        else:
            # starts at 0, ends at 1-seq_len
            positions = torch.arange(seq_len, dtype=torch.long, device=self.weight.device)
        return super().forward(positions + self.offset)


def LayerNorm(normalized_shape, eps=1e-5, elementwise_affine=True):
    if torch.cuda.is_available():
        try:
            from apex.normalization import FusedLayerNorm

            return FusedLayerNorm(normalized_shape, eps, elementwise_affine)
        except ImportError:
            pass
    return torch.nn.LayerNorm(normalized_shape, eps, elementwise_affine)


def fill_with_neg_inf(t):
    """FP16-compatible function that fills a input_ids with -inf."""
    return t.float().fill_(float("-inf")).type_as(t)


# Public API
def _get_shape(t):
    return getattr(t, "shape", None)


class GraphAttentionLayer(nn.Module):
    """
    Simple GAT layer, similar to https://arxiv.org/abs/1710.10903
    """

    def __init__(self, in_features, out_features, dropout, alpha, concat=True, relation=False):
        super(GraphAttentionLayer, self).__init__()
        self.dropout = dropout
        self.in_features = in_features
        self.out_features = out_features
        self.alpha = alpha
        self.concat = concat
        self.relation = relation

        self.W = nn.Parameter(torch.empty(size=(in_features, out_features)))
        nn.init.xavier_uniform_(self.W.data, gain=1.414)

        if self.relation:
            emb_matrix = torch.eye(18)
            self.one_hot_embedding = torch.nn.Embedding.from_pretrained(emb_matrix, freeze=True)
            self.a = nn.Parameter(torch.empty(size=(2 * out_features + 18, 1)))
        else:
            self.a = nn.Parameter(torch.empty(size=(2 * out_features, 1)))

        nn.init.xavier_uniform_(self.a.data, gain=1.414)

        self.leakyrelu = nn.LeakyReLU(self.alpha)
        self.layer_norm = LayerNorm(out_features)

    def forward(self, h, adj, relation=False):
        # Wh = torch.mm(h, self.W) # h.shape: (N, in_features), Wh.shape: (N, out_features)
        # print('h', h.shape)
        # print('W', self.W.shape)
        Wh = torch.matmul(h, self.W)  # b,N, N_out_features

        a_input = self._prepare_attentional_mechanism_input(Wh)

        if self.relation:
            long_adj = adj.clone().type(torch.LongTensor).cuda()
            relation_one_hot = self.one_hot_embedding(long_adj)

            # print(relation_one_hot.shape)

            a_input = torch.cat([a_input, relation_one_hot], dim=-1)

        # print(a_input.shape)

        e = self.leakyrelu(torch.matmul(a_input, self.a).squeeze(3))  # B, N , N

        zero_vec = -9e15 * torch.ones_like(e)
        # print(adj.shape)
        # print(e.shape)
        # TODO: Solve empty graph issue here!
        attention = torch.where(adj > 0, e, zero_vec)
        attention = F.softmax(attention, dim=2)  # B, N, N
        attention = F.dropout(attention, self.dropout, training=self.training)
        h_prime = torch.matmul(attention, Wh)

        h_prime = self.layer_norm(h_prime)
        # B, N, N_OUT_FEATURES

        if self.concat:
            return F.gelu(h_prime)
        else:
            return h_prime

    def _prepare_attentional_mechanism_input(self, Wh):
        N = Wh.size()[1]  # number of nodes
        B = Wh.size()[0]  # number of batches
        # Below, two matrices are created that contain embeddings in their rows in different orders.
        # (e stands for embedding)
        # These are the rows of the first matrix (Wh_repeated_in_chunks): 
        # e1, e1, ..., e1,            e2, e2, ..., e2,            ..., eN, eN, ..., eN
        # '-------------' -> N times  '-------------' -> N times       '-------------' -> N times
        # 
        # These are the rows of the second matrix (Wh_repeated_alternating): 
        # e1, e2, ..., eN, e1, e2, ..., eN, ..., e1, e2, ..., eN 
        # '----------------------------------------------------' -> N times
        # 
        # print('Wh', Wh.shape)
        Wh_repeated_in_chunks = Wh.repeat_interleave(N, dim=1)
        Wh_repeated_alternating = Wh.repeat(1, N, 1)
        # Wh_repeated_in_chunks.shape == Wh_repeated_alternating.shape == (N * N, out_features)

        # The all_combination_matrix, created below, will look like this (|| denotes concatenation):
        # e1 || e1
        # e1 || e2
        # e1 || e3
        # ...
        # e1 || eN
        # e2 || e1
        # e2 || e2
        # e2 || e3
        # ...
        # e2 || eN
        # ...
        # eN || e1
        # eN || e2
        # eN || e3
        # ...
        # eN || eN

        all_combinations_matrix = torch.cat([Wh_repeated_in_chunks, Wh_repeated_alternating], dim=2)
        # all_combinations_matrix.shape == (B, N * N, 2 * out_features)

        return all_combinations_matrix.view(B, N, N, 2 * self.out_features)

    def __repr__(self):
        return self.__class__.__name__ + ' (' + str(self.in_features) + ' -> ' + str(self.out_features) + ')'


class GAT(nn.Module):
    def __init__(self, config, nfeat, nhid, dropout=0.2, alpha=0.2, nheads=2, non_relation=False):
        """Dense version of GAT."""
        super(GAT, self).__init__()
        self.dropout = dropout
        if not non_relation:
            self.relation = config.relation
        else:
            self.relation = False

        self.attentions = [
            GraphAttentionLayer(nfeat, nhid, dropout=dropout, alpha=alpha, concat=True, relation=self.relation) for _ in
            range(nheads)]
        for i, attention in enumerate(self.attentions):
            self.add_module('attention_{}'.format(i), attention)
        self.out_att = GraphAttentionLayer(nhid * nheads, nhid, dropout=dropout, alpha=alpha, concat=False,
                                           relation=self.relation)

        self.fc = nn.Linear(nhid, nhid)
        self.layer_norm = LayerNorm(nhid)

    def forward(self, x, adj, relation=False):
        redisual = x
        x = F.dropout(x, self.dropout, training=self.training)
        x = torch.cat([att(x, adj) for att in self.attentions], dim=-1)
        x = F.dropout(x, self.dropout, training=self.training)
        x = F.gelu(self.out_att(x, adj))
        x = self.fc(x)
        x = x + redisual
        x = self.layer_norm(x)
        return x


'''
class GAT_action(nn.Module):
    def __init__(self, config, nfeat, nhid, dropout = 0.2, alpha = 0.2, nheads = 2, non_relation = False):
        """Dense version of GAT."""
        super(GAT_action, self).__init__()
        self.dropout = dropout
        
        self.relation = False

        self.attentions = [GraphAttentionLayer(nfeat, nhid, dropout=dropout, alpha=alpha, concat=True, relation = self.relation) for _ in range(nheads)]
        for i, attention in enumerate(self.attentions):
            self.add_module('attention_{}'.format(i), attention)
        self.out_att = GraphAttentionLayer(nhid * nheads, nhid, dropout=dropout, alpha=alpha, concat=True, relation = self.relation)

        self.out_att2 = GraphAttentionLayer(nhid, nhid, dropout=dropout, alpha=alpha, concat=False, relation = self.relation)
        
        self.fc = nn.Linear(nhid, nhid)
        self.layer_norm = LayerNorm(nhid)

    def forward(self, x, adj, relation = False):
        redisual = x
        x = F.dropout(x, self.dropout, training=self.training)
        x = torch.cat([att(x, adj) for att in self.attentions], dim=-1)
        x = F.dropout(x, self.dropout, training=self.training)
        x = self.out_att(x, adj)
        x = F.dropout(x, self.dropout, training=self.training)
        x = x + redisual
        x = self.layer_norm(x)
        x = F.gelu(self.out_att2(x, adj))
        x = self.fc(x)
        x = x + redisual
        x = self.layer_norm(x)
        return x
'''


@add_start_docstrings(
    "The bare BART Model outputting raw hidden-states without any specific head on top.",
    BART_START_DOCSTRING,
)
class BartModel(PretrainedBartModel):
    def __init__(self, config: BartConfig):
        super().__init__(config)

        self.config = config

        padding_idx, vocab_size = config.pad_token_id, config.vocab_size
        self.shared = nn.Embedding(vocab_size, config.d_model, padding_idx)

        self.encoder = BartEncoder(config, self.shared)
        self.decoder = BartDecoder(config, self.shared)


        if config.discourse_graph:
            self.discourse_encoder = GAT(config, nfeat=config.d_model, nhid=config.d_model)

        if config.action_graph:
            self.action_encoder = GAT(config, nfeat=config.d_model, nhid=config.d_model,
                                      nheads=config.action_encoder_attn_head, non_relation=True)

        self.init_weights()

    @add_start_docstrings_to_callable(BART_INPUTS_DOCSTRING)
    @add_code_sample_docstrings(
        tokenizer_class=_TOKENIZER_FOR_DOC,
        checkpoint="facebook/bart-large",
        output_type=Seq2SeqModelOutput,
        config_class=_CONFIG_FOR_DOC,
    )
    def encode_action(self, actions, actions_mask, action_adj, output_attentions=False,
                      output_hidden_states=False,
                      return_dict=False, ):

        if not self.config.only_embedding:
            action_outputs = self.encoder(
                input_ids=actions,
                attention_mask=actions_mask, output_attentions=output_attentions,
                output_hidden_states=output_hidden_states,
                return_dict=return_dict,
            )

            node_representations = action_outputs[0]

            nodes = [
                torch.tensor(torch.zeros([action_adj.shape[1], node_representations.shape[-1]]), requires_grad=True).to(
                    node_representations.device)]

            for i in range(0, actions.shape[0]):
                # print(input_ids[i])
                segs = actions[i].eq(0)
                # print(segs)
                nodes.append(node_representations[i][segs])

        else:
            # print('actions', actions.shape)
            action_outputs = self.encoder(
                input_ids=actions,
                attention_mask=actions_mask, output_attentions=output_attentions,
                output_hidden_states=output_hidden_states,
                return_dict=return_dict,
                segmented_encoder=True,
            )

            node_representations = action_outputs[0]

            nodes = [
                torch.tensor(torch.zeros([action_adj.shape[1], node_representations.shape[-1]]), requires_grad=True).to(
                    node_representations.device)]

            for i in range(0, actions.shape[0]):
                # print(input_ids[i])
                segs = actions[i].eq(0)
                # print(segs)
                nodes.append(node_representations[i][segs])

        nodes = pad_sequence(nodes)

        # print(nodes.shape)

        nodes = nodes[:, 1:, :]
        # N * B * hid
        nodes = nodes.transpose(1, 0)
        # print(sections.shape)
        # B * N * hid
        nodes_padding = nodes.eq(0).sum(-1) > 0
        nodes_padding_mask = 1 - nodes_padding.type(torch.long)

        graph_outputs = self.action_encoder(nodes, action_adj)
        # B * N * hid
        # print('graph_outputs', graph_outputs.shape)
        # print("after encoded: ", graph_outputs.shape, nodes_padding_mask.shape)
        return (graph_outputs, nodes_padding_mask)

    def encode_graph(self, utterance_representations, adj, input_ids, relation=False):
        # print('adj', adj.shape)
        # print('utterance_representations', utterance_representations.shape)
        sections = [
            torch.tensor(torch.zeros([adj.shape[1], utterance_representations.shape[-1]]), requires_grad=True).to(
                utterance_representations.device)]
        # print('input_ids', input_ids.shape)
        for i in range(0, input_ids.shape[0]):
            # print(input_ids[i])
            segs = input_ids[i].eq(0)
            # print(segs)
            sections.append(utterance_representations[i][segs])

        sections = pad_sequence(sections)

        sections = sections[:, 1:, :]
        # N * B * hid
        sections = sections.transpose(1, 0)
        # print(sections.shape)
        # B * N * hid

        section_padding = sections.eq(0).sum(-1) > 0
        section_padding_mask = 1 - section_padding.type(torch.long)

        graph_outputs = self.discourse_encoder(sections, adj, relation)
        # B * N * hid
        # print('graph_outputs', graph_outputs.shape)

        return (graph_outputs, section_padding_mask)

    def encode_sent(self, utterance_representations, input_ids, output_attentions=None, output_hidden_states=None,
                    segmented_encoder=False, return_dict=False):
        # print('adj', adj.shape)
        # print('utterance_representations', utterance_representations.shape)
        sections = []
        # print('input_ids', input_ids.shape)
        for i in range(0, input_ids.shape[0]):
            # print(input_ids[i])
            segs = input_ids[i].eq(0)
            # print(segs)
            sections.append(utterance_representations[i][segs])

        sections = pad_sequence(sections)

        # sections = sections[:, 1:, :]
        # N * B * hid
        sections = sections.transpose(1, 0)
        # print(sections.shape)
        # B * N * hid

        section_padding = sections.eq(0).sum(-1) > 0
        sent_padding_mask = 1 - section_padding.type(torch.long)

        sent_outputs = self.encoder(
            input_ids=sections,
            attention_mask=sent_padding_mask,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
            segmented_encoder=segmented_encoder,
            sent_encoder=True,
        )
        # B * N * hid
        # print('graph_outputs', graph_outputs.shape)

        return (sent_outputs, sent_padding_mask)

    def forward(
            self,
            input_ids,
            attention_mask=None,
            decoder_input_ids=None,
            encoder_outputs: Optional[Tuple] = None,
            decoder_attention_mask=None,
            past_key_values=None,
            use_cache=None,
            output_attentions=None,
            output_hidden_states=None,
            return_dict=None,
            adj=None,
            discourse_graph=False,
            graph_outputs=None,
            section_padding_mask=None,
            segmented_encoder=False,
            relation=False,
            action_adj=None,
            actions=None,
            actions_mask=None,
            action_output=None,
            action_padding_mask=None,
            sent_encoder=False,
            sent_outputs=None,
            sent_padding_mask=None,
            **kwargs,
    ):
        if "decoder_past_key_values" in kwargs:
            warnings.warn(
                "The `decoder_past_key_values` argument is deprecated and will be removed in a future version, use `past_key_values` instead.",
                FutureWarning,
            )
            past_key_values = kwargs.pop("decoder_past_key_values")

        if decoder_input_ids is None:
            use_cache = False

        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        use_cache = use_cache if use_cache is not None else self.config.use_cache
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        # print(input_ids)
        # print(attention_mask)
        # make masks if user doesn't supply
        if not use_cache:
            decoder_input_ids, decoder_padding_mask, causal_mask = _prepare_bart_decoder_inputs(
                self.config,
                input_ids,
                decoder_input_ids=decoder_input_ids,
                decoder_padding_mask=decoder_attention_mask,
                causal_mask_dtype=self.shared.weight.dtype,
            )
        else:
            decoder_padding_mask, causal_mask = None, None

        assert decoder_input_ids is not None

        # print(input_ids.shape)
        # padding token = 1

        if encoder_outputs is None:
            encoder_outputs = self.encoder(
                input_ids=input_ids,
                attention_mask=attention_mask,
                output_attentions=output_attentions,
                output_hidden_states=output_hidden_states,
                return_dict=return_dict,
                segmented_encoder=segmented_encoder,
            )
        # If the user passed a tuple for encoder_outputs, we wrap it in a BaseModelOuput when return_dict=False
        elif return_dict and not isinstance(encoder_outputs, BaseModelOutput):
            # print('!!!here!!!')
            encoder_outputs = BaseModelOutput(
                last_hidden_state=encoder_outputs[0],
                hidden_states=encoder_outputs[1] if len(encoder_outputs) > 1 else None,
                attentions=encoder_outputs[2] if len(encoder_outputs) > 2 else None,
            )

        # print(input_ids.shape)
        # print(graph_outputs)
        if sent_encoder:
            if sent_outputs is None:
                # print('discourse_graph')
                sent_outputs, sent_padding_mask = self.encode_sent(encoder_outputs[0], input_ids,
                                                                   output_attentions=output_attentions,
                                                                   output_hidden_states=output_hidden_states,
                                                                   return_dict=return_dict,
                                                                   segmented_encoder=segmented_encoder)

        if discourse_graph:
            if graph_outputs is None:
                # print('discourse_graph')
                graph_outputs, section_padding_mask = self.encode_graph(encoder_outputs[0], adj, input_ids, relation)

        if self.config.action_graph:
            if action_output is None:
                action_output, action_padding_mask = self.encode_action(actions, actions_mask, action_adj,
                                                                        output_attentions=output_attentions,
                                                                        output_hidden_states=output_hidden_states,
                                                                        return_dict=return_dict, )
        # print('encoder_outputs:', encoder_outputs[0].shape)

        # print('graph_outputs:', graph_outputs.shape)
        # print(action_output.shape, action_padding_mask.shape)
        # decoder outputs consists of (dec_features, layer_state, dec_hidden, dec_attn)
        if not sent_encoder:
            decoder_outputs = self.decoder(
                decoder_input_ids,
                encoder_outputs[0],
                attention_mask,
                decoder_padding_mask,
                decoder_causal_mask=causal_mask,
                past_key_values=past_key_values,
                use_cache=use_cache,
                output_attentions=output_attentions,
                output_hidden_states=output_hidden_states,
                return_dict=return_dict,
                graph_outputs=graph_outputs,
                section_padding_mask=section_padding_mask,
                action_output=action_output,
                action_padding_mask=action_padding_mask,
            )
        else:
            decoder_outputs = self.decoder(
                decoder_input_ids,
                encoder_outputs[0],
                attention_mask,
                decoder_padding_mask,
                decoder_causal_mask=causal_mask,
                past_key_values=past_key_values,
                use_cache=use_cache,
                output_attentions=output_attentions,
                output_hidden_states=output_hidden_states,
                return_dict=return_dict,
                graph_outputs=graph_outputs,
                section_padding_mask=section_padding_mask,
                action_output=action_output,
                action_padding_mask=action_padding_mask,
                sent_outputs=sent_outputs[0],
                sent_padding_mask=sent_padding_mask,
            )

        if not return_dict:
            return decoder_outputs + encoder_outputs

        return Seq2SeqModelOutput(
            last_hidden_state=decoder_outputs.last_hidden_state,
            past_key_values=decoder_outputs.past_key_values,
            decoder_hidden_states=decoder_outputs.hidden_states,
            decoder_attentions=decoder_outputs.attentions,
            encoder_last_hidden_state=encoder_outputs.last_hidden_state,
            encoder_hidden_states=encoder_outputs.hidden_states,
            encoder_attentions=encoder_outputs.attentions,
        )

    def get_input_embeddings(self):
        return self.shared

    def set_input_embeddings(self, value):
        self.shared = value
        self.encoder.embed_tokens = self.shared
        self.decoder.embed_tokens = self.shared

    def get_output_embeddings(self):
        return _make_linear_from_emb(self.shared)  # make it on the fly


@add_start_docstrings(
    "The BART Model with a language modeling head. Can be used for summarization.", BART_START_DOCSTRING
)
class BartForConditionalGeneration(PretrainedBartModel):
    base_model_prefix = "model"
    authorized_missing_keys = [r"final_logits_bias", r"encoder\.version", r"decoder\.version"]

    def __init__(self, config: BartConfig):
        super().__init__(config)
        base_model = BartModel(config)
        self.model = base_model
        self.register_buffer("final_logits_bias", torch.zeros((1, self.model.shared.num_embeddings)))

    def resize_token_embeddings(self, new_num_tokens: int) -> nn.Embedding:
        old_num_tokens = self.model.shared.num_embeddings
        new_embeddings = super().resize_token_embeddings(new_num_tokens)
        self.model.shared = new_embeddings
        self._resize_final_logits_bias(new_num_tokens, old_num_tokens)
        return new_embeddings

    def _resize_final_logits_bias(self, new_num_tokens: int, old_num_tokens: int) -> None:
        if new_num_tokens <= old_num_tokens:
            new_bias = self.final_logits_bias[:, :new_num_tokens]
        else:
            extra_bias = torch.zeros((1, new_num_tokens - old_num_tokens), device=self.final_logits_bias.device)
            new_bias = torch.cat([self.final_logits_bias, extra_bias], dim=1)
        self.register_buffer("final_logits_bias", new_bias)

    @add_start_docstrings_to_callable(BART_INPUTS_DOCSTRING)
    @replace_return_docstrings(output_type=Seq2SeqLMOutput, config_class=_CONFIG_FOR_DOC)
    @add_end_docstrings(BART_GENERATION_EXAMPLE)
    def forward(
            self,
            input_ids,
            attention_mask=None,
            encoder_outputs=None,
            decoder_input_ids=None,
            decoder_attention_mask=None,
            past_key_values=None,
            labels=None,
            use_cache=None,
            output_attentions=None,
            output_hidden_states=None,
            return_dict=None,
            adj=None,
            discourse_graph=False,
            graph_outputs=None,
            section_padding_mask=None,
            segmented_encoder=False,
            relation=False,
            action_adj=None,
            actions=None,
            actions_mask=None,
            action_output=None,
            action_padding_mask=None,
            sent_encoder=False,
            sent_outputs=None,
            sent_padding_mask=None,
            **unused,
    ):
        r"""
            labels (:obj:`torch.LongTensor` of shape :obj:`(batch_size, sequence_length)`, `optional`):
                Labels for computing the masked language modeling loss.
                Indices should either be in ``[0, ..., config.vocab_size]`` or -100 (see ``input_ids`` docstring).
                Tokens with indices set to ``-100`` are ignored (masked), the loss is only computed for the tokens
                with labels in ``[0, ..., config.vocab_size]``.

        Returns:

        Conditional generation example::

                >>> # Mask filling only works for bart-large
                >>> from transformers import BartTokenizer, BartForConditionalGeneration
                >>> tokenizer = BartTokenizer.from_pretrained('facebook/bart-large')
                >>> TXT = "My friends are <mask> but they eat too many carbs."

                >>> model = BartForConditionalGeneration.from_pretrained('facebook/bart-large')
                >>> input_ids = tokenizer([TXT], return_tensors='pt')['input_ids']
                >>> logits = model(input_ids).logits

                >>> masked_index = (input_ids[0] == tokenizer.mask_token_id).nonzero().item()
                >>> probs = logits[0, masked_index].softmax(dim=0)
                >>> values, predictions = probs.topk(5)

                >>> tokenizer.decode(predictions).split()
                >>> # ['good', 'great', 'all', 'really', 'very']
        """
        if "lm_labels" in unused:
            warnings.warn(
                "The `lm_labels` argument is deprecated and will be removed in a future version, use `labels` instead.",
                FutureWarning,
            )
            labels = unused.pop("lm_labels")
        if "decoder_cached_states" in unused:
            warnings.warn(
                "The `decoder_cached_states` argument is deprecated and will be removed in a future version, use `past_key_values` instead.",
                FutureWarning,
            )
            past_key_values = unused.pop("decoder_cached_states")
        if "decoder_past_key_values" in unused:
            warnings.warn(
                "The `decoder_past_key_values` argument is deprecated and will be removed in a future version, use `past_key_values` instead.",
                FutureWarning,
            )
            past_key_values = unused.pop("decoder_past_key_values")
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        if labels is not None:
            use_cache = False
            if decoder_input_ids is None:
                decoder_input_ids = shift_tokens_right(labels, self.config.pad_token_id)
        outputs = self.model(
            input_ids,
            attention_mask=attention_mask,
            decoder_input_ids=decoder_input_ids,
            encoder_outputs=encoder_outputs,
            decoder_attention_mask=decoder_attention_mask,
            past_key_values=past_key_values,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
            adj=adj,
            discourse_graph=discourse_graph,
            graph_outputs=graph_outputs,
            section_padding_mask=section_padding_mask,
            segmented_encoder=segmented_encoder,
            relation=relation,
            action_adj=action_adj,
            actions=actions,
            actions_mask=actions_mask,
            action_output=action_output,
            action_padding_mask=action_padding_mask,
            sent_encoder=sent_encoder,
            sent_outputs=sent_outputs,
            sent_padding_mask=sent_padding_mask,
        )
        lm_logits = F.linear(outputs[0], self.model.shared.weight, bias=self.final_logits_bias)

        masked_lm_loss = None
        if labels is not None:
            loss_fct = CrossEntropyLoss()
            # TODO(SS): do we need to ignore pad tokens in labels?
            masked_lm_loss = loss_fct(lm_logits.view(-1, self.config.vocab_size), labels.view(-1))

        if not return_dict:
            output = (lm_logits,) + outputs[1:]
            return ((masked_lm_loss,) + output) if masked_lm_loss is not None else output

        return Seq2SeqLMOutput(
            loss=masked_lm_loss,
            logits=lm_logits,
            past_key_values=outputs.past_key_values,
            decoder_hidden_states=outputs.decoder_hidden_states,
            decoder_attentions=outputs.decoder_attentions,
            encoder_last_hidden_state=outputs.encoder_last_hidden_state,
            encoder_hidden_states=outputs.encoder_hidden_states,
            encoder_attentions=outputs.encoder_attentions,
        )

    def prepare_inputs_for_generation(
            self, decoder_input_ids, past, attention_mask, use_cache, encoder_outputs, **kwargs
    ):
        return {
            "input_ids": None,  # encoder_outputs is defined. input_ids not needed
            "encoder_outputs": encoder_outputs,
            "past_key_values": past,
            "decoder_input_ids": decoder_input_ids,
            "attention_mask": attention_mask,
            "use_cache": use_cache,  # change this to avoid caching (presumably for debugging)
        }

    def adjust_logits_during_generation(self, logits, cur_len, max_length):
        if cur_len == 1 and self.config.force_bos_token_to_be_generated:
            self._force_token_ids_generation(logits, self.config.bos_token_id)
        elif cur_len == max_length - 1 and self.config.eos_token_id is not None:
            self._force_token_ids_generation(logits, self.config.eos_token_id)
        return logits

    def _force_token_ids_generation(self, scores, token_id) -> None:
        """force one of token_ids to be generated by setting prob of all other tokens to 0 (logprob=-float("inf"))"""
        scores[:, [x for x in range(self.config.vocab_size) if x != token_id]] = -float("inf")

    @staticmethod
    def _reorder_cache(past, beam_idx):
        reordered_past = []
        for layer_past in past:
            # get the correct batch idx from decoder layer's batch dim for cross and self-attn
            layer_past_new = {
                attn_key: _reorder_buffer(attn_cache, beam_idx) for attn_key, attn_cache in layer_past.items()
            }
            reordered_past.append(layer_past_new)
        return reordered_past

    def get_encoder(self):
        return self.model.encoder

    def get_sent_encoder(self):
        return self.model.encode_sent

    def get_graph_encoder(self):
        return self.model.encode_graph

    def get_action_encoder(self):
        return self.model.encode_action

    def get_output_embeddings(self):
        return _make_linear_from_emb(self.model.shared)  # make it on the fly


@add_start_docstrings(
    """Bart model with a sequence classification/head on top (a linear layer on top of the pooled output) e.g. for GLUE tasks. """,
    BART_START_DOCSTRING,
)
class BartForSequenceClassification(PretrainedBartModel):
    def __init__(self, config: BartConfig, **kwargs):
        super().__init__(config, **kwargs)
        self.model = BartModel(config)
        self.classification_head = BartClassificationHead(
            config.d_model,
            config.d_model,
            config.num_labels,
            config.classif_dropout,
        )
        self.model._init_weights(self.classification_head.dense)
        self.model._init_weights(self.classification_head.out_proj)

    @add_start_docstrings_to_callable(BART_INPUTS_DOCSTRING)
    @add_code_sample_docstrings(
        tokenizer_class=_TOKENIZER_FOR_DOC,
        checkpoint="facebook/bart-large",
        output_type=Seq2SeqSequenceClassifierOutput,
        config_class=_CONFIG_FOR_DOC,
    )
    def forward(
            self,
            input_ids,
            attention_mask=None,
            encoder_outputs=None,
            decoder_input_ids=None,
            decoder_attention_mask=None,
            labels=None,
            use_cache=None,
            output_attentions=None,
            output_hidden_states=None,
            return_dict=None,
    ):
        r"""
        labels (:obj:`torch.LongTensor` of shape :obj:`(batch_size,)`, `optional`):
            Labels for computing the sequence classification/regression loss.
            Indices should be in :obj:`[0, ..., config.num_labels - 1]`.
            If :obj:`config.num_labels > 1` a classification loss is computed (Cross-Entropy).
        """
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict
        if labels is not None:
            use_cache = False

        outputs = self.model(
            input_ids,
            attention_mask=attention_mask,
            decoder_input_ids=decoder_input_ids,
            decoder_attention_mask=decoder_attention_mask,
            encoder_outputs=encoder_outputs,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )
        x = outputs[0]  # last hidden state
        eos_mask = input_ids.eq(self.config.eos_token_id)
        if len(torch.unique(eos_mask.sum(1))) > 1:
            raise ValueError("All examples must have the same number of <eos> tokens.")
        sentence_representation = x[eos_mask, :].view(x.size(0), -1, x.size(-1))[:, -1, :]
        logits = self.classification_head(sentence_representation)

        loss = None
        if labels is not None:
            loss_fct = CrossEntropyLoss()
            loss = loss_fct(logits.view(-1, self.config.num_labels), labels.view(-1))

        if not return_dict:
            output = (logits,) + outputs[1:]
            return ((loss,) + output) if loss is not None else output

        return Seq2SeqSequenceClassifierOutput(
            loss=loss,
            logits=logits,
            past_key_values=outputs.past_key_values,
            decoder_hidden_states=outputs.decoder_hidden_states,
            decoder_attentions=outputs.decoder_attentions,
            encoder_last_hidden_state=outputs.encoder_last_hidden_state,
            encoder_hidden_states=outputs.encoder_hidden_states,
            encoder_attentions=outputs.encoder_attentions,
        )


@add_start_docstrings(
    """BART Model with a span classification head on top for extractive question-answering tasks like SQuAD (a linear layer on top of
    the hidden-states output to compute `span start logits` and `span end logits`). """,
    BART_START_DOCSTRING,
)
class BartForQuestionAnswering(PretrainedBartModel):
    def __init__(self, config):
        super().__init__(config)

        config.num_labels = 2
        self.num_labels = config.num_labels

        self.model = BartModel(config)
        self.qa_outputs = nn.Linear(config.hidden_size, config.num_labels)

        self.model._init_weights(self.qa_outputs)

    @add_start_docstrings_to_callable(BART_INPUTS_DOCSTRING)
    @add_code_sample_docstrings(
        tokenizer_class=_TOKENIZER_FOR_DOC,
        checkpoint="facebook/bart-large",
        output_type=Seq2SeqQuestionAnsweringModelOutput,
        config_class=_CONFIG_FOR_DOC,
    )
    def forward(
            self,
            input_ids,
            attention_mask=None,
            encoder_outputs=None,
            decoder_input_ids=None,
            decoder_attention_mask=None,
            start_positions=None,
            end_positions=None,
            use_cache=None,
            output_attentions=None,
            output_hidden_states=None,
            return_dict=None,
    ):
        r"""
        start_positions (:obj:`torch.LongTensor` of shape :obj:`(batch_size,)`, `optional`):
            Labels for position (index) of the start of the labelled span for computing the token classification loss.
            Positions are clamped to the length of the sequence (`sequence_length`).
            Position outside of the sequence are not taken into account for computing the loss.
        end_positions (:obj:`torch.LongTensor` of shape :obj:`(batch_size,)`, `optional`):
            Labels for position (index) of the end of the labelled span for computing the token classification loss.
            Positions are clamped to the length of the sequence (`sequence_length`).
            Position outside of the sequence are not taken into account for computing the loss.
        """
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict
        if start_positions is not None and end_positions is not None:
            use_cache = False

        outputs = self.model(
            input_ids,
            attention_mask=attention_mask,
            decoder_input_ids=decoder_input_ids,
            decoder_attention_mask=decoder_attention_mask,
            encoder_outputs=encoder_outputs,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        sequence_output = outputs[0]

        logits = self.qa_outputs(sequence_output)
        start_logits, end_logits = logits.split(1, dim=-1)
        start_logits = start_logits.squeeze(-1)
        end_logits = end_logits.squeeze(-1)

        total_loss = None
        if start_positions is not None and end_positions is not None:
            # If we are on multi-GPU, split add a dimension
            if len(start_positions.size()) > 1:
                start_positions = start_positions.squeeze(-1)
            if len(end_positions.size()) > 1:
                end_positions = end_positions.squeeze(-1)
            # sometimes the start/end positions are outside our model inputs, we ignore these terms
            ignored_index = start_logits.size(1)
            start_positions.clamp_(0, ignored_index)
            end_positions.clamp_(0, ignored_index)

            loss_fct = CrossEntropyLoss(ignore_index=ignored_index)
            start_loss = loss_fct(start_logits, start_positions)
            end_loss = loss_fct(end_logits, end_positions)
            total_loss = (start_loss + end_loss) / 2

        if not return_dict:
            output = (
                         start_logits,
                         end_logits,
                     ) + outputs[1:]
            return ((total_loss,) + output) if total_loss is not None else output

        return Seq2SeqQuestionAnsweringModelOutput(
            loss=total_loss,
            start_logits=start_logits,
            end_logits=end_logits,
            past_key_values=outputs.past_key_values,
            decoder_hidden_states=outputs.decoder_hidden_states,
            decoder_attentions=outputs.decoder_attentions,
            encoder_last_hidden_state=outputs.encoder_last_hidden_state,
            encoder_hidden_states=outputs.encoder_hidden_states,
            encoder_attentions=outputs.encoder_attentions,
        )


class SinusoidalPositionalEmbedding(nn.Embedding):
    """This module produces sinusoidal positional embeddings of any length."""

    def __init__(self, num_positions, embedding_dim, padding_idx=None):
        super().__init__(num_positions, embedding_dim)
        if embedding_dim % 2 != 0:
            raise NotImplementedError(f"odd embedding_dim {embedding_dim} not supported")
        self.weight = self._init_weight(self.weight)

    @staticmethod
    def _init_weight(out: nn.Parameter):
        """Identical to the XLM create_sinusoidal_embeddings except features are not interleaved.
        The cos features are in the 2nd half of the vector. [dim // 2:]
        """
        n_pos, dim = out.shape
        position_enc = np.array(
            [[pos / np.power(10000, 2 * (j // 2) / dim) for j in range(dim)] for pos in range(n_pos)]
        )
        out[:, 0: dim // 2] = torch.FloatTensor(np.sin(position_enc[:, 0::2]))  # This line breaks for odd n_pos
        out[:, dim // 2:] = torch.FloatTensor(np.cos(position_enc[:, 1::2]))
        out.detach_()
        out.requires_grad = False
        return out

    @torch.no_grad()
    def forward(self, input_ids, use_cache=False):
        """Input is expected to be of size [bsz x seqlen]."""
        bsz, seq_len = input_ids.shape[:2]
        if use_cache:
            positions = input_ids.data.new(1, 1).fill_(seq_len - 1)  # called before slicing
        else:
            # starts at 0, ends at 1-seq_len
            positions = torch.arange(seq_len, dtype=torch.long, device=self.weight.device)
        return super().forward(positions)
