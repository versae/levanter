import dataclasses
import logging
import os
import tempfile
import typing
import warnings
from dataclasses import dataclass
from typing import Any, List, Optional, Union

import draccus
import jax
from draccus import field
from git import InvalidGitRepositoryError, NoSuchPathError, Repo

from levanter.tracker import Tracker
from levanter.tracker.helpers import generate_pip_freeze, hparams_to_dict, infer_experiment_git_root
from levanter.tracker.tracker import TrackerConfig
from levanter.utils import jax_utils


if typing.TYPE_CHECKING:
    import wandb
    import wandb.sdk.lib.disabled


logger = logging.getLogger(__name__)

WandbRun = Union["wandb.sdk.wandb_run.Run", "wandb.sdk.lib.disabled.RunDisabled"]


class WandbTracker(Tracker):
    _run: Optional[WandbRun]

    def __init__(self, run: Optional[WandbRun]):
        import wandb

        if run is None:
            if wandb.run is None:
                logger.warning("Wandb run is not initialized. Initializing a new run.")
                run = wandb.init()

        self._run = run

    def log_hyperparameters(self, hparams: dict[str, Any]):
        if self._run is None:
            raise RuntimeError("Must call init before logging hyperparameters")
        self._run.config.update(hparams)

    def log(self, metrics: dict[str, Any], *, step, commit=None):
        if self._run is None:
            raise RuntimeError("Must call init before logging metrics")
        self._run.log(metrics, step=step, commit=commit)

    def log_summary(self, metrics: dict[str, Any]):
        if self._run is None:
            raise RuntimeError("Must call init before logging summary")
        self._run.summary.update(metrics)

    def log_artifact(self, artifact, *, name: Optional[str] = None, type: Optional[str] = None):
        if self._run is None:
            raise RuntimeError("Must call init before logging artifacts")
        self._run.log_artifact(artifact, name=name, type=type)


def is_wandb_available():
    try:
        import wandb
    except ImportError:
        return False
    return wandb is not None and wandb.run is not None


@TrackerConfig.register_subclass("wandb")
@dataclass
class WandbConfig(TrackerConfig):
    """
    Configuration for wandb.
    """

    entity: Optional[str] = None  # An entity is a username or team name where you send runs
    project: Optional[str] = None  # The name of the project where you are sending the enw run.
    name: Optional[str] = None  # A short display name for this run, which is how you'll identify this run in the UI.
    tags: List[str] = field(default_factory=list)  # Will populate the list of tags on this run in the UI.
    id: Optional[str] = None  # A unique ID for this run, used for resuming. It must be unique in the project
    group: Optional[str] = None  # Specify a group to organize individual runs into a larger experiment.
    mode: Optional[str] = None  # Can be "online", "offline" or "disabled". If None, it will be whatever W&B decides.
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

    def init(self, run_id: Optional[str], hparams=None) -> WandbTracker:
        import wandb

        if run_id is not None and self.id is not None and run_id != self.id:
            warnings.warn(
                f"Both trainer's id {run_id} and WandB's id {self.id} are set. WandB will use the id set in its"
                " config."
            )

        id = self.id
        if id is None:
            id = run_id

        hparams_to_save = hparams_to_dict(hparams)

        # for distributed runs, we only want the primary worker to use wandb, so we make everyone else be disabled
        # however, we do share information about the run id, so that we can link to it from the other workers
        if jax.process_index() != 0:
            mode = self.mode
        else:
            mode = "disabled"

        git_settings = self._git_settings()

        if "git_commit" in git_settings:
            hparams_to_save["git_commit"] = git_settings["git_commit"]

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
            settings=git_settings,
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

            logger.info(f"Synced wandb run information from process 0: {r.name} {r.id}")

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
            requirements = generate_pip_freeze()
            with open(requirements_path, "w") as f:
                f.write(requirements)
            if wandb.run is not None:
                wandb.run.log_artifact(str(requirements_path), name="requirements.txt", type="requirements")

        wandb.summary["num_devices"] = jax.device_count()
        wandb.summary["num_hosts"] = jax.process_count()
        wandb.summary["backend"] = jax.default_backend()

        return WandbTracker(r)

    def _git_settings(self):
        other_settings = dict()
        if isinstance(self.save_code, str):
            code_dir = self.save_code
        elif self.save_code:
            code_dir = infer_experiment_git_root() or "."  # type: ignore
        else:
            code_dir = None
        if code_dir is not None:
            logger.info(f"Setting wandb code_dir to {code_dir}")
            other_settings["code_dir"] = code_dir
            other_settings["git_root"] = code_dir
            # for some reason, wandb isn't populating the git commit, so we do it here
            try:
                repo = Repo(code_dir)
                other_settings["git_commit"] = repo.head.commit.hexsha
            except (NoSuchPathError, InvalidGitRepositoryError):
                logger.warning(f"Could not find git repo at {code_dir}")
                pass
        return other_settings
