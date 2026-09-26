"""Exception queue ranking and the dispute summaries, including the systemic pattern check."""

import numpy as np
import pandas as pd
import pytest

from freight_audit_lab.audit.engine import audit
from freight_audit_lab.exceptions import (binomial_sf, build_disputes, build_exception_queue, systemic_findings,
                                          systemic_line, write_exceptions)
from tests.fixtures import audit_inputs, invoice, make_cfg

CENT = 0.01
TS = pd.Timestamp


def queue_for(specs, **kwargs):
    norm, rr, ref, cfg = audit_inputs(specs, **kwargs)
    result = audit(norm, rr, ref, cfg)
    return build_exception_queue(norm, result["flags"], result["invoice_summary"], cfg), norm, result, cfg


def test_queue_is_ranked_by_recoverable_dollars_then_oldest_first():
    specs = [invoice("1", "S1", lh=220.00, fsc=19.80, received="2025-03-20"),         # rate error, $21.80
             invoice("2", "S2", match="unmatched", received="2025-03-25"),            # phantom, its $196.20 total
             invoice("3", "S6", lh=660.00, fsc=59.40, received="2025-03-10"),         # rate error, $65.40
             invoice("4", "S6", lh=630.00, fsc=56.70, received="2025-03-02"),         # rate error, $32.70
             invoice("5", "S5", lh=220.00, fsc=19.80, received="2025-03-12")]         # rate error, $21.80, older than #1
    queue, *_ = queue_for(specs)
    assert list(queue["invoice_id"]) == ["CARA:2", "CARA:3", "CARA:4", "CARA:5", "CARA:1"]
    assert list(queue["rank"]) == [1, 2, 3, 4, 5] and set(queue["status"]) == {"open"}


def test_queue_row_has_the_dates_days_open_and_reference_numbers():
    queue, *_ = queue_for([invoice("1", "S1", lh=220.00, fsc=19.80, received="2025-03-18")])
    row = queue.iloc[0]
    assert (row.carrier_id, row.invoice_number, row.pro_number, row.bol) == ("CARA", "N1", "PRO1", "00000001")
    assert row.days_open == (TS("2026-03-31") - TS("2025-03-18")).days          # as of audit_as_of
    assert row.error_type == "rate_overcharge" and "Linehaul $220.00" in row.reason
    assert row.recoverable_estimate == pytest.approx(21.80, abs=CENT)


def test_an_invoice_with_several_flags_is_one_row_with_deduplicated_recoverable_dollars():
    """Both copies carry a rate error. The duplicate is worth its whole total ($239.80), not total + rate impact."""
    bad = dict(lh=220.00, fsc=19.80)
    queue, *_ = queue_for([invoice("1", "S1", **bad), invoice("2", "S1", received="2025-03-28", **bad)])
    dup = queue[queue["invoice_id"] == "CARA:2"].iloc[0]
    assert len(queue) == 2 and dup.n_flags == 2 and dup.error_type == "duplicate_invoice;rate_overcharge"
    assert dup.recoverable_estimate == pytest.approx(239.80, abs=CENT)
    assert dup.dollar_impact_estimate == pytest.approx(239.80 + 21.80, abs=CENT)     # gross, before de-duplication


def test_superseded_and_clean_invoices_never_reach_the_queue():
    queue, *_ = queue_for([invoice("1", "S1", lh=300.00, superseded=True), invoice("2", "S1", itype="rebill")])
    assert queue.empty


# ---------------------------------------------------------------- systemic pattern check


def world(flag_rate_by_carrier_month):
    """Invoices and flags for CARA/CARB/CARC (all LTL), 30 invoices a month for 12 months.
    `flag_rate_by_carrier_month(carrier, month)` gives how many of the 30 are flagged as rate_overcharge."""
    rows, flagged = [], []
    for carrier in ["CARA", "CARB", "CARC"]:
        for month in range(1, 13):
            for k in range(30):
                iid = f"{carrier}:{month}:{k}"
                rows.append({"invoice_id": iid, "carrier_id": carrier, "ship_date": TS(2025, month, 10), "is_superseded": False})
                if k < flag_rate_by_carrier_month(carrier, month):
                    flagged.append({"invoice_id": iid, "error_type": "rate_overcharge", "billed_value": 110.0,
                                    "expected_value": 100.0, "reason": "x" * 30, "dollar_impact_estimate": 10.0,
                                    "counted_in_recoverable": True})
    return {"invoices": pd.DataFrame(rows)}, pd.DataFrame(flagged)


def test_systemic_check_fires_on_a_concentrated_pattern():
    """CARA's rate flags jump to 60% for Aug-Oct; everyone else stays at ~3%."""
    norm, flags = world(lambda c, m: 18 if c == "CARA" and 8 <= m <= 10 else 1)
    found = systemic_findings(norm, flags, make_cfg())
    assert list(found["carrier_id"]) == ["CARA"] and list(found["error_type"]) == ["rate_overcharge"]
    row = found.iloc[0]
    assert (row.window_start, row.window_end) == (TS("2025-08-01"), TS("2025-10-31"))
    assert row.flagged == 54 and row.invoices == 90 and row.carrier_rate == pytest.approx(0.6)
    assert row.other_carriers_rate == pytest.approx(1 / 30)
    line = systemic_line(row)
    assert "60% of invoices shipped Aug-Oct 2025 (54 of 90)" in line and "3% across other LTL carriers" in line
    assert "10.0% above expected" in line and "one-sided binomial p = " in line and "Bonferroni" in line
    assert row.p_value < row.alpha_adjusted and row.alpha_adjusted == pytest.approx(0.01 / (3 * 6 * 10))   # 3 carriers x 6 types x 10 windows


def test_systemic_check_stays_quiet_when_flags_are_uniform_or_only_mildly_higher():
    uniform, flags = world(lambda c, m: 2)
    assert systemic_findings(uniform, flags, make_cfg()).empty
    mild, flags = world(lambda c, m: 4 if c == "CARA" else 2)          # 2x the others, under the 3x rule
    assert systemic_findings(mild, flags, make_cfg()).empty


def test_binomial_tail_matches_hand_calculations():
    assert binomial_sf(2, 3, 0.5) == pytest.approx(0.5)                    # (3 + 1) / 8
    assert binomial_sf(10, 10, 0.5) == pytest.approx(1 / 1024)
    assert binomial_sf(1, 4, 0.25) == pytest.approx(1 - 0.75 ** 4)
    assert binomial_sf(0, 50, 0.1) == 1.0 and binomial_sf(5, 50, 0.0) == 0.0 and binomial_sf(5, 50, 1.0) == 1.0
    assert 0.0 <= binomial_sf(300, 400, 0.01) < 1e-300 or binomial_sf(300, 400, 0.01) == 0.0    # no overflow at large n


def test_a_chance_looking_window_is_suppressed_by_the_significance_test():
    """CARA has 10 flags of 90 (11%) in May-Jul vs peers' 3.3%: over 3x and at the minimum count, and unlikely
    at the 1% level (p = 0.0009) taken alone, but with 180 tests run that is not unusual enough. It fires only
    if the significance threshold is relaxed."""
    norm, flags = world(lambda c, m: {5: 4, 6: 3, 7: 3}.get(m, 1) if c == "CARA" else 1)
    assert systemic_findings(norm, flags, make_cfg()).empty
    relaxed = make_cfg()
    relaxed["evaluation"]["systemic"]["alpha"] = 1e6
    row = systemic_findings(norm, flags, relaxed).iloc[0]
    assert row.carrier_id == "CARA" and row.flagged == 10 and row.carrier_rate >= 3 * row.other_carriers_rate
    assert 0.01 / 180 < row.p_value < 0.01                 # significant at 0.01 unadjusted, not after Bonferroni


def test_systemic_check_ignores_a_tiny_sample():
    """One bad month with 3 flagged invoices is 10% vs 0%, but 3 flags is under the minimum."""
    norm, flags = world(lambda c, m: 3 if c == "CARA" and m == 6 else 0)
    assert systemic_findings(norm, flags, make_cfg()).empty


def test_dispute_summary_lists_the_systemic_line_and_only_that_carriers_invoices(tmp_path):
    norm, flags = world(lambda c, m: 18 if c == "CARA" and 8 <= m <= 10 else 1)
    flags["reason"] = "Linehaul $110.00 vs contract $100.00"
    norm["invoices"] = norm["invoices"].assign(invoice_number=lambda d: d["invoice_id"], pro_number="P", bol_raw="B",
                                               received_date=TS("2025-11-01"), total=110.0)
    summary = pd.DataFrame({"invoice_id": flags["invoice_id"], "recoverable_estimate": 10.0})
    cfg = make_cfg()
    queue = build_exception_queue(norm, flags, summary, cfg)
    disputes, findings = build_disputes(norm, flags, queue, cfg)
    text, table = disputes["CARA"]
    assert "Possible systemic issue" in text and "Top 10 invoices" in text and text.count("\n1. ") == 1
    assert len(table) == (queue["carrier_id"] == "CARA").sum() and set(table.columns) >= {"rank", "reason"}
    assert "Possible systemic issue" not in disputes["CARB"][0]
    assert "No exceptions found" in disputes["CARD"][0] and "no invoices audited" in disputes["CARD"][0]
    write_exceptions(queue, disputes, findings, tmp_path)
    assert (tmp_path / "exception_queue.csv").exists() and (tmp_path / "disputes" / "CARA.md").exists()
    assert len(list((tmp_path / "disputes").glob("*.md"))) == 8 == len(list((tmp_path / "disputes").glob("*.csv")))


def test_full_data_flags_the_injected_fuel_problem_at_carf(audited, cfg):
    norm = {"invoices": audited["invoices"]}
    found = systemic_findings(norm, audited["audit_flags"], cfg)
    carf = found[(found["carrier_id"] == "CARF") & (found["error_type"] == "fsc_mismatch")].iloc[0]
    assert carf.window_start == TS("2025-08-01") and carf.window_end == TS("2025-10-31") and carf.carrier_rate > 0.9
