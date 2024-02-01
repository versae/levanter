import logging
from dataclasses import dataclass

import equinox as eqx
import jax
import jmp

import haliax as hax
from haliax import Axis
from haliax.partitioning import fsdp, round_axis_for_partitioning

import levanter
from levanter.checkpoint import load_checkpoint
from levanter.data import ReplicatedBatchLoader
from levanter.data.text import CausalLmDataset, LMDatasetConfig
from levanter.models.gpt2 import Gpt2Config
from levanter.models.lm_model import LmConfig, LmExample, LmHeadModel
from levanter.trainer import TrainerConfig
from levanter.utils.jax_utils import use_cpu_device
from levanter.utils.tree_utils import inference_mode
from levanter.visualization import compute_and_visualize_log_probs


logger = logging.getLogger(__name__)


@dataclass
class VizGpt2Config:
    checkpoint_path: str
    path: str = "logprobs.html"
    trainer: TrainerConfig = TrainerConfig()
    data: LMDatasetConfig = LMDatasetConfig()
    model: LmConfig = Gpt2Config()

    num_docs: int = 256


def main(config: VizGpt2Config):
    levanter.initialize(config)
    tokenizer = config.data.the_tokenizer

    # some axes we use outside the model proper
    EvalBatch = config.trainer.EvalBatch
    Pos = config.model.Pos
    KeyPos = config.model.KeyPos

    validation_set = config.data.validation_set(Pos.size)
    assert validation_set is not None
    eval_loader = ReplicatedBatchLoader(
        CausalLmDataset(validation_set, Pos, KeyPos), EvalBatch, config.trainer.compute_env
    )

    compute_axis_mapping = config.trainer.compute_axis_mapping
    parameter_axis_mapping = config.trainer.parameter_axis_mapping

    with config.trainer.param_env:
        key = jax.random.PRNGKey(0)

        vocab_size = len(tokenizer)
        Vocab = round_axis_for_partitioning(Axis("vocab", vocab_size))
        if vocab_size != Vocab.size:
            logger.info(f"Rounding vocab size from {vocab_size} to {Vocab.size} for partitioning")

        mp: jmp.Policy = config.trainer.mp

        # don't want to compute the mask w.r.t. the final token

        @fsdp(parameter_axis_mapping, compute_axis_mapping)
        def compute_log_probs(model: LmHeadModel, example: LmExample):
            model = inference_mode(model, True)
            model = mp.cast_to_compute(model)
            logprobs = model.compute_loss(example, reduction=None)
            # roll forward to get the loss for each predicted token
            logprobs = hax.roll(logprobs, 1, Pos)
            return logprobs.rearrange((EvalBatch, Pos)).array

        # initialize the model
        with use_cpu_device():
            model = eqx.filter_eval_shape(config.model.build, Vocab, key=key)
            # TODO: don't load the entire checkpoint into CPU memory when we only need our share of the model
            model = load_checkpoint(model, config.checkpoint_path, subpath="model")

        assert model is not None

        model = hax.shard(model)

        compute_and_visualize_log_probs(
            path=config.path,
            model=model,
            tokenizer=tokenizer,
            log_prob_fn=compute_log_probs,
            test_data=eval_loader,
            max_docs=config.num_docs,
        )


if __name__ == "__main__":
    levanter.config.main(main)()
