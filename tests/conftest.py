"""Shared fixtures. The full Stage 2 dataset is generated once per test session."""

import pytest

from freight_audit_lab.config import load_config
from freight_audit_lab.generate import generate_all


@pytest.fixture(scope="session")
def cfg():
    return load_config()


@pytest.fixture(scope="session")
def full(cfg, tmp_path_factory):
    """(tables, data_dir) from generate_all on the default config."""
    data_dir = tmp_path_factory.mktemp("data")
    return generate_all(cfg, data_dir), data_dir
