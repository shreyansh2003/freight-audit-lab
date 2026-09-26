import copy

import pandas as pd
import pytest

from freight_audit_lab.config import load_config
from freight_audit_lab.generate.diesel import diesel_weeks, eia_prices, load_eia_csv

EIA_FIXTURE = """Back to Contents,Data 1: Weekly U.S. No 2 Diesel Retail Prices  (Dollars per Gallon)
Sourcekey,EMD_EPD2D_PTE_NUS_DPG

Date,Weekly U.S. No 2 Diesel Retail Prices  (Dollars per Gallon)
"Jan 06, 2025",3.608
"Jan 13, 2025",3.636
"Jan 21, 2025",3.705
"Feb 03, 2025",3.680
"""


def test_eia_loader_skips_junk_headers(tmp_path):
    path = tmp_path / "eia.csv"
    path.write_text(EIA_FIXTURE)
    df = load_eia_csv(path)
    assert len(df) == 4
    assert df["week_start"].iloc[0] == pd.Timestamp("2025-01-06")
    # Tuesday Jan 21 (after the MLK holiday) snaps to Monday Jan 20
    assert df["week_start"].iloc[2] == pd.Timestamp("2025-01-20")
    assert df["price_per_gallon"].tolist() == [3.608, 3.636, 3.705, 3.680]


def _cfg_with_eia(path):
    cfg = copy.deepcopy(load_config())
    cfg["diesel"]["eia_csv_path"] = str(path)
    return cfg


def test_eia_fails_when_period_not_covered(tmp_path):
    path = tmp_path / "eia.csv"
    path.write_text(EIA_FIXTURE)
    cfg = _cfg_with_eia(path)
    with pytest.raises(ValueError, match="covers 2025-01-06 to 2025-02-03"):
        eia_prices(diesel_weeks(cfg), cfg)


def test_eia_gap_carries_last_price_forward(tmp_path):
    path = tmp_path / "eia.csv"
    path.write_text(EIA_FIXTURE)
    cfg = _cfg_with_eia(path)
    weeks = pd.date_range("2025-01-06", "2025-02-03", freq="7D")
    # Jan 27 is missing from the file, so it keeps Jan 20's 3.705
    assert list(eia_prices(weeks, cfg)) == [3.608, 3.636, 3.705, 3.705, 3.680]


def test_diesel_weeks_span():
    weeks = diesel_weeks(load_config())
    assert weeks[0] == pd.Timestamp("2024-12-02")   # 4 weeks before Monday 2024-12-30
    assert weeks[-1] == pd.Timestamp("2026-03-30")  # last Monday on or before 2026-03-31
    assert all(w.weekday() == 0 for w in weeks)
