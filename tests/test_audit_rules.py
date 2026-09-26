"""Each audit rule on hand-built invoices: (a) catches a clear error, (b) ignores its matching trap,
(c) the tolerance boundary works (just under passes, just over is flagged), (d) the impact is right
to the cent. See tests/fixtures.py for the prices behind every number."""

import pandas as pd
import pytest

from freight_audit_lab.audit.rules import (duplicate_invoice, fsc_mismatch, phantom_invoice, rate_overcharge,
                                           unauthorized_accessorial, weight_overbilling)
from tests.fixtures import audit_inputs, invoice, make_cfg

CENT = 0.01


def flagged(rule, specs, **kwargs):
    """{invoice_id: flag row} for a rule run on invoice specs."""
    flags = rule(*audit_inputs(specs, **kwargs))
    return {r.invoice_id: r for r in flags.itertuples(index=False)}


# ------------------------------------------------------------------ duplicate_invoice


def test_duplicate_flags_the_later_copy_at_its_full_total():
    hits = flagged(duplicate_invoice, [invoice("1", "S1", received="2025-03-18"),
                                       invoice("2", "S1", received="2025-03-28")])
    assert list(hits) == ["CARA:2"]
    assert hits["CARA:2"].dollar_impact_estimate == pytest.approx(218.00, abs=CENT)
    assert hits["CARA:2"].related_invoice_id == "CARA:1"
    assert "10 days" in hits["CARA:2"].reason


def test_duplicate_ignores_rebills_and_balance_due():
    """A rebill replaces its (superseded) parent and here matches its amount exactly; a balance-due bills a
    different charge on the same shipment and here is contrived to total exactly the parent's $218.00.
    Neither is a duplicate."""
    hits = flagged(duplicate_invoice, [
        invoice("1", "S1", superseded=True),
        invoice("2", "S1", itype="rebill", received="2025-04-01"),
        invoice("3", "S5"),
        invoice("4", "S5", lh=None, fsc=None, acc=[("DETENTION", 218.00)], itype="balance_due", received="2025-04-05")])
    assert hits == {}


def test_duplicate_ignores_a_superseded_copy():
    hits = flagged(duplicate_invoice, [invoice("1", "S1"), invoice("2", "S1", superseded=True, received="2025-03-25")])
    assert hits == {}


@pytest.mark.parametrize("total_gap, expect", [(0.99, True), (1.00, True), (1.01, False)])
def test_duplicate_amount_tolerance_boundary(total_gap, expect):
    hits = flagged(duplicate_invoice, [invoice("1", "S1", fsc=18.00),
                                       invoice("2", "S1", fsc=round(18.00 + total_gap, 2), received="2025-03-28")])
    assert ("CARA:2" in hits) == expect


@pytest.mark.parametrize("days_apart, expect", [(120, True), (121, False)])
def test_duplicate_window_boundary(days_apart, expect):
    later = (pd.Timestamp("2025-03-18") + pd.Timedelta(days=days_apart)).strftime("%Y-%m-%d")
    hits = flagged(duplicate_invoice, [invoice("1", "S1"), invoice("2", "S1", received=later)])
    assert ("CARA:2" in hits) == expect


def test_duplicate_same_invoice_number_is_flagged_even_for_different_freight():
    hits = flagged(duplicate_invoice, [invoice("1", "S1", number="N100"),
                                       invoice("2", "S6", number="N100", received="2025-03-25")])
    assert list(hits) == ["CARA:2"] and "invoice number" in hits["CARA:2"].reason


def test_duplicate_groups_unmatched_invoices_by_carrier_and_bol():
    hits = flagged(duplicate_invoice, [invoice("1", "S1", match="unmatched", bol="99999999"),
                                       invoice("2", "S1", match="unmatched", bol="99999999", received="2025-03-30")])
    assert list(hits) == ["CARA:2"]


# ------------------------------------------------------------------ phantom_invoice


def test_phantom_flags_an_unmatched_invoice_at_its_total():
    hits = flagged(phantom_invoice, [invoice("1", "S1", match="unmatched")])
    assert hits["CARA:1"].dollar_impact_estimate == pytest.approx(218.00, abs=CENT)
    assert hits["CARA:1"].shipment_id == ""


def test_phantom_ignores_bol_noise_fallback_matches_and_superseded_invoices():
    hits = flagged(phantom_invoice, [
        invoice("1", "S1", bol="BOL#00000001"),                    # noisy text, matched after canonicalizing
        invoice("2", "S1", match="fallback", bol="00000010"),      # transposed BOL rescued by the fallback match
        invoice("3", "S1", match="unmatched", superseded=True)])
    assert hits == {}


# ------------------------------------------------------------------ rate_overcharge


def test_rate_overcharge_flags_a_markup_with_the_numbers_in_the_reason():
    hits = flagged(rate_overcharge, [invoice("1", "S1", lh=220.00, fsc=19.80)])
    hit = hits["CARA:1"]
    assert hit.dollar_impact_estimate == pytest.approx(21.80, abs=CENT)      # 20.00 x 1.09 fuel knock-on
    assert "Linehaul $220.00 vs contract $200.00" in hit.reason
    assert "Chicago → Atlanta" in hit.reason and "rate effective 2025-01-01" in hit.reason
    assert "+$20.00 (+10.0%)" in hit.reason


def test_rate_overcharge_stale_rate_is_flagged_and_the_amendment_trap_is_not():
    """S2 shipped after the July cut. Billing the old $200 is an overcharge; billing the new $180 is not.
    S7 shipped after an increase: the correct new rate is above the old one and must not be flagged."""
    hits = flagged(rate_overcharge, [invoice("1", "S2", lh=200.00, fsc=18.00),
                                     invoice("2", "S2"),
                                     invoice("3", "S7")])
    assert list(hits) == ["CARA:1"]
    assert "rate effective 2025-07-01" in hits["CARA:1"].reason
    assert hits["CARA:1"].dollar_impact_estimate == pytest.approx(21.80, abs=CENT)


def test_rate_overcharge_ignores_rounding_noise_and_documented_reweigh():
    hits = flagged(rate_overcharge, [invoice("1", "S1", lh=200.40),          # +0.2% carrier rounding
                                     invoice("2", "S3", weight=1100)])      # certified reweigh, contract LH at 1,100 lb
    assert hits == {}


@pytest.mark.parametrize("billed, expect", [(202.00, False), (202.01, True)])
def test_rate_dollar_floor_boundary(billed, expect):
    """LH $200: 1% is $2.00, equal to the $2.00 floor. The gap must strictly exceed it."""
    assert ("CARA:1" in flagged(rate_overcharge, [invoice("1", "S1", lh=billed)])) == expect


@pytest.mark.parametrize("billed, expect", [(1515.00, False), (1515.01, True)])
def test_rate_percentage_boundary_and_tl_impact(billed, expect):
    """TL LH $1,500: 1% is $15.00, above the floor. TL has no fuel knock-on, so impact = billed - contract."""
    hits = flagged(rate_overcharge, [invoice("1", "S4", lh=billed)])
    assert ("CARF:1" in hits) == expect
    if expect:
        assert hits["CARF:1"].dollar_impact_estimate == pytest.approx(15.01, abs=CENT)


def test_rate_tolerance_comes_from_config():
    assert "CARA:1" in flagged(rate_overcharge, [invoice("1", "S1", lh=203.00)])
    assert "CARA:1" not in flagged(rate_overcharge, [invoice("1", "S1", lh=203.00)],
                                   cfg=make_cfg(rate_pct=0.05, rate_abs=10.0))


# ------------------------------------------------------------------ fsc_mismatch


def test_fsc_ltl_flags_extra_steps_and_reports_percentages():
    """Two extra 0.9-point steps: 11.0% of $200 = $22.00 vs $18.00 expected."""
    hit = flagged(fsc_mismatch, [invoice("1", "S1", fsc=22.00)])["CARA:1"]
    assert hit.dollar_impact_estimate == pytest.approx(4.00, abs=CENT)
    assert "$22.00 vs expected $18.00" in hit.reason and "11.0% of linehaul vs expected 9.0%" in hit.reason


def test_fsc_is_measured_on_the_billed_linehaul_so_a_rate_error_is_not_counted_twice():
    """LH overcharged to $220 with FSC a correct 9% of that ($19.80): a rate problem, not an FSC problem."""
    assert flagged(fsc_mismatch, [invoice("1", "S1", lh=220.00, fsc=19.80)]) == {}


def test_fsc_ignores_rounding_noise():
    assert flagged(fsc_mismatch, [invoice("1", "S1", fsc=18.05), invoice("2", "S4", fsc=96.50)]) == {}


@pytest.mark.parametrize("fsc, expect", [(55.50, False), (55.51, True)])
def test_fsc_ltl_percentage_point_boundary(fsc, expect):
    """S6 (LH $600, expected 9.0% = $54.00): 55.50 is exactly 0.25 points over, and must be strictly more."""
    assert ("CARA:1" in flagged(fsc_mismatch, [invoice("1", "S6", fsc=fsc)])) == expect


def test_fsc_ltl_dollar_floor():
    """Over the point tolerance (0.26 pp) but only $0.52 on a $200 linehaul: below the $1.00 floor."""
    assert flagged(fsc_mismatch, [invoice("1", "S1", fsc=18.52)]) == {}
    assert "CARA:1" in flagged(fsc_mismatch, [invoice("1", "S1", fsc=19.20)])       # +$1.20, 0.6 pp


@pytest.mark.parametrize("fsc, expect", [(97.00, False), (97.01, True)])
def test_fsc_tl_dollar_boundary(fsc, expect):
    hits = flagged(fsc_mismatch, [invoice("1", "S4", fsc=fsc)])
    assert ("CARF:1" in hits) == expect
    if expect:
        assert hits["CARF:1"].dollar_impact_estimate == pytest.approx(2.01, abs=CENT)


@pytest.mark.parametrize("fsc, expect", [(99.75, False), (99.76, True)])
def test_fsc_tl_percentage_tolerance_scales_with_the_fuel_line(fsc, expect):
    """With fsc_tl_pct at 5% the allowance on $95.00 expected fuel is $4.75, above the $2.00 floor:
    a $4.75 gap passes and $4.76 is flagged."""
    hits = flagged(fsc_mismatch, [invoice("1", "S4", fsc=fsc)], cfg=make_cfg(fsc_tl_pct=0.05))
    assert ("CARF:1" in hits) == expect


def test_fsc_tl_dollar_floor_wins_when_the_percentage_allowance_is_smaller():
    """At the default 0.5% the allowance on $95.00 is $0.48, below the $2.00 floor, so the floor decides."""
    assert flagged(fsc_mismatch, [invoice("1", "S4", fsc=96.90)]) == {}
    assert "CARF:1" in flagged(fsc_mismatch, [invoice("1", "S4", fsc=97.10)])


def test_fsc_tl_percentage_does_not_change_the_ltl_check():
    """LTL still uses percentage points and the $1.00 floor: fsc_tl_pct is a TL setting."""
    loose = make_cfg(fsc_tl_pct=0.50)
    assert "CARA:1" in flagged(fsc_mismatch, [invoice("1", "S1", fsc=22.00)], cfg=loose)


def test_fsc_tl_wrong_mpg_table_is_flagged():
    """Fuel billed at 5.5 mpg instead of 6.0: 0.95 / 5.5 x 600 = $103.64 vs $95.00."""
    hit = flagged(fsc_mismatch, [invoice("1", "S4", fsc=103.64)])["CARF:1"]
    assert hit.dollar_impact_estimate == pytest.approx(8.64, abs=CENT)
    assert "/mi" in hit.reason and "600 mi" in hit.reason


# ------------------------------------------------------------------ unauthorized_accessorial


def test_unauthorized_accessorial_flags_a_charge_with_no_authorization():
    hits = flagged(unauthorized_accessorial, [invoice("1", "S1", acc=[("LIFTGATE", 95.0)])])
    assert hits["CARA:1"].dollar_impact_estimate == pytest.approx(95.00, abs=CENT)
    assert "Liftgate $95.00" in hits["CARA:1"].reason


def test_late_authorization_is_not_an_error_but_one_after_the_audit_date_is():
    """S5's liftgate was authorized ten days after the invoice date, before audit_as_of: legitimate."""
    assert flagged(unauthorized_accessorial, [invoice("1", "S5", acc=[("LIFTGATE", 95.0)])]) == {}
    hits = flagged(unauthorized_accessorial, [invoice("1", "S1", acc=[("LIFTGATE", 95.0)])],
                   extra_auths=[("S1", "LIFTGATE", 95.0, "2026-04-01")])
    assert "CARA:1" in hits


def test_balance_due_accessorial_is_checked_like_any_other():
    ok = flagged(unauthorized_accessorial, [invoice("1", "S5", lh=None, fsc=None, acc=[("LIFTGATE", 95.0)],
                                                    itype="balance_due")])
    bad = flagged(unauthorized_accessorial, [invoice("2", "S1", lh=None, fsc=None, acc=[("LIFTGATE", 95.0)],
                                                     itype="balance_due")])
    assert ok == {} and list(bad) == ["CARA:2"]


@pytest.mark.parametrize("authorized_on, expect", [("2026-03-31", False), ("2026-04-01", True)])
def test_authorization_date_boundary_is_the_audit_date_inclusive(authorized_on, expect):
    hits = flagged(unauthorized_accessorial, [invoice("1", "S1", acc=[("LIFTGATE", 95.0)])],
                   extra_auths=[("S1", "LIFTGATE", 95.0, authorized_on)])
    assert ("CARA:1" in hits) == expect


# ------------------------------------------------------------------ weight_overbilling


def test_weight_overbilling_flags_inflation_and_prices_it_at_the_contract_rate():
    """1,300 lb billed vs 1,000: LH 260 vs 200 = 60 x 1.09 = $65.40 with fuel."""
    hit = flagged(weight_overbilling, [invoice("1", "S1", weight=1300, lh=260.00, fsc=23.40)])["CARA:1"]
    assert hit.dollar_impact_estimate == pytest.approx(65.40, abs=CENT)
    assert "1,300 lb vs reference 1,000 lb" in hit.reason and "+30.0%" in hit.reason


def test_weight_ignores_documented_reweigh_and_tl():
    hits = flagged(weight_overbilling, [invoice("1", "S3", weight=1100),
                                        invoice("2", "S4", weight=41000)])
    assert hits == {}


@pytest.mark.parametrize("weight, expect", [(1020, False), (1021, True)])
def test_weight_percentage_boundary(weight, expect):
    """2% over 1,000 lb is 1,020 lb, which passes; 1,021 is flagged and worth (204.20 - 200) x 1.09 = $4.58."""
    hits = flagged(weight_overbilling, [invoice("1", "S1", weight=weight)])
    assert ("CARA:1" in hits) == expect
    if expect:
        assert hits["CARA:1"].dollar_impact_estimate == pytest.approx(4.58, abs=CENT)


def test_weight_tolerance_comes_from_config():
    specs = [invoice("1", "S1", weight=1040)]
    assert "CARA:1" in flagged(weight_overbilling, specs)
    assert flagged(weight_overbilling, specs, cfg=make_cfg(weight_pct=0.05)) == {}

