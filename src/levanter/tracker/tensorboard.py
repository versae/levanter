import logging
import os
import typing
from dataclasses import dataclass
from typing import Any, Optional

import jax
import numpy as np

from levanter.tracker import Tracker, TrackerConfig
from levanter.tracker.histogram import Histogram


pylogger = logging.getLogger(__name__)

if typing.TYPE_CHECKING:
    from tensorboardX import SummaryWriter  # noqa: F401


class TensorboardTracker(Tracker):
    name: str = "tensorboard"

    def __init__(self, writer: "SummaryWriter"):
        self.writer = writer

    def log_hyperparameters(self, hparams: typing.Mapping[str, Any]):
        self.writer.add_hparams(hparams, {"dummy": 0})

    def log(self, metrics: typing.Mapping[str, Any], *, step, commit=None):
        del commit
        metrics = _flatten_nested_dict(metrics)
        for k, value in metrics.items():
            if isinstance(value, jax.Array):
                if value.ndim == 0:
                    value = value.item()
                else:
                    value = np.array(value)
            elif isinstance(value, Histogram):
                num = value.num
                if hasattr(num, "item"):
                    num = num.item()
                self.writer.add_histogram_raw(
                    k,
                    min=value.min.item(),
                    max=value.max.item(),
                    num=num,
                    sum=value.sum.item(),
                    sum_squares=value.sum_squares.item(),
                    bucket_limits=np.array(value.bucket_limits).tolist(),
                    bucket_counts=np.concatenate([[0], np.array(value.bucket_counts)]).tolist(),
                    global_step=step,
                )
                continue

            self.writer.add_scalar(k, value, global_step=step)

    def log_summary(self, metrics: dict[str, Any]):
        for k, v in metrics.items():
            self.writer.add_scalar(k, v, global_step=None)

    def log_artifact(self, artifact_path, *, name: Optional[str] = None, type: Optional[str] = None):
        pylogger.error("TensorboardLogger does not support logging artifacts yet")
        pass


@TrackerConfig.register_subclass("tensorboard")
@dataclass
class TensorboardConfig(TrackerConfig):
    logdir: str = "tblogs"
    comment: Optional[str] = ""
    purge_step: Optional[int] = None
    max_queue: Optional[int] = 10
    flush_secs: Optional[int] = 120
    filename_suffix: Optional[str] = ""
    write_to_disk: Optional[bool] = True

    def init(self, run_id: Optional[str]) -> TensorboardTracker:
        dir_to_write = self.logdir
        if run_id is not None:
            dir_to_write = os.path.join(dir_to_write, run_id)

        pylogger.info(f"Writing Tensorboard logs to {dir_to_write}")

        from tensorboardX import SummaryWriter  # noqa: F811

        writer = SummaryWriter(
            dir_to_write,
            comment=self.comment,
            purge_step=self.purge_step,
            max_queue=self.max_queue,
            flush_secs=self.flush_secs,
            filename_suffix=self.filename_suffix,
            write_to_disk=self.write_to_disk,
        )

        return TensorboardTracker(writer)


def _flatten_nested_dict(d):
    def items():
        for key, value in d.items():
            if isinstance(value, dict):
                for subkey, subvalue in _flatten_nested_dict(value).items():
                    yield key + "/" + subkey, subvalue
            else:
                yield key, value

    return dict(items())