import abc
import contextlib
import dataclasses
import logging as pylogging
import os
import tempfile
import time
import typing
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Union

import draccus
import jax
from draccus import field
from git import InvalidGitRepositoryError, NoSuchPathError, Repo
from optax import MultiStepsState

from levanter.utils import jax_utils
from levanter.utils.jax_utils import jnp_to_python


pylogger = pylogging.getLogger(__name__)

_global_logger: Optional["MetricsLogger"] = None


def log_metrics(metrics: dict[str, Any], *, step):
    """
    Log metrics to the global logger.

    :param metrics: Metrics to log
    :param step: Step to log metrics at
    """
    global _global_logger
    if _global_logger is None:
        raise RuntimeError("No global logger set")

    _global_logger.log(metrics, step=step)


def jit_log_metrics(metrics, *, step=None):
    """uses jax effect callback to log to wandb from the host"""
    jax.debug.callback(log_metrics, metrics, step=step)


def log_summary(metrics: dict[str, Any]):
    """
    Log summary metrics to the global logger.

    :param metrics: Metrics to log
    """
    global _global_logger
    if _global_logger is None:
        raise RuntimeError("No global logger set")
    _global_logger.log_summary(metrics)

@typing.overload
def global_logger() -> "MetricsLogger":
    ...


@typing.overload
def global_logger(logger: "MetricsLogger") -> contextlib.AbstractContextManager:
    """Context manager for setting the global logger"""
    ...


def global_logger(logger: Optional["MetricsLogger"] = None) -> Union["MetricsLogger", contextlib.AbstractContextManager]:
    """
    Get or set the global logger.

    :param logger: If provided, sets the global logger to this value.
    :return: The global logger, or a context manager for setting the global logger.
    """
    global _global_logger
    if logger is None:
        if _global_logger is None:
            raise RuntimeError("No global logger set")
        return _global_logger
    else:
        return _GlobalLoggerContextManager(logger)


class MetricsLogger(abc.ABC):
    """
    A logger for logging metrics to some backend(s).
    Meant to be used with the [global_logger][] context manager, but can also be used directly.
    """
    @abc.abstractmethod
    def init(self, run_id: Optional[str]):
        pass

    @abc.abstractmethod
    def log_hyperparameters(self, hparams: dict[str, Any]):
        pass

    @abc.abstractmethod
    def log(self, metrics: dict[str, Any], *, step):
        """
        Log metrics to the logger. Step is always required.
        """
        pass

    @abc.abstractmethod
    def log_summary(self, metrics: dict[str, Any]):
        pass

    @abc.abstractmethod
    def log_artifact(self, artifact, *, name: Optional[str] = None, type: Optional[str] = None):
        pass


class CompositeLogger(MetricsLogger):
    def __init__(self, loggers: List[MetricsLogger]):
        self.loggers = loggers

    def init(self, run_id: Optional[str]):
        for logger in self.loggers:
            logger.init(run_id)

    def log_hyperparameters(self, hparams: dict[str, Any]):
        for logger in self.loggers:
            logger.log_hyperparameters(hparams)

    def log(self, metrics: dict[str, Any], *, step):
        for logger in self.loggers:
            logger.log(metrics, step=step)

    def log_summary(self, metrics: dict[str, Any]):
        for logger in self.loggers:
            logger.log_summary(metrics)

    def log_artifact(self, artifact, *, name: Optional[str] = None, type: Optional[str] = None):
        for logger in self.loggers:
            logger.log_artifact(artifact, name=name, type=type)


class _GlobalLoggerContextManager(contextlib.AbstractContextManager):
    def __init__(self, logger: "MetricsLogger"):
        self.logger = logger

    def __enter__(self):
        global _global_logger
        self.old_logger = _global_logger
        _global_logger = self.logger

    def __exit__(self, exc_type, exc_val, exc_tb):
        global _global_logger
        _global_logger = self.old_logger


class WandbLogger(MetricsLogger):
    def __init__(self, config: 'WandbConfig'):
        self.config = config
        self._run = None

    def init(self, run_id: Optional[str]):
        self._run = self.config.init(run_id)

    def log_hyperparameters(self, hparams: dict[str, Any]):
        if self._run is None:
            raise RuntimeError("Must call init before logging hyperparameters")
        self._run.config.update(hparams)

    def log(self, metrics: dict[str, Any], *, step):
        if self._run is None:
            raise RuntimeError("Must call init before logging metrics")
        self._run.log(metrics, step=step)

    def log_summary(self, metrics: dict[str, Any]):
        if self._run is None:
            raise RuntimeError("Must call init before logging summary")
        self._run.summary.update(metrics)

    def log_artifact(self, artifact, *, name: Optional[str] = None, type: Optional[str] = None):
        if self._run is None:
            raise RuntimeError("Must call init before logging artifacts")
        self._run.log_artifact(artifact, name=name, type=type)


class TensorboardLogger(MetricsLogger):

    def __init__(self, logdir: Union[str, Path]):
        self.logdir = logdir
        self.writer = None

    def init(self, run_id: Optional[str]):
        from tensorboardX import SummaryWriter
        dir_to_write = self.logdir
        if run_id is not None:
            dir_to_write = os.path.join(dir_to_write, run_id)
        self.writer = SummaryWriter(dir_to_write)

    def log_hyperparameters(self, hparams: dict[str, Any]):
        self.writer.add_hparams(hparams, {"dummy": 0})

    def log(self, metrics: dict[str, Any], *, step):
        for k, v in metrics.items():
            self.writer.add_scalar(k, v, step)

    def log_summary(self, metrics: dict[str, Any]):
        for k, v in metrics.items():
            self.writer.add_scalar(k, v, 0)

    def log_artifact(self, artifact, *, name: Optional[str] = None, type: Optional[str] = None):
        pylogger.warning("TensorboardLoggerSink does not support logging artifacts yet")
        pass


def log_optimizer_hyperparams(opt_state, prefix: Optional[str] = None, *, step=None):
    if isinstance(opt_state, MultiStepsState):
        opt_state = opt_state.inner_opt_state

    def wrap_key(key):
        if prefix:
            return f"{prefix}/{key}"
        return key

    if hasattr(opt_state, "hyperparams"):
        params = {wrap_key(k): jnp_to_python(v) for k, v in opt_state.hyperparams.items()}
        log_metrics(params, step=step)


def init_logging(path: Union[str, Path], level: int = pylogging.INFO) -> None:
    """
    Initialize logging.Logger with the appropriate name, console, and file handlers.

    :param path: Path for writing log file
    :param level: Default logging level
    """
    process_index = jax.process_index()
    log_format = f"%(asctime)s - {process_index} - %(name)s - %(filename)s:%(lineno)d - %(levelname)s :: %(message)s"
    # use ISO 8601 format for timestamps, except no TZ, because who cares
    date_format = "%Y-%m-%dT%H:%M:%S"

    handlers: List[pylogging.Handler] = [pylogging.FileHandler(path, mode="a"), pylogging.StreamHandler()]

    # Create Root Logger w/ Base Formatting
    pylogging.basicConfig(level=level, format=log_format, datefmt=date_format, handlers=handlers, force=True)

    # Silence Transformers' "None of PyTorch, TensorFlow 2.0 or Flax have been found..." thing
    silence_transformer_nag()


def save_xla_dumps_to_wandb(initial_time: float):
    import os
    if not is_wandb_available():
        pylogger.warning("Wandb is not available, so we can't save XLA dumps")
        return

    import wandb

    # attempt to parse xla_flags to see if we're dumping assembly files
    flags = os.getenv("XLA_FLAGS", None)
    if flags is not None and "xla_dump_to" in flags:
        # parse the path
        # this isn't robust to quotes
        path = flags.split("xla_dump_to=")[1].split(" ")[0]
        pylogger.info(f"Found xla_dump_to={path}, logging to wandb")
        if wandb.run:
            # only want to save the files that were generated during this run
            # XLA_FLAGS has to be set before the first jax call, so we can't just set it in the middle of the run
            # which means it's a pain to control where the files are saved
            # so we just save all the files that were generated during this run
            # this is a bit hacky, but it works
            def include_file(path: str):
                return os.path.getmtime(path) > initial_time

            wandb.run.log_code(root=path, name="xla_dumps", include_fn=include_file)
    else:
        pylogger.warning("XLA_FLAGS is not set to dump to a path, so we can't save the dumps to wandb")


@contextlib.contextmanager
def capture_time():
    start = time.perf_counter()
    done = False

    def fn():
        if done:
            return end - start
        else:
            return time.perf_counter() - start

    yield fn
    end = time.time()





def is_wandb_available():
    try:
        import wandb
    except ImportError:
        return False
    return wandb is not None and wandb.run is not None


def silence_transformer_nag():
    # this is a hack to silence the transformers' "None of PyTorch, TensorFlow 2.0 or Flax have been found..." thing
    # which is annoying and not useful
    # Often we won't call this early enough, but it helps with multiprocessing stuff
    logger = pylogging.getLogger("transformers")
    logger.setLevel(pylogging.ERROR)

    # log propagation bites us here when using ray
    logger.propagate = False


@dataclass
class WandbConfig:
    """
    Configuration for wandb.
    """

    entity: Optional[str] = None  # An entity is a username or team name where you send runs
    project: Optional[str] = None  # The name of the project where you are sending the enw run.
    name: Optional[str] = None  # A short display name for this run, which is how you'll identify this run in the UI.
    tags: List[str] = field(default_factory=list)  # Will populate the list of tags on this run in the UI.
    id: Optional[str] = None  # A unique ID for this run, used for resuming. It must be unique in the project
    group: Optional[str] = None  # Specify a group to organize individual runs into a larger experiment.
    mode: Optional[str] = None  # Can be "online", "offline" or "disabled". If None, it will be online.
    resume: Optional[Union[bool, str]] = None  #
    """
    Set the resume behavior. Options: "allow", "must", "never", "auto" or None.
    By default, if the new run has the same ID as a previous run, this run overwrites that data.
    Please refer to [init](https://docs.wandb.ai/ref/python/init) and [resume](https://docs.wandb.ai/guides/runs/resuming)
    document for more details.
    """

    save_code: Union[bool, str] = True
    """If string, will save code from that directory. If True, will attempt to sniff out the main directory (since we
    typically don't run from the root of the repo)."""

    save_xla_dumps: bool = False
    """If True, will save the XLA code to wandb (as configured by XLA_FLAGS). This is useful for debugging."""

    def init(self, run_id: Optional[str], hparams=None, **extra_hparams):
        import wandb

        if run_id is not None and self.id is not None and run_id != self.id:
            warnings.warn(
                f"Both trainer's id {run_id} and WandB's id {self.id} are set. WandB will use the id set in its"
                " config."
            )

        id = self.id
        if id is None:
            id = run_id

        if hparams is None:
            hparams_to_save = {}
        elif dataclasses.is_dataclass(hparams):
            hparams_to_save = dataclasses.asdict(hparams)
        else:
            hparams_to_save = dict(hparams)

        if extra_hparams:
            hparams_to_save.update(extra_hparams)

        # for distributed runs, we only want the primary worker to use wandb, so we make everyone else be disabled
        # however, we do share information about the run id, so that we can link to it from the other workers
        mode = self.mode
        if jax.process_index() != 0:
            mode = "disabled"

        if isinstance(self.save_code, str):
            code_dir = self.save_code
        elif self.save_code:
            code_dir = WandbConfig._infer_experiment_git_root() or "."  # type: ignore
        else:
            code_dir = None

        other_settings = dict()
        if code_dir is not None:
            pylogger.info(f"Setting wandb code_dir to {code_dir}")
            other_settings["code_dir"] = code_dir
            other_settings["git_root"] = code_dir
            # for some reason, wandb isn't populating the git commit, so we do it here
            try:
                repo = Repo(code_dir)
                other_settings["git_commit"] = repo.head.commit.hexsha
                hparams_to_save["git_commit"] = repo.head.commit.hexsha
            except (NoSuchPathError, InvalidGitRepositoryError):
                pylogger.warning(f"Could not find git repo at {code_dir}")
                pass

        r = wandb.init(
            entity=self.entity,
            project=self.project,
            name=self.name,
            tags=self.tags,
            id=id,
            group=self.group,
            resume=self.resume,
            mode=mode,
            config=hparams_to_save,
            settings=other_settings,
        )

        assert r is not None

        if jax.process_count() > 1:
            # we need to share wandb run information across all hosts, because we use it for checkpoint paths and things
            metadata_to_share = dict(
                entity=r.entity,
                project=r.project,
                name=r.name,
                tags=r.tags,
                id=r.id,
                group=r.group,
            )
            metadata_to_share = jax_utils.multihost_broadcast_sync(
                metadata_to_share, is_source=jax.process_index() == 0
            )

            if jax.process_index() != 0:
                assert r.mode == "disabled"
                for k, v in metadata_to_share.items():
                    setattr(r, k, v)

            pylogger.info(f"Synced wandb run information from process 0: {r.name} {r.id}")

        if dataclasses.is_dataclass(hparams):
            with tempfile.TemporaryDirectory() as tmpdir:
                config_path = os.path.join(tmpdir, "config.yaml")
                with open(config_path, "w") as f:
                    draccus.dump(hparams, f, encoding="utf-8")
                if wandb.run is not None:
                    wandb.run.log_artifact(str(config_path), name="config.yaml", type="config")

        # generate a pip freeze
        with tempfile.TemporaryDirectory() as tmpdir:
            requirements_path = os.path.join(tmpdir, "requirements.txt")
            requirements = _generate_pip_freeze()
            with open(requirements_path, "w") as f:
                f.write(requirements)
            if wandb.run is not None:
                wandb.run.log_artifact(str(requirements_path), name="requirements.txt", type="requirements")

        wandb.summary["num_devices"] = jax.device_count()
        wandb.summary["num_hosts"] = jax.process_count()
        wandb.summary["backend"] = jax.default_backend()

        return r

    @staticmethod
    def _infer_experiment_git_root() -> Optional[str | os.PathLike[str]]:
        # sniff out the main directory (since we typically don't run from the root of the repo)
        # we'll walk the stack and directories for the files in the stack the until we're at a git root
        import os
        import traceback

        stack = traceback.extract_stack()
        # start from the top of the stack and work our way down since we want to hit the main file first
        top_git_root = None
        for frame in stack:
            dirname = os.path.dirname(frame.filename)
            # bit hacky but we want to skip anything that's in the python env
            if any(x in dirname for x in ["site-packages", "dist-packages", "venv", "opt/homebrew", "conda", "pyenv"]):
                continue
            # see if it's under a git root
            try:
                repo = Repo(dirname, search_parent_directories=True)
                top_git_root = repo.working_dir
                break
            except (NoSuchPathError, InvalidGitRepositoryError):
                pylogger.debug(f"Skipping {dirname} since it's not a git root")
                pass
        return top_git_root


def _generate_pip_freeze():
    from importlib.metadata import distributions

    dists = distributions()
    return "\n".join(f"{dist.name}=={dist.version}" for dist in dists)
