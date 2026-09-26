"""Shared fixtures. The full Stage 2 dataset is generated once per test session."""

import pytest

from freight_audit_lab.config import load_config
from freight_audit_lab.generate import generate_all
from tests.fixtures import run_pipeline


@pytest.fixture(scope="session")
def cfg():
    return load_config()


@pytest.fixture(scope="session")
def full(cfg, tmp_path_factory):
    """(tables, data_dir) from generate_all on the default config."""
    data_dir = tmp_path_factory.mktemp("data")
    return generate_all(cfg, data_dir), data_dir


@pytest.fixture(scope="session")
def audited(cfg, full):
    """Every result frame from normalize -> rerate -> audit -> baseline on the full generated data."""
    return run_pipeline(full[1], cfg)
