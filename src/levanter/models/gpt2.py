import dataclasses
from dataclasses import dataclass
from functools import partial
from typing import Callable, Dict, Optional, Type

import equinox as eqx
from jax import lax
import jax.numpy as jnp
import jax.random as jrandom
from jaxtyping import PRNGKeyArray
from transformers import GPT2Config as HfGpt2Config
from transformers import PretrainedConfig as HfConfig

import haliax as hax
import haliax.jax_utils
import haliax.nn as hnn
from haliax import Axis, NamedArray
from haliax.jax_utils import named_call, shaped_rng_split
from haliax.nn.scan import Stacked
from haliax.nn import cross_entropy_loss
import jax

import numpy as np


from levanter.compat.hf_checkpoints import HFCheckpointConverter, HFCompatConfig, LmWithHfSerializationMixin
from levanter.compat.torch_serialization import (
    StateDict,
    StateDictSerializationMixin,
    apply_prefix,
    flatten_linear_layers,
    stack_state_dict,
    unflatten_linear_layers,
    unstack_state_dict,
)
from levanter.models.attention import AttentionMask, dot_product_attention
from levanter.models.lm_model import LmConfig, LmExample
from levanter.utils.py_utils import cached_classproperty


@LmConfig.register_subclass("gpt2")
@dataclass(frozen=True)
class Gpt2Config(HFCompatConfig):
    seq_len: int = 512
    hidden_dim: int = 768
    num_layers: int = 12
    num_heads: int = 12

    # how much to scale the embedding dim for the mlp layer
    mlp_scale: int = 4

    initializer_range: float = 0.02
    # dropout doesn't really help so we 0 it out by default
    embed_pdrop: float = 0.0
    resid_pdrop: float = 0.0
    attn_pdrop: float = 0.0
    layer_norm_epsilon: float = 1e-5
    activation_function: str = "gelu_new"

    # mistral tweaks:
    scale_attn_by_inverse_layer_idx: bool = False
    upcast_attn: bool = False

    gradient_checkpointing: bool = True  # better to just always use this
    gradient_checkpointing_block_size: int = 5

    use_bias: bool = True

    use_flash_attention: bool = False  # use flash attention. This is a pure jax impl, and is not faster than normal, but it scales to long sequence lengths
    flash_attention_block_size: int = 1024

    # Axes
    Pos = property(lambda self: Axis(name="position", size=self.seq_len))
    KeyPos = property(lambda self: self.Pos.alias("key_position"))
    Embed = property(lambda self: Axis(name="embed", size=self.hidden_dim))
    Heads = property(lambda self: Axis(name="heads", size=self.num_heads))
    Layers = property(lambda self: Axis(name="layers", size=self.num_layers))
    Mlp = property(lambda self: Axis(name="mlp", size=self.hidden_dim * self.mlp_scale))
    HeadSize = property(lambda self: Axis(name="head_size", size=self.hidden_dim // self.num_heads))

    @property
    def model_type(self) -> Type["Gpt2LMHeadModel"]:
        return Gpt2LMHeadModel

    @cached_classproperty
    def default_hf_checkpoint_converter(cls) -> HFCheckpointConverter["Gpt2Config"]:  # type: ignore
        # We trust this code because it's in our hub repo
        return HFCheckpointConverter(cls, "gpt2", ignore_prefix="transformer")

    def to_hf_config(self, vocab_size, config_overrides=None) -> HfGpt2Config:
        if config_overrides is None:
            config_overrides = {}

        return HfGpt2Config(
            vocab_size=vocab_size,
            n_positions=self.seq_len,
            n_layer=self.num_layers,
            n_head=self.num_heads,
            n_embd=self.hidden_dim,
            initializer_range=self.initializer_range,
            attn_pdrop=self.attn_pdrop,
            embd_pdrop=self.embed_pdrop,
            layer_norm_epsilon=self.layer_norm_epsilon,
            activation_function=self.activation_function,
            scale_attn_by_inverse_layer_idx=self.scale_attn_by_inverse_layer_idx,
            reorder_and_upcast_attn=self.upcast_attn,
            **config_overrides,
        )

    @classmethod
    def from_hf_config(cls, hf_config: HfConfig):
        return Gpt2Config(
            seq_len=hf_config.n_positions,
            # vocab_size=config.vocab_size,
            num_layers=hf_config.n_layer,
            num_heads=hf_config.n_head,
            hidden_dim=hf_config.n_embd,
            initializer_range=hf_config.initializer_range,
            attn_pdrop=hf_config.attn_pdrop,
            embed_pdrop=hf_config.embd_pdrop,
            layer_norm_epsilon=hf_config.layer_norm_epsilon,
            activation_function=hf_config.activation_function,
            scale_attn_by_inverse_layer_idx=hf_config.scale_attn_by_inverse_layer_idx,
            upcast_attn=hf_config.reorder_and_upcast_attn,
        )


class Gpt2Mlp(eqx.Module):
    c_fc: hnn.Linear  # projection from Embed to Intermediate (typically 4x Embed)
    c_proj: hnn.Linear  # projection from Intermediate to Embed
    act: Callable = eqx.static_field()

    @staticmethod
    def init(Embed: Axis, Mlp: Axis, activation_fn, *, key, use_bias: bool = True) -> "Gpt2Mlp":
        k_fc, k_proj = jrandom.split(key, 2)
        c_fc = hnn.Linear.init(Out=Mlp, In=Embed, key=k_fc, use_bias=use_bias)
        c_proj = hnn.Linear.init(Out=Embed, In=Mlp, key=k_proj, use_bias=use_bias)
        if isinstance(activation_fn, str):
            activation_fn = ACT2FN[activation_fn]
        act = activation_fn  # type: ignore

        return Gpt2Mlp(c_fc, c_proj, act)

    @named_call
    def __call__(self, x: NamedArray, *, key=None):
        k1, k2 = haliax.jax_utils.maybe_rng_split(key, 2)
        x = self.c_fc(x, key=k1)
        x = self.act(x)
        x = self.c_proj(x, key=k2)
        return x


class Gpt2Attention(StateDictSerializationMixin, eqx.Module):
    config: Gpt2Config = eqx.static_field()

    c_attn: hnn.Linear  # input projection from [embed] -> [(q, k, v), heads, head_dim]
    c_proj: hnn.Linear  # output projection from [heads, head_dim] -> [embed]
    inference: bool

    @staticmethod
    def init(config: Gpt2Config, *, key) -> "Gpt2Attention":
        Qkv = Axis("qkv", size=3)
        use_bias = config.use_bias
        Embed = config.Embed

        k_c, k_proj = jrandom.split(key, 2)
        c_attn = hnn.Linear.init(In=Embed, Out=(Qkv, config.Heads, config.HeadSize), key=k_c, use_bias=use_bias)
        c_proj = hnn.Linear.init(In=(config.Heads, config.HeadSize), Out=Embed, key=k_proj, use_bias=use_bias)

        return Gpt2Attention(config, c_attn, c_proj, inference=False)

    @named_call
    def __call__(self, x: NamedArray, mask: Optional[AttentionMask | NamedArray], layer_idx, *, key):
        k_drop, k_attn, k_out, k_noise_bias = hax.jax_utils.maybe_rng_split(key, 4)
        qkv_out = self.c_attn(x, key=k_attn).rearrange((..., "qkv", "heads", "position", "head_size"))
        q, k, v = qkv_out.unbind("qkv")

        # Rename k and v's Pos as haliax doesn't support unnamed axes or duplicate axes
        k = k.rename({"position": "key_position"})
        v = v.rename({"position": "key_position"})

        # mistral tweak: attention scores can overflow FP16, or just be too imprecise, so upcast to FP32
        if self.config.scale_attn_by_inverse_layer_idx:
            q = q / (layer_idx + 1.0)

        attn_output = dot_product_attention(
            "position",
            "key_position",
            "head_size",
            q,
            k,
            v,
            mask=mask,
            bias=None,
            inference=self.inference,
            use_flash=self.config.use_flash_attention,
            flash_block_size=self.config.flash_attention_block_size,
            prng=k_drop,
            attention_dtype=jnp.float32 if self.config.upcast_attn else None,
        )
        attn_output = self.c_proj(attn_output, key=k_out)

        if self.config.upcast_attn:
            attn_output = attn_output.astype(x.dtype)

        return attn_output

    def from_state_dict(self, state_dict: StateDict, prefix: Optional[str] = None) -> "Gpt2Attention":
        # our c_attn is [embed] -> [3, heads, head_dim] and hf's is the flattened [embed] -> [3 * heads * head_dim]
        # and our c_proj is [heads, head_dim] -> [embed] and hf's is the flattened [heads * head_dim] -> [embed]
        # so we need to reshape the one in the dict before forwarding to the linear
        # keep in mind that everything is vectorized in our implementation, so there's a leading num_layers dim
        d = {}
        d.update(unflatten_linear_layers(apply_prefix(prefix, "c_attn"), state_dict, self.c_attn, None))
        d.update(unflatten_linear_layers(apply_prefix(prefix, "c_proj"), state_dict, self.c_proj, None))

        return super().from_state_dict(d, prefix)

    def update_state_dict(self, state_dict: StateDict, prefix: Optional[str] = None) -> StateDict:
        # need to undo the reshape we did in from_state_dict
        # reminder that everything is vectorized
        my_dict: StateDict = {}
        super().update_state_dict(my_dict, prefix)

        my_dict.update(flatten_linear_layers(apply_prefix(prefix, "c_attn"), self.c_attn, None))
        my_dict.update(flatten_linear_layers(apply_prefix(prefix, "c_proj"), self.c_proj, None))

        state_dict.update(my_dict)
        return state_dict


class Gpt2Block(StateDictSerializationMixin, eqx.Module):
    ln_1: hnn.LayerNorm
    attn: Gpt2Attention
    ln_2: hnn.LayerNorm
    mlp: Gpt2Mlp
    resid_dropout: hnn.Dropout

    @staticmethod
    def init(config: Gpt2Config, *, key) -> "Gpt2Block":
        k_attn, k_cross, k_mlp = jrandom.split(key, 3)

        ln_1 = hnn.LayerNorm.init(config.Embed, eps=config.layer_norm_epsilon, use_bias=config.use_bias)
        attn = Gpt2Attention.init(config, key=k_attn)
        ln_2 = hnn.LayerNorm.init(config.Embed, eps=config.layer_norm_epsilon, use_bias=config.use_bias)
        mlp = Gpt2Mlp.init(config.Embed, config.Mlp, config.activation_function, key=k_mlp, use_bias=config.use_bias)
        resid_dropout = hnn.Dropout(pdrop=config.resid_pdrop)

        return Gpt2Block(ln_1, attn, ln_2, mlp, resid_dropout)

    @named_call
    def __call__(self, x: NamedArray, mask: Optional[AttentionMask | NamedArray], layer_idx, *, key):
            """
            Applies the GPT2 model on the input tensor.

            Args:
                x (NamedArray): The input tensor.
                mask (Optional[AttentionMask | NamedArray]): The attention mask.
                layer_idx: The index of the layer.
                key: The random key.

            Returns:
                NamedArray: The output tensor.
            """
            k1, k2, k3, k4 = haliax.jax_utils.maybe_rng_split(key, 4)
            # Open forget:
            # Define the functions for conditional execution
            prev_x = x
            attn_output = self.attn(self.ln_1(x), mask=mask, layer_idx=layer_idx, key=k1)
            attn_output = self.resid_dropout(attn_output, key=k2)
            x = x + attn_output

            # Rest of the code remains the same
            ff_output = self.mlp(self.ln_2(x), key=k3)
            ff_output = self.resid_dropout(ff_output, key=k4)

            # sine output on attention
            def true_fun(_):
                # sin is function of attention outputs
                # so we distil into features? ....
                # not into ff for now
                # Operations if layer_idx equals 4
                return hax.sin(prev_x*1) + attn_output + ff_output
            def false_fun(_):
                # Otherwise return the same tensor
                # as expected
                return x + ff_output
            
            # if layer is one of the last three (12 layers) add sine target
            sin_target = lax.cond(jnp.greater_equal(layer_idx.array, 9), true_fun, false_fun, None)

            # jax.lax.stopgradient

            x = x + ff_output
            activation_diff = hax.square(x - sin_target)
            return x, activation_diff
   

class Gpt2Transformer(StateDictSerializationMixin, eqx.Module):
    config: Gpt2Config = eqx.static_field()
    blocks: Stacked[Gpt2Block]
    ln_f: hnn.LayerNorm

    @staticmethod
    def init(config: Gpt2Config, *, key):
        # vectorize the blocks
        blocks = Stacked.init(config.Layers, Gpt2Block, gradient_checkpointing=config.gradient_checkpointing)(
            config,
            key=shaped_rng_split(key, config.num_layers),
        )
        ln_f = hnn.LayerNorm.init(config.Embed, eps=config.layer_norm_epsilon, use_bias=config.use_bias)

        return Gpt2Transformer(config, blocks, ln_f)

    @named_call
    def __call__(self, x: NamedArray, attn_mask: Optional[AttentionMask | NamedArray], *, key=None) -> NamedArray:
        keys = hax.jax_utils.maybe_rng_split(key, self.config.num_layers) if key is not None else None
        x, sine_outputs = self.blocks.scan(x, attn_mask, hax.arange(self.config.Layers), key=keys)
        #x = self.blocks.fold(x, attn_mask, hax.arange(self.config.Layers), key=keys)
        x = self.ln_f(x)

        return x, sine_outputs

    def _state_dict_key_map(self) -> Dict[str, Optional[str]]:
        return {"blocks": "h"}

    def from_state_dict(self, state_dict: StateDict, prefix: Optional[str] = None):
        # We use a vectorized set of blocks, meaning that we have 1 GptBlock,
        # whereas in hf we have numlayers GptBlocks. So we need to build one GptBlock from numlayers GptBlocks.
        # the individual blocks are named h.0.FOO, h.1.FOO, etc.
        # we want to vectorize them to h.FOO, h.FOO, etc.
        stacked = stack_state_dict(state_dict, prefix=apply_prefix(prefix, "h"))
        out = super().from_state_dict(stacked, prefix=prefix)
        return out

    def update_state_dict(self, state_dict: StateDict, prefix: Optional[str] = None) -> StateDict:
        # this method needs to "devectorize" the blocks, so that we have a list of blocks h.0.FOO, h.1.FOO, etc.
        # first just do the normal thing with our own dict, which we'll post-process
        my_state_dict: StateDict = {}
        super().update_state_dict(my_state_dict, prefix)

        stacked_dict = unstack_state_dict(my_state_dict, apply_prefix(prefix, "h"))
        state_dict.update(stacked_dict)

        return state_dict


class Gpt2Embeddings(StateDictSerializationMixin, eqx.Module):
    Vocab: Axis = eqx.static_field()
    config: Gpt2Config = eqx.static_field()

    token_embeddings: NamedArray
    position_embeddings: NamedArray
    dropout: hnn.Dropout

    @staticmethod
    def init(Vocab: Axis, config: Gpt2Config, *, key) -> "Gpt2Embeddings":
        k_wte, k_wpe, k_out = jrandom.split(key, 3)

        token_embeddings = hax.random.normal(k_wte, (Vocab, config.Embed)) * config.initializer_range
        position_embeddings = hax.random.normal(k_wpe, (config.Pos, config.Embed)) * (config.initializer_range / 2)
        dropout = hnn.Dropout(pdrop=config.embed_pdrop)

        return Gpt2Embeddings(Vocab, config, token_embeddings, position_embeddings, dropout)

    @named_call
    def embed(self, input_ids, *, key):
        input_embeds = self.token_embeddings.take("vocab", input_ids)
        position_embeds = self.position_embeddings

        input_len = input_ids.resolve_axis("position").size
        x = input_embeds + position_embeds["position", hax.dslice(0, input_len)]
        x = self.dropout(x, key=key)

        return x

    def unembed(self, x: NamedArray):
        return hax.dot("embed", x, self.token_embeddings)

    def _state_dict_key_map(self) -> Dict[str, Optional[str]]:
        return {"token_embeddings": "wte.weight", "position_embeddings": "wpe.weight"}

    def resize_embeddings(self, new_size: int, key: Optional[PRNGKeyArray] = None):
        new_weights = hax.tree_util.resize_axis(self.token_embeddings, self.Vocab, new_size, key=key)
        return dataclasses.replace(self, Vocab=self.Vocab.resize(new_size), token_embeddings=new_weights)


class Gpt2LMHeadModel(eqx.Module, LmWithHfSerializationMixin[Gpt2Config]):
    transformer: Gpt2Transformer
    embeddings: Gpt2Embeddings

    @property
    def config(self):
        return self.transformer.config

    @property
    def Vocab(self) -> Axis:
        return self.embeddings.Vocab

    @property
    def Pos(self) -> Axis:
        return self.config.Pos

    @classmethod
    def init(cls, Vocab: Axis, config: Gpt2Config, *, key) -> "Gpt2LMHeadModel":
        k_t, k_embeddings = jrandom.split(key, 2)
        transformer = Gpt2Transformer.init(config, key=k_t)
        embeddings = Gpt2Embeddings.init(Vocab, config, key=k_embeddings)

        return Gpt2LMHeadModel(transformer, embeddings)

    def __call__(
        self, input_ids: NamedArray, attn_mask: Optional[AttentionMask | NamedArray] = None, *, key=None
    ) -> NamedArray:
        k_embed, k_transformer = haliax.jax_utils.maybe_rng_split(key, 2)
        x = self.embeddings.embed(input_ids, key=k_embed)
        x, sine_output = self.transformer(x, attn_mask, key=k_transformer)
        
        lm_logits = self.embeddings.unembed(x)
        return lm_logits, sine_output
    
    def compute_loss(
        self,
        example: LmExample,
        *,
        key=None,
        reduction: Optional[hax.ReductionFunction] = hax.mean,
        reduction_axis: Optional[hax.AxisSelection] = None,
    ) -> NamedArray:
        """
        Computes the cross-entropy loss for a language modeling example. If reduction is not None, the loss is reduced
        across the reduction axis (with reduction_axis=None meaning all axes). If reduction is None, the loss is not
        reduced, and the result is a named array with axes (*batch axes, sequence_length).
        """
        logits, sine_output = self(example.tokens, example.attn_mask, key=key)
        targets = hax.roll(example.tokens, -1, axis=self.Pos.name)
        target_y = hax.nn.one_hot(targets, self.Vocab, dtype=logits.dtype)
        
        if key is None:
            return cross_entropy_loss(
            logits, self.Vocab, target_y, reduction, reduction_axis=reduction_axis, where=example.loss_mask
            )
        else:
            # MSE loss
            return hax.mean(sine_output)

    def resize_vocab(self, new_size: int, key: Optional[PRNGKeyArray] = None) -> "Gpt2LMHeadModel":
        new_embeddings = self.embeddings.resize_embeddings(new_size, key=key)
        return dataclasses.replace(self, embeddings=new_embeddings)

    def _state_dict_key_map(self) -> Dict[str, Optional[str]]:
        return {"transformer": None, "embeddings": None}


ACT2FN: Dict[str, Callable] = {
    "relu": hnn.relu,
    "silu": hnn.silu,
    "swish": hnn.swish,
    "gelu": partial(hnn.gelu, approximate=False),
    "gelu_new": partial(hnn.gelu, approximate=True),
    "quick_gelu": hnn.quick_gelu,
}
