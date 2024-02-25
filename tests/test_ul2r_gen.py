import jax
import jax.numpy as jnp
import numpy as np

import haliax as hax

import test_utils
from levanter.data.ul2r import (
    DenoisingConfig,
    PrefixLmConfig,
    RDenoisingConfig,
    Ul2Example,
    Ul2InstanceGenerator,
    XDenoisingConfig,
)


def test_ul2_generator_seed_works():
    # Generate synthetic data
    B = hax.Axis("B", 20)
    L = hax.Axis("L", 512)
    synthetic_data = hax.random.randint(jax.random.PRNGKey(0), shape=(B, L), minval=0, maxval=1000)
    tokenizer = test_utils.gpt2_tokenizer

    ul2_generator = Ul2InstanceGenerator(
        tokenizer,
        [f"<mask_{i}>" for i in range(500)],
        list(DenoisingConfig.ul2r_configs().values()),
        task_weights=None,
    )

    for i in range(B.size):
        tokens = synthetic_data["B", i].array
        a = ul2_generator.sample(tokens, jax.random.PRNGKey(i)).render(tokenizer)
        b = ul2_generator.sample(tokens, jax.random.PRNGKey(i)).render(tokenizer)
        assert a == b
        c = ul2_generator.sample(tokens, jax.random.PRNGKey(i + 1)).render(tokenizer)
        assert a != c


def test_ul2_generator_can_handle_too_few_sentinels():
    tokenizer = test_utils.gpt2_tokenizer
    B = hax.Axis("B", 20)
    L = hax.Axis("L", 512)
    synthetic_data = hax.random.randint(jax.random.PRNGKey(0), shape=(B, L), minval=0, maxval=1000)

    ul2_generator = Ul2InstanceGenerator(
        tokenizer,
        [f"<mask_{i}>" for i in range(2)],
        list(DenoisingConfig.ul2r_configs().values()),
        task_weights=None,
    )

    for i in range(B.size):
        tokens = synthetic_data["B", i].array
        # just make sure it doesn't crash
        ul2_generator.sample(tokens, jax.random.PRNGKey(i))


def test_ul2_to_decoder_only():
    QLen = hax.Axis("QLen", 25)
    KLen = QLen.alias("KLen")

    example = Ul2Example(task_token=1000, inputs=np.arange(10), outputs=np.arange(20, 30))

    converted = example.to_decoder_only(1001, QLen, KLen)

    tokens = converted.tokens.array

    assert tokens[0] == 1000
    assert tokens[1] == 0
    assert np.all(tokens[1:11] == example.inputs)
    assert np.all(tokens[11:21] == example.outputs)
    assert np.all(tokens[21:] == 1001)

    loss_mask = converted.loss_mask.array

    assert np.sum(loss_mask) == len(example.outputs)
    assert np.all(loss_mask[0:10] == 0)
    assert np.all(loss_mask[10:20] == 1)
    assert np.all(loss_mask[20:] == 0)

    attn_mask = converted.attn_mask.rearrange((QLen, KLen)).array

    assert hax.all(hax.sum(converted.attn_mask, QLen) > 0)
    assert hax.all(hax.sum(converted.attn_mask, KLen) > 0)

    assert np.all(attn_mask[:, 0] == 1)
    assert np.all(np.sum(attn_mask[np.arange(0, 11), :], 1) == 11)
    # start with 1 extra because you can attend to yourself
    assert np.all(
        np.sum(attn_mask[np.arange(11, 21), :], 1) == 11 + np.arange(1, 11)
    )  # outputs attend to task token + inputs + previous outputs


# to make double extra sure, verify we don't leak information on accident
def test_ul2r_prefix_attention():
    L = 20
    D = 2
    SeqLen = hax.Axis("SeqLen", L)
    KSeqLen = SeqLen.alias("KSeqLen")
    Head = hax.Axis("Head", D)

    input_length = 10

    inputs = np.arange(input_length)
    outputs = np.arange(input_length * 2, (input_length * 2) + (L - input_length))
    assert len(outputs) + input_length == L

    example = Ul2Example(task_token=1000, inputs=inputs, outputs=outputs).to_decoder_only(1001, SeqLen, KSeqLen)
    attn_mask = example.attn_mask
    # testing here that we can't attend to the inputs from the outputs
    keys = np.zeros((L, D), dtype=np.float32)
    keys[input_length + 1, 1] = 100.0  # really want to attend to this
    values = np.zeros((L, D), dtype=np.float32)
    values[input_length + 1, 1] = 300.0  # check if we did attend

    query = np.ones((L, D), dtype=np.float32)

    query = hax.named(query, (SeqLen, Head))
    keys = hax.named(keys, (KSeqLen, Head))
    values = hax.named(values, (KSeqLen, Head))
    result = hax.nn.attention.dot_product_attention(KSeqLen, Head, query, keys, values, mask=attn_mask)
    result = result.rearrange((SeqLen, Head)).array
    # the values for the outputs should all close to 300
    assert jnp.allclose(result[input_length + 1 :, 1], 300)
    assert jnp.allclose(result[0 : input_length + 1, 1], 0)


def test_ul2r_decoder_only_uses_both_inputs_and_outputs():
    ex = Ul2Example(task_token=1000, inputs=np.arange(10), outputs=np.arange(20, 30))
    decoder_only = ex.to_decoder_only(1001, hax.Axis("L", 10), hax.Axis("KL", 10))
    assert decoder_only.loss_mask.size == 10
    assert hax.sum(decoder_only.loss_mask, "L") == 5

    ex = Ul2Example(task_token=1000, inputs=np.arange(10), outputs=np.arange(20, 23))
    decoder_only = ex.to_decoder_only(1001, hax.Axis("L", 10), hax.Axis("KL", 10))
    assert decoder_only.loss_mask.size == 10
    assert hax.sum(decoder_only.loss_mask, "L") == 3


def test_ul2r_task_weights_work():
    tokenizer = test_utils.gpt2_tokenizer
    B = hax.Axis("B", 200)
    L = hax.Axis("L", 512)
    synthetic_data = hax.random.randint(jax.random.PRNGKey(0), shape=(B, L), minval=0, maxval=1000)

    tasks = [
        RDenoisingConfig("[R]", 0.15, 3.0),
        XDenoisingConfig("[X]", 0.15, 32.0),
        XDenoisingConfig("[X]", 0.5, 3.0),
        PrefixLmConfig("[P]"),
    ]

    ul2_generator_1 = Ul2InstanceGenerator(
        tokenizer,
        [f"<mask_{i}>" for i in range(2)],
        tasks,
        task_weights=[1.0, 0.0, 0.0, 1.0],
    )

    x_token_index = tokenizer.encode("[X]")[0]
    r_token_index = tokenizer.encode("[R]")[0]
    p_token_index = tokenizer.encode("[P]")[0]

    samples_1 = [
        ul2_generator_1.sample(synthetic_data["B", i].array, jax.random.PRNGKey(i)).task_token for i in range(B.size)
    ]
    # get count of task tokens
    x_count_1 = np.sum([np.sum(s == x_token_index) for s in samples_1])
    r_count_1 = np.sum([np.sum(s == r_token_index) for s in samples_1])
    p_count_1 = np.sum([np.sum(s == p_token_index) for s in samples_1])

    assert x_count_1 == 0
    assert r_count_1 > B.size / 4
    assert p_count_1 > B.size / 4

    ul2_generator_2 = Ul2InstanceGenerator(
        tokenizer,
        [f"<mask_{i}>" for i in range(2)],
        tasks,
        task_weights=[0.0, 1.0, 0.0, 1.0],
    )

    samples_2 = [
        ul2_generator_2.sample(synthetic_data["B", i].array, jax.random.PRNGKey(i)).task_token for i in range(B.size)
    ]
    # get count of task tokens
    x_count_2 = np.sum([np.sum(s == x_token_index) for s in samples_2])
    r_count_2 = np.sum([np.sum(s == r_token_index) for s in samples_2])
    p_count_2 = np.sum([np.sum(s == p_token_index) for s in samples_2])

    assert x_count_2 > B.size / 4
    assert r_count_2 == 0
    assert p_count_2 > B.size / 4