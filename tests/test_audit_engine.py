"""Engine (recoverable dollars, outputs) and baseline (the shortcuts that make it wrong)."""

import pandas as pd
import pytest

from freight_audit_lab.audit.baseline import run_baseline
from freight_audit_lab.audit.engine import apply_recoverable, audit, write_audit
from freight_audit_lab.csv_io import read_csv
from tests.fixtures import audit_inputs, build_norm, invoice, make_cfg, make_ref

CENT = 0.01


def run_engine(specs, **kwargs):
    norm, rr, ref, cfg = audit_inputs(specs, **kwargs)
    return audit(norm, rr, ref, cfg)


def recoverable(result):
    return result["invoice_summary"].set_index("invoice_id")["recoverable_estimate"]


# ---------------------------------------------------------------- engine


def test_duplicate_that_also_has_a_rate_error_counts_once_at_its_total():
    """Both copies carry a $20 linehaul markup (total $239.80). The original is a rate error worth
    $21.80; the duplicate is worth its whole $239.80, and its rate flag is kept but not counted."""
    bad = dict(lh=220.00, fsc=19.80)
    result = run_engine([invoice("1", "S1", **bad), invoice("2", "S1", received="2025-03-28", **bad)])
    rec = recoverable(result)
    assert rec["CARA:1"] == pytest.approx(21.80, abs=CENT)
    assert rec["CARA:2"] == pytest.approx(239.80, abs=CENT)
    flags = result["flags"].set_index(["invoice_id", "error_type"])["counted_in_recoverable"]
    assert flags[("CARA:2", "duplicate_invoice")] and not flags[("CARA:2", "rate_overcharge")]
    assert flags[("CARA:1", "rate_overcharge")]


def test_a_phantom_counts_at_its_total_and_nothing_else():
    assert recoverable(run_engine([invoice("1", "S1", match="unmatched")]))["CARA:1"] == pytest.approx(218.00, abs=CENT)


def test_weight_and_rate_impacts_add_up_to_the_whole_overcharge():
    """Billed 1,200 lb with a 5% markup: weight $43.60 + rate $13.08 = $56.68, with no fuel flag on top."""
    result = run_engine([invoice("1", "S1", weight=1200, lh=252.00, fsc=22.68)])
    assert sorted(result["flags"]["error_type"]) == ["rate_overcharge", "weight_overbilling"]
    assert recoverable(result)["CARA:1"] == pytest.approx(56.68, abs=CENT)


def test_recoverable_is_capped_at_the_invoice_total():
    flags = pd.DataFrame({"invoice_id": ["X", "X"], "error_type": ["rate_overcharge", "weight_overbilling"],
                          "dollar_impact_estimate": [70.0, 50.0]})
    invoices = pd.DataFrame({"invoice_id": ["X"], "total": [100.0]})
    _, rec = apply_recoverable(flags, invoices)
    assert rec["X"] == 100.0


def test_superseded_invoices_are_neither_flagged_nor_summarized():
    result = run_engine([invoice("1", "S1", lh=300.00, superseded=True), invoice("2", "S1", itype="rebill")])
    assert result["flags"].empty and list(result["invoice_summary"]["invoice_id"]) == ["CARA:2"]


def test_a_rebill_is_audited_like_an_original():
    result = run_engine([invoice("1", "S1", superseded=True), invoice("2", "S1", itype="rebill", lh=220.00, fsc=19.80)])
    assert list(result["flags"]["invoice_id"]) == ["CARA:2"] and list(result["flags"]["error_type"]) == ["rate_overcharge"]


def test_balance_due_only_goes_through_accessorial_and_phantom_checks():
    """A balance-due invoice has no linehaul or fuel line and the same shipment as its parent: none of the
    duplicate, rate, fuel, or weight rules may touch it, but its accessorial is still checked."""
    specs = [invoice("1", "S1"),
             invoice("2", "S1", lh=None, fsc=None, acc=[("LIFTGATE", 95.0)], itype="balance_due", received="2025-04-01")]
    result = run_engine(specs)
    assert list(result["flags"]["error_type"]) == ["unauthorized_accessorial"]
    assert list(result["flags"]["invoice_id"]) == ["CARA:2"]


def test_process_metric_counts_accessorials_authorized_after_the_invoice_date():
    specs = [invoice("1", "S5", acc=[("LIFTGATE", 95.0)]),          # authorized 10 days after the invoice date
             invoice("2", "S1", acc=[("RESIDENTIAL", 125.0)]),      # authorized before
             invoice("3", "S1", acc=[("LIFTGATE", 95.0)])]          # never authorized
    metrics = run_engine(specs)["process_metrics"].set_index("metric")["value"]
    assert metrics["accessorial_lines_audited"] == 3
    assert metrics["accessorials_authorized_after_invoice_date"] == 1
    assert metrics["accessorials_unauthorized"] == 1


def test_outputs_are_written(tmp_path):
    result = run_engine([invoice("1", "S1", lh=220.00, fsc=19.80)])
    write_audit(result, tmp_path)
    assert {p.name for p in tmp_path.iterdir()} == {"audit_flags.csv", "audit_invoice_summary.csv",
                                                    "audit_process_metrics.csv"}
    flags = read_csv(tmp_path / "audit_flags.csv")
    assert {"invoice_id", "error_type", "reason", "dollar_impact_estimate"} <= set(flags.columns)


# ---------------------------------------------------------------- baseline: each shortcut vs the engine


def both(specs, cfg=None, extra_auths=()):
    """(engine flags, baseline flags) as {(invoice_id, error_type)} sets."""
    norm, rr, ref, cfg = audit_inputs(specs, cfg, extra_auths)
    eng = audit(norm, rr, ref, cfg)["flags"]
    base = run_baseline(norm, ref, cfg)["flags"]
    return set(zip(eng.invoice_id, eng.error_type)), set(zip(base.invoice_id, base.error_type))


def test_baseline_calls_a_rebill_and_a_balance_due_duplicates_but_the_engine_does_not():
    specs = [invoice("1", "S5", acc=[("LIFTGATE", 95.0)], superseded=True),
             invoice("2", "S5", acc=[("LIFTGATE", 95.0)], itype="rebill", received="2025-04-01"),
             invoice("3", "S5", lh=None, fsc=None, acc=[("LIFTGATE", 95.0)], itype="balance_due", received="2025-04-05")]
    eng, base = both(specs)
    assert not {f for f in eng if f[1] == "duplicate_invoice"}
    assert {("CARA:2", "duplicate_invoice"), ("CARA:3", "duplicate_invoice")} <= base


def test_baseline_still_catches_a_real_duplicate():
    eng, base = both([invoice("1", "S1"), invoice("2", "S1", received="2025-03-28")])
    assert ("CARA:2", "duplicate_invoice") in eng and ("CARA:2", "duplicate_invoice") in base


def test_baseline_calls_bol_noise_a_phantom_but_the_engine_does_not():
    eng, base = both([invoice("1", "S1", bol="BOL#00000001")])
    assert eng == set() and ("CARA:1", "phantom_invoice") in base


def test_baseline_flags_a_correct_rate_after_an_upward_amendment():
    """S7's lane went up 10% in July: the invoice is right, but the first rate row says it is $20 high."""
    eng, base = both([invoice("1", "S7")])
    assert eng == set() and ("CARA:1", "rate_overcharge") in base


def test_baseline_flags_a_documented_reweigh_as_weight_overbilling():
    eng, base = both([invoice("1", "S3", weight=1100)])
    assert eng == set() and ("CARA:1", "weight_overbilling") in base


def test_baseline_flags_a_late_authorization():
    eng, base = both([invoice("1", "S5", acc=[("LIFTGATE", 95.0)])])
    assert eng == set() and ("CARA:1", "unauthorized_accessorial") in base


def test_baseline_uses_the_same_tolerances_as_the_engine():
    """A $10 markup on $200 linehaul is caught by both at the default 1%, by neither once rate_pct is 10%
    and the dollar floor is $15."""
    specs = [invoice("1", "S1", lh=210.00, fsc=18.90)]
    assert both(specs)[0] == both(specs)[1] == {("CARA:1", "rate_overcharge")}
    loose = make_cfg(rate_pct=0.10, rate_abs=15.0)
    assert both(specs, loose) == (set(), set())


def test_baseline_recoverable_uses_the_same_counting_rule():
    norm, rr, ref, cfg = audit_inputs([invoice("1", "S1", match="unmatched", bol="99999999"),
                                             invoice("2", "S1", lh=220.00, fsc=19.80)])
    rec = run_baseline(norm, ref, cfg)["recoverable"]
    assert rec["CARA:1"] == pytest.approx(218.00, abs=CENT) and rec["CARA:2"] == pytest.approx(21.80, abs=CENT)
