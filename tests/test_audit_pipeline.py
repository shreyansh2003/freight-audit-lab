"""Structural checks on the audit run over the full generated data. These use no answer key: they
test that the audit's own outputs are internally consistent (accuracy against the key is Stage 5)."""

import pandas as pd


def test_flags_never_land_on_superseded_invoices(audited):
    superseded = set(audited["invoices"].loc[audited["invoices"]["is_superseded"], "invoice_id"])
    assert superseded and not superseded & set(audited["audit_flags"]["invoice_id"])


def test_balance_due_invoices_only_get_accessorial_and_phantom_flags(audited):
    inv = audited["invoices"]
    balance_due = set(inv.loc[inv["invoice_type"] == "balance_due", "invoice_id"])
    flags = audited["audit_flags"]
    kinds = set(flags.loc[flags["invoice_id"].isin(balance_due), "error_type"])
    assert kinds <= {"unauthorized_accessorial", "phantom_invoice"}


def test_recoverable_never_exceeds_the_invoice_total_and_every_flag_has_a_reason(audited):
    summary = audited["audit_invoice_summary"]
    assert (summary["recoverable_estimate"] <= summary["total"] + 0.005).all()
    assert (summary["recoverable_estimate"] >= 0).all()
    flags = audited["audit_flags"]
    assert flags["reason"].str.len().min() > 20 and (flags["dollar_impact_estimate"] >= 0).all()


def test_every_audited_invoice_is_in_the_summary_once(audited):
    inv = audited["invoices"]
    summary = audited["audit_invoice_summary"]
    assert sorted(summary["invoice_id"]) == sorted(inv.loc[~inv["is_superseded"], "invoice_id"])


def test_baseline_flags_more_than_the_engine_on_the_same_data(audited):
    assert len(audited["baseline_flags"]) > len(audited["audit_flags"])
