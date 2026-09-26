"""The re-rater: reference weight, contract linehaul, expected fuel surcharge, and the impact split."""

import pandas as pd
import pytest

from freight_audit_lab.rerate import rerate
from tests.fixtures import audit_inputs, invoice, make_cfg, make_ref, build_norm

CENT = 0.01


def rerated_row(spec, **kwargs):
    _, rr, _, _ = audit_inputs([spec], **kwargs)
    return rr["invoices"].iloc[0]


def test_clean_invoice_reprices_to_itself():
    r = rerated_row(invoice("1", "S1"))
    assert r["reference_weight_lbs"] == 1000 and not r["has_certificate"]
    assert r["lh_contract_at_billed_wt"] == pytest.approx(200.00, abs=CENT)
    assert r["fsc_pct_expected"] == pytest.approx(0.09) and r["fsc_expected"] == pytest.approx(18.00, abs=CENT)
    assert (r["rate_impact_estimate"], r["weight_impact_estimate"], r["fsc_impact_estimate"]) == (0, 0, 0)
    assert r["lane"] == "Chicago → Atlanta"


def test_rate_row_is_chosen_by_ship_date_not_invoice_date():
    """S1 shipped before the July amendment; even an invoice dated after it is priced at v1."""
    r = rerated_row(invoice("1", "S1", invoice_date="2025-09-01", received="2025-09-03"))
    assert r["rate_version"] == 1 and r["lh_contract_at_billed_wt"] == pytest.approx(200.00, abs=CENT)
    r = rerated_row(invoice("2", "S2"))
    assert r["rate_version"] == 2 and r["lh_contract_at_billed_wt"] == pytest.approx(180.00, abs=CENT)


def test_certificate_sets_reference_weight_and_clears_the_weight_impact():
    r = rerated_row(invoice("1", "S3", weight=1100))
    assert r["reference_weight_lbs"] == 1100 and r["has_certificate"]
    assert r["weight_impact_estimate"] == 0


def test_certificate_recorded_after_audit_date_is_not_used():
    ref = make_ref()
    ref["reweigh_certificates"]["certified_at"] = pd.Timestamp("2026-06-01")
    cfg = make_cfg()
    norm = build_norm([invoice("1", "S3", weight=1100)])
    r = rerate(norm, ref, cfg)["invoices"].iloc[0]
    assert r["reference_weight_lbs"] == 1000 and r["weight_impact_estimate"] > 0


def test_weight_and_rate_impacts_split_without_overlap():
    """Billed 1,200 lb (contract LH $240) at a 5% markup ($252.00), FSC 9% of the billed LH.

    weight = (240 - 200) x 1.09 = 43.60;  rate = (252 - 240) x 1.09 = 13.08;  FSC gap = 0.
    Together they equal the whole overcharge, (252 - 200) x 1.09 = 56.68, with nothing counted twice.
    """
    r = rerated_row(invoice("1", "S1", weight=1200, lh=252.00, fsc=22.68))
    assert r["lh_contract_at_billed_wt"] == pytest.approx(240.00, abs=CENT)
    assert r["weight_impact_estimate"] == pytest.approx(43.60, abs=CENT)
    assert r["rate_impact_estimate"] == pytest.approx(13.08, abs=CENT)
    assert r["fsc_impact_estimate"] == pytest.approx(0.00, abs=CENT)
    assert r["weight_impact_estimate"] + r["rate_impact_estimate"] == pytest.approx(56.68, abs=CENT)


def test_tl_has_no_weight_impact_and_no_fuel_knock_on():
    """TL linehaul ignores weight; a $20 linehaul overcharge is $20, and FSC is $/mile x miles."""
    r = rerated_row(invoice("1", "S4", lh=1520.00, weight=41000))
    assert r["lh_contract_at_billed_wt"] == pytest.approx(1500.00, abs=CENT)
    assert r["rate_impact_estimate"] == pytest.approx(20.00, abs=CENT)
    assert r["weight_impact_estimate"] == 0 and pd.isna(r["fsc_pct_expected"])
    assert r["fsc_expected"] == pytest.approx(95.00, abs=CENT)


def test_only_live_matched_originals_and_rebills_are_rerated():
    specs = [invoice("1", "S1"),
             invoice("2", "S1", superseded=True),
             invoice("3", "S1", itype="rebill"),
             invoice("4", "S5", lh=None, fsc=None, acc=[("LIFTGATE", 95.0)], itype="balance_due"),
             invoice("5", "S1", match="unmatched")]
    _, rr, _, _ = audit_inputs(specs)
    assert sorted(rr["invoices"]["invoice_id"]) == ["CARA:1", "CARA:3"]
    assert list(rr["accessorials"]["invoice_id"]) == ["CARA:4"]


def test_accessorial_status_as_of_the_audit_date():
    """S5's liftgate was authorized 2025-03-25, after this invoice's 2025-03-15 date but before audit_as_of."""
    specs = [invoice("1", "S5", acc=[("LIFTGATE", 95.0)]),        # authorized late
             invoice("2", "S1", acc=[("LIFTGATE", 95.0)]),        # S1 only has a residential authorization
             invoice("3", "S1", acc=[("RESIDENTIAL", 125.0)])]    # authorized before the invoice date
    _, rr, _, _ = audit_inputs(specs)
    status = rr["accessorials"].set_index("invoice_id")["auth_status"]
    assert status.to_dict() == {"CARA:1": "authorized_late", "CARA:2": "unauthorized", "CARA:3": "authorized"}


def test_authorization_recorded_after_audit_date_does_not_count():
    _, rr, _, _ = audit_inputs([invoice("1", "S1", acc=[("LIFTGATE", 95.0)])],
                               extra_auths=[("S1", "LIFTGATE", 95.0, "2026-04-01")])
    assert rr["accessorials"]["auth_status"].iloc[0] == "unauthorized"
