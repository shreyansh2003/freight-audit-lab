import copy

import pytest

from freight_audit_lab.config import ConfigError, load_config, validate


def test_config_loads():
    cfg = load_config()
    assert cfg["seed"] == 42
    assert len(cfg["carriers"]) == 8


def test_missing_key_names_the_key():
    cfg = copy.deepcopy(load_config())
    del cfg["rates"]["ltl"]["min_charge"]
    with pytest.raises(ConfigError, match="rates.ltl.min_charge"):
        validate(cfg)


def test_monthly_weights_must_sum_to_one():
    cfg = copy.deepcopy(load_config())
    cfg["shipments"]["monthly_weights"][0] += 0.05
    with pytest.raises(ConfigError, match="sum to 1"):
        validate(cfg)


def test_missing_file_is_clear(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")
