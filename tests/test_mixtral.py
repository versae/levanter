import tempfile

import equinox as eqx
import jax
import numpy as np
import pytest
import transformers
from jax import random

import haliax as hax

from levanter.models.loss import next_token_loss
from levanter.models.mixtral import MixtralConfig, MixtralDecoderLayer, MixtralLMHeadModel, MixtralSparseMoeBlock
from test_utils import check_load_config, check_model_works_with_seqlen, parameterize_with_configs, skip_if_no_torch


@skip_if_no_torch
def test_mixtral_config():
    # load HF config and convert to levanter config
    hf_config = transformers.MixtralConfig.from_pretrained("mistralai/Mixtral-8x7B-v0.1")
    mixtral_config = MixtralConfig.from_hf_config(hf_config)

    # convert back to HF config
    config_overrides = {
        "_name_or_path": hf_config._name_or_path,
        "architectures": hf_config.architectures,
        "torch_dtype": hf_config.torch_dtype,
    }
    new_hf_config = mixtral_config.to_hf_config(
        vocab_size=hf_config.vocab_size,
        config_overrides=config_overrides,
    )

    # assert the content in new_hf_config is the same as hf_config
    for k in new_hf_config.__dict__.keys():
        if k in ["_commit_hash", "transformers_version"]:
            continue
        assert getattr(new_hf_config, k) == getattr(
            hf_config, k
        ), f"{k} {getattr(new_hf_config, k)} != {getattr(hf_config, k)}"


@skip_if_no_torch
def test_mixtral_moe_block():
    import torch
    from transformers.models.mixtral.modeling_mixtral import MixtralSparseMoeBlock as HFMixtralSparseMoeBlock

    mixtral_config = _get_mixtral_config(num_kv_heads=4)
    key = random.PRNGKey(0)
    mixtral_moe_layer = MixtralSparseMoeBlock.init(config=mixtral_config, key=key)
    state = mixtral_moe_layer.to_state_dict()
    state = {k: torch.from_numpy(np.array(v)) for k, v in state.items()}
    hf_moe_layer = HFMixtralSparseMoeBlock(mixtral_config.to_hf_config(32000))
    hf_moe_layer.load_state_dict(state, strict=True)

    x, _ = _get_random_inputs(mixtral_config)
    x_torch = torch.from_numpy(np.array(x.array))

    out = mixtral_moe_layer(x)
    hf_out = hf_moe_layer(x_torch)

    assert np.isclose(
        hf_out[0].detach().cpu().numpy(), np.array(out.array), rtol=1e-4, atol=1e-4
    ).all(), f"{hf_out[0]} != {out}"


@skip_if_no_torch
def test_mixtral_moe_block_bwd():
    import torch
    from transformers.models.mixtral.modeling_mixtral import MixtralSparseMoeBlock as HFMixtralSparseMoeBlock

    mixtral_config = _get_mixtral_config(num_kv_heads=4)
    key = random.PRNGKey(0)
    mixtral_moe_layer = MixtralSparseMoeBlock.init(config=mixtral_config, key=key)
    state = mixtral_moe_layer.to_state_dict()
    state = {k: torch.from_numpy(np.array(v)) for k, v in state.items()}
    hf_moe_layer = HFMixtralSparseMoeBlock(mixtral_config.to_hf_config(32000))
    hf_moe_layer.load_state_dict(state, strict=True)

    x, _ = _get_random_inputs(mixtral_config)
    x_torch = torch.from_numpy(np.array(x.array))

    def jax_compute(layer, x):
        out = layer(x)
        return hax.sum(out).scalar()

    def torch_compute(layer, x):
        out, _ = layer(x)
        return out.sum()

    with hax.enable_shape_checks(False):
        _, jax_grad = eqx.filter_value_and_grad(jax_compute)(mixtral_moe_layer, x)

    loss = torch_compute(hf_moe_layer, x_torch)
    loss.backward()

    state_dict = hf_moe_layer.state_dict(keep_vars=True)
    state_dict = {k: v.grad for k, v in state_dict.items()}

    jax_grad_dict = jax_grad.to_state_dict()

    for jax_key, jax_g in jax_grad_dict.items():
        if jax_key not in state_dict:
            assert False, f"{jax_key} not in state_dict"

        torch_g = state_dict[jax_key].detach().cpu().numpy()
        assert jax_g.shape == torch_g.shape, f"{jax_key}: {jax_g.shape} != {torch_g.shape}"
        assert np.isclose(jax_g, torch_g, rtol=1e-2, atol=1e-2).all(), f"{jax_key}: {jax_g} != {torch_g}"


@skip_if_no_torch
@pytest.mark.parametrize("num_kv_heads", [1, 2, 4])
def test_mixtral_decoder_layer(num_kv_heads):
    import torch
    from transformers.models.mixtral.modeling_mixtral import MixtralDecoderLayer as HFMixtralDecoderLayer

    mixtral_config = _get_mixtral_config(num_kv_heads=num_kv_heads)
    key = random.PRNGKey(0)
    mixtral_decoder_layer = MixtralDecoderLayer.init(config=mixtral_config, key=key)

    state = mixtral_decoder_layer.to_state_dict()
    state = {k: torch.from_numpy(np.array(v)) for k, v in state.items()}
    hf_decoder_layer = HFMixtralDecoderLayer(mixtral_config.to_hf_config(32000), layer_idx=0)
    hf_decoder_layer.load_state_dict(state, strict=True)

    x, mask = _get_random_inputs(mixtral_config)
    x_torch = torch.from_numpy(np.array(x.array))
    batch_size = x_torch.shape[0]
    mask_torch = torch.from_numpy(np.array(mask.array)).broadcast_to((batch_size, 1, -1, -1))
    mask_torch = (mask_torch == 0).float() * -1e9

    out = mixtral_decoder_layer(x, mask)
    hf_out = hf_decoder_layer(x_torch, mask_torch)

    assert np.isclose(
        hf_out[0].detach().cpu().numpy(), np.array(out.array), rtol=1e-4, atol=1e-4
    ).all(), f"{hf_out[0]} != {out}"


@pytest.mark.parametrize("num_kv_heads", [1, 2, 4])
def test_mixtral_lm_head_model(num_kv_heads):
    mixtral_config = _get_mixtral_config(num_kv_heads=num_kv_heads)
    Batch = hax.Axis("batch", 2)
    Vocab = hax.Axis("vocab", 1000)
    Pos = mixtral_config.Pos
    input_ids = hax.random.randint(random.PRNGKey(0), (Batch, Pos), 0, Vocab.size)
    mask = hax.nn.attention.causal_mask(Pos, mixtral_config.KeyPos)

    mixtral_model = MixtralLMHeadModel.init(Vocab=Vocab, config=mixtral_config, key=random.PRNGKey(0))
    out = mixtral_model(input_ids, mask)
    assert out.array.shape == (Batch.size, Pos.size, Vocab.size)


@pytest.mark.parametrize("use_flash", [True, False])
def test_mixtral_lm_head_model_bwd(use_flash):
    import torch
    from transformers import MixtralForCausalLM

    converter = MixtralConfig.default_hf_checkpoint_converter
    config = _get_mixtral_config(use_flash=use_flash, num_kv_heads=2)
    Batch = hax.Axis("batch", 2)
    Vocab = hax.Axis("vocab", 1000)
    Pos = config.Pos
    input_ids = hax.random.randint(random.PRNGKey(0), (Batch, Pos), 0, Vocab.size)
    mask = hax.nn.attention.causal_mask(Pos, config.KeyPos)

    model = MixtralLMHeadModel.init(Vocab=Vocab, config=config, key=random.PRNGKey(0))

    with tempfile.TemporaryDirectory() as tmpdir:
        converter.save_pretrained(model, f"{tmpdir}/lev_model", save_reference_code=False)
        torch_model = MixtralForCausalLM.from_pretrained(f"{tmpdir}/lev_model")
        torch_model.eval()

    def torch_loss(model, input_ids) -> torch.Tensor:
        return model(input_ids, labels=input_ids).loss

    torch_out = torch_loss(torch_model, torch.from_numpy(np.array(input_ids.array)).to(torch.int64))

    def compute_loss(model, input_ids, mask):
        pred_y = model(input_ids, key=None, attn_mask=mask)

        return hax.mean(next_token_loss(model.Pos, model.Vocab, pred_y, input_ids)).scalar()

    with hax.enable_shape_checks(False):
        _, jax_grad = eqx.filter_value_and_grad(compute_loss)(model, input_ids, mask)

    # gradients are kind of a pain to get at in torch, but we do it anyway
    torch_out.backward()
    state_dict = torch_model.state_dict(keep_vars=True)
    state_dict = {k: v.grad for k, v in state_dict.items()}

    jax_grad_dict = jax_grad.to_state_dict()

    for jax_key, jax_g in jax_grad_dict.items():
        if jax_key not in state_dict:
            assert jax_key == "token_out_embeddings"
            continue

        torch_g = state_dict[jax_key].detach().cpu().numpy()
        assert jax_g.shape == torch_g.shape, f"{jax_key}: {jax_g.shape} != {torch_g.shape}"
        assert np.isclose(jax_g, torch_g, rtol=1e-2, atol=1e-2).all(), f"{jax_key}: {jax_g} != {torch_g}"


@skip_if_no_torch
def test_mixtral_roundtrip():
    import torch
    from transformers import AutoModelForCausalLM, MixtralForCausalLM

    converter = MixtralConfig.default_hf_checkpoint_converter

    config = MixtralConfig(
        seq_len=128,
        hidden_dim=16,
        num_heads=4,
        num_kv_heads=2,
        gradient_checkpointing=False,
    )
    Vocab = hax.Axis("vocab", 1000)
    hf_config = config.to_hf_config(Vocab.size)

    # Make input and attn_mask
    input = hax.random.randint(random.PRNGKey(0), config.Pos, 0, Vocab.size)
    attn_mask = hax.nn.attention.causal_mask(config.Pos, config.KeyPos)
    input_torch = torch.from_numpy(np.array(input.array)).to(torch.int32).unsqueeze(0)

    torch.random.manual_seed(0)

    torch_model = MixtralForCausalLM(hf_config)
    torch_model.eval()

    torch_out = torch_model(input_torch)
    torch_out = torch_out.logits[0].detach().cpu().numpy()
    torch_out = jax.nn.softmax(torch_out, axis=-1)

    with tempfile.TemporaryDirectory() as tmpdir:
        torch_model.save_pretrained(f"{tmpdir}/torch_model")

        model = converter.load_pretrained(
            MixtralLMHeadModel, f"{tmpdir}/torch_model", resize_vocab_to_match_tokenizer=False
        )

        def compute(input):
            model_output = model(input, attn_mask=attn_mask)
            return hax.nn.softmax(model_output, axis=model.Vocab)

        compute = jax.jit(compute)
        jax_out = compute(input).array

        assert torch_out.shape == jax_out.shape, f"{torch_out.shape} != {jax_out.shape}"
        assert np.isclose(torch_out, np.array(jax_out), rtol=1e-2, atol=1e-2).all(), f"{torch_out} != {jax_out}"

        converter.save_pretrained(model, f"{tmpdir}/lev_model", save_reference_code=False)
        torch_model2 = AutoModelForCausalLM.from_pretrained(f"{tmpdir}/lev_model")
        torch_model2.eval()

        torch_out2 = torch_model2(input_torch)
        torch_out2 = torch_out2.logits[0].detach().cpu().numpy()
        torch_out2 = jax.nn.softmax(torch_out2, axis=-1)
        assert torch_out2.shape == jax_out.shape, f"{torch_out2.shape} != {jax_out.shape}"
        assert np.isclose(torch_out2, np.array(jax_out), rtol=1e-2, atol=1e-2).all(), f"{torch_out2} != {jax_out}"


def _get_mixtral_config(use_flash=False, num_kv_heads=4) -> MixtralConfig:
    rope_scaling = {
        "type": "linear",
        "factor": 2.0,
    }
    return MixtralConfig(
        seq_len=128,
        hidden_dim=16,
        intermediate_dim=32,
        num_heads=4,
        num_kv_heads=num_kv_heads,
        rope_scaling=rope_scaling,
        gradient_checkpointing=False,  # disable for tests so debugging is easier
        use_flash_attention=use_flash,
        flash_attention_block_size=8 if use_flash else None,
    )


def _get_random_inputs(config: MixtralConfig):
    Embed = config.Embed
    Pos = config.Pos
    Batch = hax.Axis("batch", 2)
    x = hax.random.normal(random.PRNGKey(0), (Batch, Pos, Embed))
    mask = hax.nn.attention.causal_mask(config.Pos, config.KeyPos)
    return x, mask


@parameterize_with_configs("mixtral*.yaml")
def test_mixtral_configs(config_file):
    from levanter.main.train_lm import TrainLmConfig

    config_class = TrainLmConfig

    check_load_config(config_class, config_file)


@pytest.mark.parametrize("num_kv_heads", [1, 2])
def test_pass_different_length_seq(num_kv_heads):
    config = MixtralConfig(
        seq_len=32,
        hidden_dim=16,
        intermediate_dim=32,
        num_heads=2,
        num_kv_heads=num_kv_heads,
    )
    check_model_works_with_seqlen(MixtralLMHeadModel, config, 16)