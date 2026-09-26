"""Tolerance sweep: cost arithmetic, the recommendation rule, and a consistency check on the full data."""

import json

import pandas as pd
import pytest

from freight_audit_lab.evaluate import evaluate, load_labels
from freight_audit_lab.sweep import SWEEPS, grid_with_current, net_value, recommend, rationale, run_sweep, write_sweep

CENT = 0.01
EVAL_CFG = {"review_minutes_per_flag": 6, "analyst_cost_per_hour": 45.0, "false_dispute_cost": 25.0}


def table(rows):
    """A sweep table from (value, precision, net, is_current) tuples."""
    df = pd.DataFrame(rows, columns=["value", "precision", "net_value_estimate", "is_current"])
    df["meets_precision_floor"] = df["precision"] >= 0.90
    return df.assign(error_type="rate_overcharge", carrier_mode="ALL", flags=10, tp=9, fp=1, fn=0, recall=0.9)


def test_net_value_is_tp_dollars_less_review_and_false_dispute_cost():
    """10 flags x 6 min = 1 hour = $45.00; 2 false disputes x $25 = $50; $200 recovered - 95 = $105."""
    review, false_dispute, net = net_value({"flags": 10, "fp": 2, "tp_dollars": 200.0}, EVAL_CFG)
    assert (review, false_dispute) == (pytest.approx(45.00, abs=CENT), pytest.approx(50.00, abs=CENT))
    assert net == pytest.approx(105.00, abs=CENT)


def test_recommendation_maximizes_net_value_subject_to_the_precision_floor():
    t = table([(0.0, 0.70, 900.0, False),      # best net value, but under the floor
               (0.5, 0.95, 500.0, False),
               (1.0, 0.99, 400.0, True),
               (2.0, 1.00, 100.0, False)])
    assert t.loc[recommend(t, 0.90), "value"] == 0.5


def test_a_tie_goes_to_the_point_closest_to_the_current_setting():
    t = table([(0.5, 0.95, 500.0, False), (1.0, 0.99, 500.0, True), (2.0, 1.0, 500.0, False)])
    assert t.loc[recommend(t, 0.90), "value"] == 1.0


def test_when_nothing_meets_the_floor_the_most_precise_point_is_recommended_and_says_so():
    t = table([(0.0, 0.50, 900.0, False), (1.0, 0.80, 400.0, True), (2.0, 0.85, 100.0, False)])
    i = recommend(t, 0.90)
    assert t.loc[i, "value"] == 2.0 and "No point on the grid reaches 90% precision" in rationale(t, i, 0.90)


def test_rationale_quotes_the_numbers_and_the_current_setting():
    t = table([(0.0, 0.70, 900.0, False), (0.5, 0.95, 500.0, False), (1.0, 0.99, 400.0, True)])
    text = rationale(t, 1, 0.90)
    assert "At 0.5" in text and "$500" in text and "current 1 gives $400" in text and "$100" in text


def test_the_current_setting_is_always_on_the_grid():
    assert grid_with_current([0.0, 0.01, 0.02], 0.015) == [0.0, 0.01, 0.015, 0.02]
    assert grid_with_current([0.0, 0.01], 0.01) == [0.0, 0.01]


# ---------------------------------------------------------------- on the full generated data


@pytest.fixture(scope="module")
def swept(audited, full, cfg):
    labels = load_labels(full[1])
    from freight_audit_lab.csv_io import load_reference
    from freight_audit_lab.normalize import normalize
    from freight_audit_lab.rerate import rerate
    norm = normalize(cfg, full[1])
    ref = load_reference(full[1])
    return run_sweep(norm, rerate(norm, ref, cfg), ref, cfg, labels), labels


def test_every_recommendation_meets_the_precision_floor_and_config_is_untouched(swept, cfg):
    result, _ = swept
    for name, rec in result["recommended"].items():
        rows = result["sweep"].query("tolerance == @name")
        best = rows[rows["is_recommended"]].iloc[0]
        assert len(rows[rows["is_recommended"]]) == 1 and best["value"] == rec["recommended"]
        assert best["precision"] >= cfg["evaluation"]["min_precision"]
        assert best["net_value_estimate"] == rows.loc[rows["meets_precision_floor"], "net_value_estimate"].max()
        assert rec["current"] == cfg["audit"]["tolerances"][name]         # reported next to, not written into, config


def test_the_grid_is_swept_for_all_four_tolerances_including_fsc_tl_pct(swept, cfg):
    sweep = swept[0]["sweep"]
    assert set(sweep["tolerance"]) == set(SWEEPS) and "fsc_tl_pct" in set(sweep["tolerance"])
    tl = sweep[sweep["tolerance"] == "fsc_tl_pct"]
    assert list(tl["value"]) == cfg["evaluation"]["sweep"]["fsc_tl_pct"] and set(tl["carrier_mode"]) == {"TL"}


def test_the_current_point_matches_the_engine_evaluation(swept, audited, full):
    """Scoring one rule at config tolerances in the sweep must give the same counts as the full evaluation."""
    result, labels = swept
    engine = evaluate(audited["audit_flags"], audited["baseline_flags"], labels)["by_type"].query("system == 'engine'")
    engine = engine.set_index("error_type")
    for name, error_type in [("rate_pct", "rate_overcharge"), ("weight_pct", "weight_overbilling")]:
        cur = result["sweep"].query("tolerance == @name and is_current").iloc[0]
        assert (cur["tp"], cur["fp"], cur["fn"]) == tuple(engine.loc[error_type, ["tp", "fp", "fn"]])


def test_loosening_a_tolerance_never_adds_flags(swept):
    for _, rows in swept[0]["sweep"].groupby("tolerance"):
        assert rows.sort_values("value")["flags"].is_monotonic_decreasing


def test_outputs_are_written(swept, tmp_path):
    write_sweep(swept[0], tmp_path)
    recommended = json.loads((tmp_path / "recommended_tolerances.json").read_text())
    assert set(recommended) == set(SWEEPS) and "rationale" in recommended["rate_pct"]
    assert (tmp_path / "sweep.csv").exists()
