"""The dashboard's numbers and sentences come from output files and nothing else.

Tiny hand-built output frames make every headline number checkable by eye; one test runs the same
functions on the real outputs/ to make sure they still fit the files the pipeline writes.
"""

import pandas as pd
import pytest

from freight_audit_lab.dashboard import (accrual_finding_text, demote_headings, key_findings, load_outputs, md_escape, outputs_ready,
                                         overview_metrics, pct, recoverable_by_carrier, recoverable_by_error_type,
                                         strongest_systemic_carrier, systemic_finding_text, trap_finding_text, usd)

NAMES = {"CARA": "Carrier A Freight", "CARF": "Carrier F Trucking"}


@pytest.fixture
def small():
    """Outputs for 4 invoices: $1,000 billed on CARA, $3,000 on CARF; recoverable $100 and $200."""
    ts = pd.Timestamp
    return {
        "audit_invoice_summary": pd.DataFrame({
            "carrier_id": ["CARA", "CARA", "CARF", "CARF"], "total": [400.0, 600.0, 1000.0, 2000.0],
            "n_flags": [0, 1, 2, 0], "recoverable_estimate": [0.0, 100.0, 200.0, 0.0]}),
        "eval_engine_vs_baseline": pd.DataFrame({
            "error_type": ["duplicate_invoice", "rate_overcharge", "ALL"],
            "engine_precision": [1.0, 0.5, 0.75], "baseline_precision": [0.4, 0.3, 0.35],
            "engine_recall": [1.0, 0.8, 0.9], "baseline_recall": [1.0, 0.8, 0.9],
            "engine_fp": [0, 1, 1], "baseline_fp": [5, 4, 9]}),
        "eval_by_type": pd.DataFrame({
            "system": ["engine"] * 3 + ["baseline"] * 3, "error_type": ["duplicate_invoice", "rate_overcharge", "ALL"] * 2,
            "flagged_dollars_estimate": [100.0, 200.0, 300.0, 5.0, 6.0, 11.0]}),
        "eval_traps": pd.DataFrame({
            "trap": ["rebill", "rate_amendment", "(none: clean invoice)"], "n_invoices": [100, 1000, 5000],
            "engine_fp": [0, 2, 0], "baseline_fp": [106, 622, 0],
            "baseline_fp_by_type": ["duplicate_invoice:100;rate_overcharge:6", "rate_overcharge:554;weight_overbilling:68", None]}),
        "baseline_fp_causes": pd.DataFrame({
            "cause": ["rate_amendment", "late_authorization", "other"],
            "description": ["rate amendment", "late authorization", "other"],
            "false_flags": [554, 300, 46], "share_of_false_flags": [0.6156, 0.3333, 0.0511]}),
        "systemic_findings": pd.DataFrame({
            "carrier_id": ["CARA", "CARF"], "error_type": ["weight_overbilling", "fsc_mismatch"],
            "window_start": [ts("2025-01-01"), ts("2025-08-01")], "window_end": [ts("2025-03-31"), ts("2025-10-31")],
            "invoices": [346, 182], "flagged": [16, 178], "carrier_rate": [0.0462, 0.978], "other_carriers_rate": [0.013, 0.0145],
            "mode": ["LTL", "TL"], "p_value": [1.6e-05, 1e-320], "avg_billed_over_expected": [0.2, 0.0909]}),
        "accrual_accuracy": pd.DataFrame({
            "month_end": ["2025-01-31", "2025-02-28", "ALL"], "error_pct_estimate": [-0.01, 0.03, -0.001],
            "error_estimate": [-10.0, 30.0, -100.0], "accessorial_error_estimate": [-4.0, 10.0, -60.0],
            "accessorial_accrual_estimate": [50.0, 50.0, 100.0], "accrual_estimate": [1000.0, 1000.0, 2000.0]}),
        "accrual_sensitivity": pd.DataFrame({
            "month_end": ["2025-01-31", "2025-02-28", "ALL"], "late_auth_effect_estimate": [10.0, 30.0, 40.0]}),
    }


def test_overview_metrics_are_sums_and_differences_of_the_output_files(small):
    m = overview_metrics(small)
    assert m["invoices_audited"] == 4 and m["billed_spend"] == 4000.0 and m["invoices_flagged"] == 2
    assert m["recoverable_estimate"] == 300.0 and m["recoverable_pct_of_spend_estimate"] == pytest.approx(0.075)
    assert (m["engine_precision"], m["baseline_precision"]) == (0.75, 0.35)
    assert m["false_disputes_avoided"] == 9 - 1


def test_recoverable_by_carrier_and_type_are_sorted_and_named(small):
    by_carrier = recoverable_by_carrier(small, NAMES)
    assert list(by_carrier["carrier"]) == ["Carrier F Trucking", "Carrier A Freight"]
    assert list(by_carrier["recoverable_estimate"]) == [200.0, 100.0]
    by_type = recoverable_by_error_type(small)
    assert list(by_type["label"]) == ["Rate overcharge", "Duplicate invoice"] and by_type["recoverable_estimate"].sum() == 300.0


def test_systemic_bullet_picks_the_smallest_p_value_and_mentions_the_others(small):
    text = systemic_finding_text(small, NAMES)
    assert "Carrier F Trucking (CARF)" in text and "178 of 182 invoices (97.8%)" in text
    assert "Aug 2025 to Oct 2025" in text and "1.5% at the other TL carriers (p < 1e-300)" in text
    assert "9.1% above expected" in text and "1 weaker pattern also flagged" in text
    assert "No systemic" in systemic_finding_text(dict(small, systemic_findings=small["systemic_findings"].iloc[0:0]), NAMES)


def test_false_flag_bullet_names_the_top_cause_out_of_the_additive_total_and_never_names_other(small):
    text = trap_finding_text(small)
    assert "Rate amendment caused the most baseline false flags" in text and "554 of its 900 false flags (62%)" in text
    assert "ignores effective dates" in text and "The engine raised 1 false flag in total." in text
    small["baseline_fp_causes"].loc[2, "false_flags"] = 5000                    # "other" larger than every named cause
    assert "Rate amendment caused" in trap_finding_text(small)


def test_accrual_bullet_shares_are_of_the_net_error(small):
    """Net error -$100: accessorials are -$60 (60%), authorizations recorded late explain +$40 (40%). Accessorials
    are $100 of a $2,000 accrual (5.0%). Mean absolute monthly error is (1% + 3%) / 2 = 2.00%."""
    text = accrual_finding_text(small)
    assert "0.10% below eventual payable" in text and "mean absolute monthly error 2.00%" in text
    assert "5.0% of the accrual but 60% of the net shortfall" in text and "explain 40% of the net shortfall" in text


def test_dispute_viewer_opens_on_the_carrier_with_the_smallest_p_value(small):
    assert strongest_systemic_carrier(small) == "CARF"                     # p 1e-320 beats 1.6e-05, though CARA is listed first
    assert strongest_systemic_carrier(dict(small, systemic_findings=small["systemic_findings"].iloc[0:0])) is None


def test_demote_headings_keeps_the_text_and_leaves_other_hashes_alone():
    md = "# Dispute summary: Carrier F (CARF)\n\nPeriod: x.\n\n## By error type\n\n1. **0600346** (BOL#123)"
    assert demote_headings(md) == "**Dispute summary: Carrier F (CARF)**\n\nPeriod: x.\n\n**By error type**\n\n1. **0600346** (BOL#123)"


def test_formatting_helpers():
    assert usd(12033604.67) == "$12,033,605" and usd(1234.567, cents=True) == "$1,234.57"
    assert pct(0.9991) == "99.9%" and pct(0.00244, 2) == "0.24%"
    assert md_escape("$1.00 and $2.00") == r"\$1.00 and \$2.00"


def test_real_outputs_feed_every_function():
    """The functions must fit the files the pipeline really writes, and the bullets must say what the files say."""
    assert outputs_ready()
    o = load_outputs()
    names = {c: c for c in o["audit_invoice_summary"]["carrier_id"].unique()}
    m = overview_metrics(o)
    assert m["false_disputes_avoided"] > 0 and m["engine_precision"] > m["baseline_precision"]
    assert m["recoverable_estimate"] == pytest.approx(recoverable_by_error_type(o)["recoverable_estimate"].sum(), abs=0.01)
    assert m["recoverable_estimate"] == pytest.approx(recoverable_by_carrier(o, names)["recoverable_estimate"].sum(), abs=0.01)
    bullets = key_findings(o, names)
    assert len(bullets) == 3 and all(b.startswith("**") for b in bullets)
    assert "CARF" in bullets[0]
