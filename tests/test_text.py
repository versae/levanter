from tempfile import TemporaryDirectory

from levanter.data.text import LMDatasetConfig


def test_dont_blow_up_without_validation_set():
    with TemporaryDirectory() as td:
        config = LMDatasetConfig(train_urls=["kaa"], validation_urls=[], cache_dir=f"{td}")

        # mostly just making sure this doesn't blow up
        assert config.validation_set(10) is None
