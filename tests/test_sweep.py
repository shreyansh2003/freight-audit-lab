"""Tolerance sweep: cost arithmetic, the recommendation rule, and a consistency check on the full data."""

import json

import pandas as pd
import pytest

from freight_audit_lab.evaluate import evaluate, load_labels
from freight_audit_lab.sweep import (SWEEPS, best_point, grid_with_current, net_value, rationale, recommend, run_sweep,
                                     write_sweep)

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


def test_best_point_maximizes_net_value_subject_to_the_precision_floor():
    t = table([(0.0, 0.70, 900.0, False),      # best net value, but under the floor
               (0.5, 0.95, 500.0, False),
               (1.0, 0.99, 400.0, True),
               (2.0, 1.00, 100.0, False)])
    assert t.loc[best_point(t), "value"] == 0.5


def test_a_tie_goes_to_the_point_closest_to_the_current_setting():
    t = table([(0.5, 0.95, 500.0, False), (1.0, 0.99, 500.0, True), (2.0, 1.0, 500.0, False)])
    assert t.loc[best_point(t), "value"] == 1.0


@pytest.mark.parametrize("best_net, changes", [(1400.0, False), (1500.0, False), (1500.01, True)])
def test_materiality_the_best_point_must_beat_the_current_one_by_more_than_the_threshold(best_net, changes):
    """Current net is $500; the threshold is $1,000, so the gain must be strictly more than $1,000."""
    t = table([(0.5, 0.95, best_net, False), (1.0, 0.99, 500.0, True)])
    best, rec = recommend(t, 0.90, 1000.0)
    assert t.loc[best, "value"] == 0.5 and (t.loc[rec, "value"] == 0.5) == changes
    text = rationale(t, best, rec, 0.90, 1000.0)
    assert ("Change to 0.5" in text) == changes and ("Keep 1" in text) != changes
    if not changes:
        assert "materiality threshold" in text and "only $" in text


def test_a_current_setting_below_the_precision_floor_is_changed_whatever_the_gain():
    t = table([(0.0, 0.80, 510.0, True), (1.0, 0.95, 500.0, False)])
    best, rec = recommend(t, 0.90, 1000.0)
    assert t.loc[rec, "value"] == 1.0 and "below the 90% precision floor" in rationale(t, best, rec, 0.90, 1000.0)


def test_when_nothing_meets_the_floor_the_most_precise_point_is_named_and_says_so():
    t = table([(0.0, 0.50, 900.0, False), (1.0, 0.80, 400.0, True), (2.0, 0.85, 100.0, False)])
    best, rec = recommend(t, 0.90, 1000.0)
    assert t.loc[best, "value"] == 2.0 and "No point on the grid reaches 90% precision" in rationale(t, best, rec, 0.90, 1000.0)


def test_rationale_appends_a_caveat_when_there_is_one():
    t = table([(0.0, 0.70, 900.0, False), (0.5, 0.95, 500.0, False), (1.0, 0.99, 400.0, True)])
    best, rec = recommend(t, 0.90, 50.0)
    text = rationale(t, best, rec, 0.90, 50.0, "Caveat: no scale noise.")
    assert "Change to 0.5" in text and "$500" in text and "$100 above the current 1" in text and text.endswith("Caveat: no scale noise.")


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


def test_recommendations_follow_the_materiality_rule_and_config_is_untouched(swept, cfg):
    result, _ = swept
    ev = cfg["evaluation"]
    for name, rec in result["recommended"].items():
        rows = result["sweep"].query("tolerance == @name")
        best, chosen = rows[rows["is_best"]].iloc[0], rows[rows["is_recommended"]].iloc[0]
        current = rows[rows["is_current"]].iloc[0]
        assert len(rows[rows["is_best"]]) == 1 and len(rows[rows["is_recommended"]]) == 1
        assert best["precision"] >= ev["min_precision"]
        assert best["net_value_estimate"] == rows.loc[rows["meets_precision_floor"], "net_value_estimate"].max()
        gain = best["net_value_estimate"] - current["net_value_estimate"]
        assert (chosen["value"] == best["value"]) == (gain > ev["min_material_gain"] or best["value"] == current["value"])
        assert chosen["precision"] >= ev["min_precision"] and rec["recommended"] == chosen["value"]
        assert rec["current"] == cfg["audit"]["tolerances"][name]         # reported next to, not written into, config


def test_only_the_weight_rationale_carries_the_scale_noise_caveat(swept):
    rec = swept[0]["recommended"]
    assert "no scale noise" in rec["weight_pct"]["rationale"] and "cannot calibrate the weight tolerance" in rec["weight_pct"]["rationale"]
    assert all("scale noise" not in v["rationale"] for k, v in rec.items() if k != "weight_pct")


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
