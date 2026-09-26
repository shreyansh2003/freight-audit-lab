"""Month-end accruals: cutoff, no look-ahead, hand-computed estimates, journal entries, accuracy.

The small tests use the hand-priced world in tests/fixtures.py. Every shipment is delivered two days after it
ships: S1, S3, S4, S5, S6 ship 2025-03-10 (delivered 03-12), S2 and S7 ship 2025-08-04. Clean prices: S6 is
$600.00 linehaul + $54.00 fuel, S4 (TL, carrier CARF) $1,500.00 + $95.00, S1/S3/S5 $200.00 + $18.00 (S3 is accrued
at its shipment weight, not its certified reweigh weight).
"""

import pandas as pd
import pytest

from freight_audit_lab.accruals import (accessorial_allowance, accrue_at, accuracy_by_month, accuracy_summary,
                                        authorizations_at_delivery, journal_entries, month_ends, price_shipments, run_accruals, shipment_actuals,
                                        write_accruals)
from freight_audit_lab.audit.engine import audit
from freight_audit_lab.csv_io import load_reference, read_csv
from tests.fixtures import audit_inputs, invoice, make_cfg

CENT = 0.01
TS = pd.Timestamp
M = TS("2025-03-31")


def accrue(specs, month_end=M, extra_auths=()):
    """(accrual detail at month_end, norm, ref, cfg) for a set of invoice specs."""
    norm, _, ref, cfg = audit_inputs(specs, None, extra_auths)
    priced = price_shipments(ref["shipments"], ref, cfg)
    return accrue_at(month_end, priced, norm, ref, cfg), norm, ref, cfg


def accrued(detail):
    """{shipment_id: accrual_estimate}."""
    return dict(zip(detail["shipment_id"], detail["accrual_estimate"]))


# History that gives CARA a $73.33 accessorial allowance at 2025-03-31: three billed shipments (S1, S3, S5) and $220.00 of
# authorized accessorials among them ($125 residential authorized 03-05, $95 liftgate authorized 03-25).
HISTORY = [invoice("1", "S1", acc=[("RESIDENTIAL", 125.0)]), invoice("2", "S3"), invoice("3", "S5", acc=[("LIFTGATE", 95.0)])]


# ---------------------------------------------------------------- hand-computed estimates


def test_ltl_accrual_is_contract_linehaul_plus_fuel_plus_the_carriers_accessorial_allowance():
    """S6: $600.00 + $54.00 + (125 + 95) / 3 = $73.33 = $727.33."""
    detail, *_ = accrue(HISTORY)
    row = detail[detail["shipment_id"] == "S6"].iloc[0]
    assert (row.lh_estimate, row.fsc_estimate) == (600.00, 54.00)
    assert row.accessorial_estimate == pytest.approx(73.33, abs=CENT) and row.accessorial_basis == "history"
    assert row.accrual_estimate == pytest.approx(727.33, abs=CENT)
    assert (row.carrier_id, row.cost_center, row["mode"]) == ("CARA", "CC-101", "LTL")


def test_tl_accrual_uses_the_config_default_allowance_when_the_carrier_has_no_history():
    """S4: $1,500.00 + $95.00 + $8.00 TL default = $1,603.00."""
    row = accrue(HISTORY)[0].query("shipment_id == 'S4'").iloc[0]
    assert row.accessorial_basis == "default" and row.accessorial_estimate == 8.00
    assert row.accrual_estimate == pytest.approx(1603.00, abs=CENT)


def test_the_rate_in_force_on_the_ship_date_prices_the_accrual():
    """S2 shipped after the July amendment (10% lower): $180.00 + $16.20, not the old $200.00 + $18.00."""
    detail, *_ = accrue([], month_end=TS("2025-08-31"))
    row = detail[detail["shipment_id"] == "S2"].iloc[0]
    assert (row.lh_estimate, row.fsc_estimate) == (180.00, 16.20)


# ---------------------------------------------------------------- cutoff


@pytest.mark.parametrize("month_end, expect", [("2025-03-12", True), ("2025-03-11", False)])
def test_delivered_on_month_end_is_accrued_and_delivered_the_next_day_is_not(month_end, expect):
    detail, *_ = accrue([], month_end=TS(month_end))
    assert ("S6" in accrued(detail)) == expect


@pytest.mark.parametrize("received, expect", [("2025-03-12", False), ("2025-03-13", True)])
def test_invoice_received_on_month_end_is_not_accrued_but_received_the_next_day_is(received, expect):
    detail, *_ = accrue([invoice("1", "S6", received=received)], month_end=TS("2025-03-12"))
    assert ("S6" in accrued(detail)) == expect


def test_a_balance_due_invoice_does_not_bill_the_shipment():
    detail, *_ = accrue([invoice("1", "S6", lh=None, fsc=None, acc=[("DETENTION", 218.0)], itype="balance_due")])
    assert "S6" in accrued(detail)


def test_a_phantom_invoice_that_matched_no_shipment_does_not_bill_one():
    detail, *_ = accrue([invoice("1", "S6", match="unmatched")])
    assert "S6" in accrued(detail)


def test_in_transit_shipments_are_not_accrued():
    detail, *_ = accrue([], month_end=TS("2025-08-05"))           # S2 and S7 shipped 08-04, delivered 08-06
    assert not {"S2", "S7"} & set(detail["shipment_id"])


# ---------------------------------------------------------------- no look-ahead


def test_an_invoice_received_after_month_end_changes_neither_the_population_nor_the_allowance():
    base, *_ = accrue(HISTORY)
    later = [invoice("8", "S6", received="2025-04-02"),                                       # bills S6 after M
             invoice("9", "S1", acc=[("RESIDENTIAL", 125.0)], received="2025-04-03", number="N9")]   # more accessorial history
    with_later, *_ = accrue(HISTORY + later)
    pd.testing.assert_frame_equal(base, with_later)
    assert "S6" in accrued(with_later)
    assert "S6" not in accrued(accrue(HISTORY + later, month_end=TS("2025-04-30"))[0])         # ...but it is billed by April


def test_an_authorization_recorded_after_month_end_is_not_in_the_allowance():
    """S5's liftgate was authorized 03-25. Move it to 04-01 and the $95.00 drops out of the March allowance."""
    norm, _, ref, cfg = audit_inputs(HISTORY)
    ref["authorizations"] = ref["authorizations"].assign(authorized_at=lambda d: d["authorized_at"].where(
        d["code"] != "LIFTGATE", TS("2025-04-01")))
    allowance = accessorial_allowance(norm, ref["authorizations"], cfg, M).set_index("carrier_id")["accessorial_allowance"]
    assert allowance["CARA"] == pytest.approx(125.0 / 3)


def test_recording_every_authorization_at_delivery_restores_a_late_authorization_to_the_allowance():
    """Same setup as above: S5's liftgate is recorded 04-01, so the March allowance is $125 / 3. With every
    authorization recorded at delivery (03-12) it is back to ($125 + $95) / 3, and nothing else changes."""
    norm, _, ref, cfg = audit_inputs(HISTORY)
    late = ref["authorizations"].assign(authorized_at=lambda d: d["authorized_at"].where(d["code"] != "LIFTGATE", TS("2025-04-01")))
    at_delivery = authorizations_at_delivery(dict(ref, authorizations=late))
    assert (at_delivery["authorized_at"] == TS("2025-03-12")).all()
    assert at_delivery[["auth_id", "shipment_id", "code", "authorized_amount"]].equals(late[["auth_id", "shipment_id", "code", "authorized_amount"]])
    for auths, expect in ((late, 125.0 / 3), (at_delivery, 220.0 / 3)):
        allowance = accessorial_allowance(norm, auths, cfg, M).set_index("carrier_id")["accessorial_allowance"]
        assert allowance["CARA"] == pytest.approx(expect)


def test_supersession_after_month_end_is_not_used_at_month_end():
    """S5's original (with a $95 liftgate) was received 03-20; a rebill received 04-05 supersedes it, so in the final data
    the original is superseded. At 03-31 the shipper only has the original: S5 is billed, and its liftgate is in the
    allowance ($95 over the 2 shipments S5 and S1 = $47.50). At 04-30 the rebill is known and counts instead of the original."""
    specs = [invoice("1", "S1"),
             invoice("2", "S5", acc=[("LIFTGATE", 95.0)], received="2025-03-20", superseded=True, superseded_on="2025-04-05"),
             invoice("3", "S5", acc=[("LIFTGATE", 95.0)], received="2025-04-05", itype="rebill")]
    detail, norm, ref, cfg = accrue(specs)
    assert "S5" not in accrued(detail)                                                       # billed by the original at M
    assert accrued(detail)["S6"] == pytest.approx(600.00 + 54.00 + 47.50, abs=CENT)
    april = accessorial_allowance(norm, ref["authorizations"], cfg, TS("2025-04-30")).set_index("carrier_id")
    assert april.loc["CARA", "accessorial_allowance"] == pytest.approx(47.50, abs=CENT)     # rebill counted once, original not


def test_a_superseded_invoice_still_bills_its_shipment_until_the_rebill_is_known():
    """Same original, but no rebill yet at all: the shipment is not accrued, whatever normalization says later."""
    detail, *_ = accrue([invoice("1", "S6", superseded=True, superseded_on="2025-06-01")])
    assert "S6" not in accrued(detail)


# ---------------------------------------------------------------- journal entries


@pytest.fixture(scope="module")
def entries():
    a, *_ = accrue(HISTORY, month_end=M)
    b, *_ = accrue(HISTORY, month_end=TS("2025-08-31"))
    detail = pd.concat([a, b], ignore_index=True)
    return journal_entries(detail, make_cfg()), detail


def cents(x):
    return round(x * 100)


def test_every_journal_entry_balances_to_the_cent(entries):
    je, _ = entries
    for je_id, g in je.groupby("je_id"):
        assert cents(g["debit"].sum()) == cents(g["credit"].sum()), je_id
    assert (je["debit"] * je["credit"] == 0).all()                       # a line is either a debit or a credit


def test_accrual_debits_expense_by_cost_center_and_credits_accrued_freight_once(entries):
    je, detail = entries
    march = je[(je["type"] == "accrual") & (je["je_id"] == "ACR-202503")]
    expense = march[march["account"] == "6100 Freight Expense"]
    assert list(expense["cost_center"]) == sorted(expense["cost_center"]) and expense["cost_center"].is_unique
    by_cc = detail[detail["month_end"] == M].groupby("cost_center")["accrual_estimate"].sum()
    for line in expense.itertuples():
        assert line.debit == pytest.approx(by_cc[line.cost_center], abs=CENT)
    credit = march[march["account"] == "2150 Accrued Freight"]
    assert len(credit) == 1 and credit.iloc[0].credit == pytest.approx(by_cc.sum(), abs=CENT)


def test_each_accrual_is_reversed_exactly_once_on_day_one_of_the_next_month(entries):
    je, _ = entries
    for month_end, key in [(M, "202503"), (TS("2025-08-31"), "202508")]:
        accrual, reversal = je[je["je_id"] == f"ACR-{key}"], je[je["je_id"] == f"REV-{key}"]
        assert (accrual["date"] == month_end).all() and (reversal["date"] == month_end + pd.Timedelta(days=1)).all()
        assert set(reversal["type"]) == {"reversal"} and len(je[je["type"] == "reversal"]) == len(je[je["type"] == "accrual"])
        mirror = accrual[["account", "cost_center", "debit", "credit"]].reset_index(drop=True)
        back = reversal[["account", "cost_center", "credit", "debit"]].rename(
            columns={"credit": "debit", "debit": "credit"}).reset_index(drop=True)
        pd.testing.assert_frame_equal(mirror, back)


def test_accrued_freight_balance_is_zero_after_every_reversal_and_memos_say_estimate(entries):
    je, _ = entries
    liability = je[je["account"] == "2150 Accrued Freight"].sort_values(["date", "je_id"])
    balance = (liability["credit"] - liability["debit"]).map(cents).cumsum()
    assert list(balance[liability["type"] == "reversal"]) == [0, 0]
    assert je["memo"].str.startswith("ESTIMATE – synthetic data").all()


def test_month_ends_are_the_last_day_of_each_configured_month():
    ends = month_ends(make_cfg())
    assert len(ends) == 12 and ends[0] == TS("2025-01-31") and ends[1] == TS("2025-02-28") and ends[-1] == TS("2025-12-31")


# ---------------------------------------------------------------- actuals and accuracy (hand-built)


def norm_and_engine(specs, **kwargs):
    norm, rr, ref, cfg = audit_inputs(specs, **kwargs)
    return norm, audit(norm, rr, ref, cfg), ref, cfg


def test_payable_is_billed_less_the_recoverable_estimate_split_by_charge_type():
    """S1 billed $220.00 + $19.80 (a $20 linehaul markup, impact $21.80) plus an unauthorized $95 liftgate: billed
    $334.80, recoverable $116.80, payable the clean $218.00, all of it linehaul + fuel."""
    specs = [invoice("1", "S1", lh=220.00, fsc=19.80, acc=[("LIFTGATE", 95.0)])]
    norm, engine, *_ = norm_and_engine(specs)
    row = shipment_actuals(norm, engine).iloc[0]
    assert row.actual_billed == pytest.approx(220.00 + 19.80 + 95.00, abs=CENT)
    assert row.actual_recoverable_estimate == pytest.approx(21.80 + 95.00, abs=CENT)
    assert row.actual_payable_estimate == pytest.approx(218.00, abs=CENT)
    assert row.actual_payable_lh_fsc_estimate == pytest.approx(218.00, abs=CENT) and row.actual_payable_accessorial_estimate == 0.0


def test_a_duplicate_is_billed_twice_and_payable_once():
    specs = [invoice("1", "S1"), invoice("2", "S1", received="2025-03-28")]
    norm, engine, *_ = norm_and_engine(specs)
    row = shipment_actuals(norm, engine).iloc[0]
    assert (row.invoices_billed, row.actual_billed, row.actual_payable_estimate) == (2, 436.00, 218.00)


def test_superseded_invoices_are_not_actuals_and_balance_due_is():
    specs = [invoice("1", "S5", lh=230.00, fsc=20.70, superseded=True, superseded_on="2025-04-01"),
             invoice("2", "S5", itype="rebill", received="2025-04-01"),
             invoice("3", "S5", lh=None, fsc=None, acc=[("LIFTGATE", 95.0)], itype="balance_due", received="2025-04-05")]
    norm, engine, *_ = norm_and_engine(specs)
    row = shipment_actuals(norm, engine).iloc[0]
    assert row.invoices_billed == 2 and row.actual_billed == pytest.approx(218.00 + 95.00, abs=CENT)     # rebill + balance due


def test_accuracy_error_is_accrual_minus_payable_as_a_share_of_payable():
    specs = HISTORY + [invoice("4", "S6", received="2025-04-10", lh=600.00, fsc=54.00)]
    norm, engine, ref, cfg = norm_and_engine(specs)
    priced = price_shipments(ref["shipments"], ref, cfg)
    detail = accrue_at(M, priced, norm, ref, cfg).merge(shipment_actuals(norm, engine), on="shipment_id", how="left")
    acc = accuracy_by_month(detail, cfg).set_index("month_end")
    march = acc.loc[M]
    assert march["shipments_accrued"] == 2 and march["never_billed_shipments"] == 1          # S4 was never billed
    assert march["accrual_estimate"] == pytest.approx(727.33 + 1603.00, abs=CENT)
    assert march["actual_payable_estimate"] == pytest.approx(654.00, abs=CENT)               # only S6 was billed
    assert march["error_estimate"] == pytest.approx(727.33 + 1603.00 - 654.00, abs=CENT)
    assert march["error_pct_estimate"] == pytest.approx(march["error_estimate"] / 654.00)
    assert acc.loc["ALL", "accrual_estimate"] == march["accrual_estimate"]


# ---------------------------------------------------------------- on the full generated data


@pytest.fixture(scope="module")
def accrued_full(audited, full):
    norm = {"invoices": audited["invoices"], "invoice_lines": audited["invoice_lines"]}
    engine = {"flags": audited["audit_flags"], "invoice_summary": audited["audit_invoice_summary"]}
    ref = load_reference(full[1])
    return norm, ref, run_accruals(norm, ref, audited_cfg(), engine), engine


def audited_cfg():
    return make_cfg()


def test_full_data_population_matches_an_independent_recount(accrued_full):
    norm, ref, result, _ = accrued_full
    inv, ship, detail = norm["invoices"], ref["shipments"], result["detail"]
    for month_end in month_ends(make_cfg()):
        billed = set(inv[inv["invoice_type"].isin(["original", "rebill"]) & inv["shipment_id"].notna()
                         & (inv["received_date"] <= month_end)]["shipment_id"])
        expect = set(ship[(ship["delivery_date"] <= month_end) & ~ship["shipment_id"].isin(billed)]["shipment_id"])
        assert set(detail.loc[detail["month_end"] == month_end, "shipment_id"]) == expect


@pytest.mark.parametrize("month_end", ["2025-02-28", "2025-06-30", "2025-11-30"])
def test_full_data_no_look_ahead_deleting_everything_after_month_end_changes_nothing(accrued_full, month_end):
    """Rebuild the inputs from only what existed at M: invoices received by M, their lines, authorizations by M.
    The accrual at M must be identical."""
    norm, ref, _, _ = accrued_full
    month_end = TS(month_end)
    known = norm["invoices"][norm["invoices"]["received_date"] <= month_end]
    trimmed = {"invoices": known, "invoice_lines": norm["invoice_lines"][norm["invoice_lines"]["invoice_id"].isin(known["invoice_id"])]}
    ref_then = dict(ref, authorizations=ref["authorizations"][ref["authorizations"]["authorized_at"] <= month_end])
    cfg = make_cfg()
    priced = price_shipments(ref["shipments"], ref, cfg)
    pd.testing.assert_frame_equal(accrue_at(month_end, priced, norm, ref, cfg),
                                  accrue_at(month_end, priced, trimmed, ref_then, cfg))


def test_full_data_payable_identity_and_journal_entries(accrued_full):
    norm, ref, result, engine = accrued_full
    actuals = shipment_actuals(norm, engine)
    assert (actuals["actual_billed"] - actuals["actual_recoverable_estimate"] - actuals["actual_payable_estimate"]).abs().max() < 0.011
    parts = actuals["actual_payable_lh_fsc_estimate"] + actuals["actual_payable_accessorial_estimate"]
    assert (parts - actuals["actual_payable_estimate"]).abs().max() < 0.011
    je = result["journal_entries"]
    assert all(cents(g["debit"].sum()) == cents(g["credit"].sum()) for _, g in je.groupby("je_id"))
    assert je["je_id"].str.startswith("ACR").sum() == je["je_id"].str.startswith("REV").sum() > 0
    liability = je[je["account"] == "2150 Accrued Freight"].sort_values(["date", "je_id"])
    balance = (liability["credit"] - liability["debit"]).map(cents).cumsum()
    assert (balance[liability["type"] == "reversal"] == 0).all() and balance.iloc[-1] == 0


def test_full_data_accuracy_is_close_and_shipments_are_accrued_only_until_billed(accrued_full):
    _, _, result, _ = accrued_full
    accuracy = result["accuracy"]
    monthly = accuracy[accuracy["month_end"] != "ALL"]
    assert len(monthly) == 12 and monthly["error_pct_estimate"].abs().max() < 0.05
    detail = result["detail"]
    assert detail.duplicated(["month_end", "shipment_id"]).sum() == 0
    accrued_at_later_month = detail.merge(detail[["shipment_id", "month_end"]].rename(columns={"month_end": "later"}), on="shipment_id")
    assert (accrued_at_later_month["later"] >= accrued_at_later_month["month_end"]).any()
    headline = accuracy_summary(accuracy)
    assert headline["mape_pct_estimate"] >= abs(headline["bias_pct_estimate"]) >= 0


def test_full_data_sensitivity_changes_only_the_accrual_never_the_payable(accrued_full):
    """The what-if books move the accessorial allowance and nothing else: same months, same shipments, same
    payable. Earlier paperwork can only add history, so no month's accrual falls."""
    result = accrued_full[2]
    accuracy, sens = result["accuracy"], result["sensitivity"]
    assert list(sens["month_end"]) == list(accuracy["month_end"]) and len(sens) == 13
    assert (sens["shipments_accrued"] == accuracy["shipments_accrued"]).all()
    assert (sens["actual_payable_estimate"] == accuracy["actual_payable_estimate"]).all()
    assert (sens["accrual_estimate_as_built"] == accuracy["accrual_estimate"]).all()
    assert (sens["late_auth_effect_estimate"] >= -CENT).all() and sens["late_auth_effect_estimate"].iloc[-1] > 0
    moved = sens["error_estimate_auth_at_delivery"] - sens["error_estimate_as_built"]
    assert (moved - sens["late_auth_effect_estimate"]).abs().max() < CENT
    non_accessorial = lambda suffix: sens[f"error_estimate_{suffix}"] - sens[f"accessorial_error_estimate_{suffix}"]
    assert (non_accessorial("as_built") - non_accessorial("auth_at_delivery")).abs().max() < CENT   # lh + fsc side untouched


def test_outputs_are_written(accrued_full, tmp_path):
    write_accruals(accrued_full[2], tmp_path)
    assert {p.name for p in tmp_path.iterdir()} == {"accruals_detail.csv", "accrual_accuracy.csv",
                                                    "accrual_sensitivity.csv", "journal_entries.csv"}
    sens = read_csv(tmp_path / "accrual_sensitivity.csv")
    assert len(sens) == 13 and "late_auth_effect_estimate" in sens.columns
    je = read_csv(tmp_path / "journal_entries.csv")
    assert list(je.columns) == ["je_id", "date", "type", "account", "cost_center", "debit", "credit", "memo"]


def test_shipment_level_error_does_not_net_out_the_way_the_monthly_error_does(accrued_full):
    """Each shipment-month is scored on its own: the mean absolute error can be far above the (netted) monthly one."""
    accuracy = accrued_full[2]["accuracy"]
    total = accuracy[accuracy["month_end"] == "ALL"].iloc[0]
    assert total["shipment_mape_pct_estimate"] > abs(total["error_pct_estimate"])
    assert 0 < total["shipment_median_ape_pct_estimate"] <= 1 and 0 <= total["shipment_share_over_band_pct_estimate"] <= 1
