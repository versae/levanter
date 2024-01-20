import enum
import functools
from typing import Callable, Optional, ParamSpec, TypeVar

import equinox as eqx
import jax
import jax.numpy as jnp
from jax.lax import with_sharding_constraint
from jax.sharding import PartitionSpec

import haliax as hax
from haliax import Axis
from haliax.partitioning import ResourceAxis
from haliax.util import is_jax_array_like, is_named_array


Args = ParamSpec("Args")
R = TypeVar("R")


class ReductionType(enum.Enum):
    SUM = enum.auto()
    MEAN = enum.auto()
    # TODO: add MAX?


# cf https://github.com/google-research/t5x/blob/main/t5x/trainer.py#L617
def microbatched(
    fn: Callable[Args, R],
    Batch: Axis,
    per_device_parallelism: int,
    accum_axis_mapping,
    compute_axis_mapping,
    patch_in_rng_key: Optional[str] = "key",
    reduce: ReductionType = ReductionType.MEAN,
    accum_dtype: Optional[jnp.dtype] = None,
) -> Callable[Args, R]:
    """
    Wraps a function that takes a batch and changes it to instead take microbatches and accumulate the results
    This function has to reduce the batch axis, so it can't be used for functions that need to keep the batch axis.

    Args:
        fn: a function to wrap
        Batch: the batch axis
        per_device_parallelism: how many examples to process at once on each device
        accum_axis_mapping:  the axis mapping for the accumulator (typically this is the same as the params)
        compute_axis_mapping:  the axis mapping for the computation (typically this is the same as the inputs)
        patch_in_rng_key: if provided, this kwarg will be split, 1 for each accum step. It won't work if the
            PRNGKey is passed in as a positional argument.
        reduce: whether to sum or average the results
        accum_dtype: the dtype of floating point values in the accumulator. If None, this will be inferred from the return type of `fn`.

    Returns:
        a function that splits the batch into microbatches, calls the function on each microbatch, and
        accumulates the results.
    """
    batch_size = Batch.size
    data_axis_size = hax.partitioning.physical_axis_size(Batch, compute_axis_mapping)
    if data_axis_size is None:
        raise ValueError(f"{Batch} axis must be sharded")
    physical_axis_name = hax.partitioning.physical_axis_name(Batch, compute_axis_mapping)
    assert physical_axis_name is not None

    if per_device_parallelism < 0:
        raise ValueError(f"Bad value for {per_device_parallelism=}")

    microbatch_size = data_axis_size * per_device_parallelism
    num_micro_steps = batch_size // microbatch_size
    Microbatch = Batch.resize(microbatch_size)
    AccumStep = Axis("accum_step", num_micro_steps)
    assert num_micro_steps * microbatch_size == batch_size

    if reduce not in ReductionType:
        raise ValueError(f"accum_type must be one of {ReductionType}")

    @functools.wraps(fn)
    def wrapped_fn(*args, **kwargs):

        # first, determine the shape and make accumulator arrays
        r_shape = eqx.filter_eval_shape(fn, *args, **kwargs)
        acc = _zeros_like_tree(r_shape, accum_axis_mapping, accum_dtype)

        # then, reshape the inputs from (Batch, ...) to (AccumStep, Microbatch, ...)

        # Special handling for PRNGKey: it comes in as a single key, but we need to split it for each microbatch
        key = kwargs.get(patch_in_rng_key, None)
        if key is not None:
            key = jax.random.split(key, num_micro_steps)
            kwargs = kwargs.copy()
            kwargs.pop(patch_in_rng_key)

        args = _reshape_for_microbatch(Batch, Microbatch, AccumStep, args, compute_axis_mapping)

        def loop(acc, microbatch_and_key):
            microbatch, microbatch_kwargs, key = microbatch_and_key
            with jax.named_scope("compute"):
                microbatch_kwargs = microbatch_kwargs.copy()
                if key is not None:
                    microbatch_kwargs[patch_in_rng_key] = key

                with hax.axis_mapping(compute_axis_mapping):
                    this_r = fn(*microbatch, **microbatch_kwargs)

            with jax.named_scope("accum"):
                acc = eqx.apply_updates(acc, this_r)
                acc = hax.shard_with_axis_mapping(acc, accum_axis_mapping)

            return acc

        with jax.named_scope("microbatched"):
            acc = hax.fold(loop, AccumStep)(acc, (args, kwargs, key))

            if reduce == ReductionType.MEAN:
                acc = jax.tree_util.tree_map(lambda x: x / num_micro_steps, acc)

        return acc

    return wrapped_fn


def _reshape_for_microbatch(Batch: Axis, Microbatch: Axis, AccumStep: Axis, inputs, axis_mapping):
    def _reshape(x):
        if isinstance(x, hax.NamedArray):
            if not x.has_axis(Batch.name):
                return x
            x = x.unflatten_axis(Batch, (AccumStep, Microbatch))
            return hax.shard_with_axis_mapping(x, axis_mapping)
        elif isinstance(x, jnp.ndarray):
            x = x.reshape((AccumStep.size, Microbatch.size) + x.shape[1:])
            return with_sharding_constraint(x, PartitionSpec(None, ResourceAxis.DATA, *(None,) * (len(x.shape) - 2)))
        else:
            assert jnp.isscalar(x)
            return x

    return jax.tree_util.tree_map(_reshape, inputs, is_leaf=is_named_array)


def _zeros_like_tree(r_shape, axis_mapping, accum_dtype):
    _zeros = functools.partial(_zeros_like, axis_mapping, accum_dtype)
    acc = jax.tree_util.tree_map(_zeros, r_shape, is_leaf=is_named_array)
    return acc


def _zeros_like(mapping, dtype, n):
    if isinstance(n, hax.NamedArray):
        return hax.shard(hax.zeros_like(n, dtype=dtype), mapping)
    elif is_jax_array_like(n):
        return jnp.zeros_like(n, dtype)
    else:
        assert jnp.isscalar(n)
        return 0.0
