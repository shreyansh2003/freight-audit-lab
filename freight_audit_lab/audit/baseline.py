"""Baseline: what a quick spreadsheet pass over the same invoices would do.

It is a *careful* spreadsheet pass, not a strawman. It uses the *same rule functions and
tolerances* as the engine, so any difference in results comes from the business-logic shortcuts
below, not from string formatting or tighter/looser thresholds:

  - matches an invoice to a shipment on the digits of the BOL (it strips every non-digit character,
    the cleanup any analyst would do). It does not zero-pad and has no fallback match, so a BOL
    with its leading zeros dropped or two digits transposed still looks like a phantom;
  - knows nothing about supersession, so every invoice is audited, and calls any two invoices
    with the same carrier and BOL text duplicates whatever their type, so rebills and
    balance-due invoices look like duplicates of their parents;
  - checks rates against the carrier-lane's *first* rate row, ignoring effective dates, at the
    shipment weight, so an amendment looks like an overcharge or is missed;
  - ignores reweigh certificates, so a documented reweigh looks like weight overbilling;
  - asks whether an accessorial was authorized as of the *invoice* date, so paperwork recorded
    a little later looks unauthorized.

To reuse the rules, the naive inputs are put in the same shape the re-rater produces. In that
shape `lh_contract_at_billed_wt` holds the first-rate linehaul at the *shipment* weight, because
the baseline never separates weight from rate (which is why a weight-inflated invoice can trip
both the rate and the weight check here, and why baseline dollars can overlap).
"""

import numpy as np
import pandas as pd

from freight_audit_lab.audit.rules import (duplicate_invoice_flag, flags_frame, phantom_invoice, rate_overcharge,
                                           fsc_mismatch, unauthorized_accessorial, weight_overbilling)
from freight_audit_lab.audit.engine import OUTPUT_DIR, apply_recoverable
from freight_audit_lab.contract import (cents, contract_linehaul, diesel_for_ship_date, fsc_ltl_amount,
                                        fsc_ltl_pct, fsc_tl_amount, ship_week)
from freight_audit_lab.csv_io import write_csv
from freight_audit_lab.rerate import (ACCESSORIAL_CODES, RATED_TYPES, RERATED_COLUMNS, attach_authorization,
                                      authorization_status)


def bol_digits(bol_raw):
    """The BOL with every non-digit removed ("BOL#0048-2913" -> "00482913"). No zero-padding."""
    return bol_raw.fillna("").str.replace(r"\D", "", regex=True)


def match_on_bol_digits(inv, shipments):
    """Attach the shipment whose BOL equals the digits of the invoice's BOL text (same carrier)."""
    ship = shipments[["shipment_id", "carrier_id", "bol", "lane_id", "mode", "miles", "weight_lbs", "ship_date"]]
    ship = ship.rename(columns={"ship_date": "shipment_ship_date", "weight_lbs": "shipment_weight_lbs",
                                "shipment_id": "naive_shipment_id"})
    inv = inv.assign(_bol_digits=bol_digits(inv["bol_raw"]))
    return inv.merge(ship, left_on=["carrier_id", "_bol_digits"], right_on=["carrier_id", "bol"], how="left")


def naive_invoice_frame(matched, lines, ref, cfg):
    """The re-rater's per-invoice frame, built the naive way (first rate row, shipment weight)."""
    card = ref["rate_card"]
    first_row = (card.sort_values("effective_from").drop_duplicates(["carrier_id", "lane_id"])
                 .set_index(["carrier_id", "lane_id"]))
    lanes = ref["lanes"].set_index("lane_id")
    billed = (lines[lines["charge_code"].isin(["LH", "FSC"])]
              .pivot_table(index="invoice_id", columns="charge_code", values="amount", aggfunc="sum"))
    rows = []
    for r in matched[matched["invoice_type"].isin(RATED_TYPES) & matched["naive_shipment_id"].notna()].itertuples(index=False):
        rate = first_row.loc[(r.carrier_id, r.lane_id)]
        diesel = diesel_for_ship_date(ref["diesel_weekly"], r.shipment_ship_date)
        billed_lh = float(billed.at[r.invoice_id, "LH"]) if r.invoice_id in billed.index else 0.0
        billed_fsc = float(billed.at[r.invoice_id, "FSC"]) if r.invoice_id in billed.index else 0.0
        billed_weight = int(r.billed_weight_lbs)
        lh_at_shipment_wt = contract_linehaul(rate, r.shipment_weight_lbs, r.miles)
        lh_at_billed_wt = contract_linehaul(rate, billed_weight, r.miles)
        if r.mode == "LTL":
            pct = fsc_ltl_pct(diesel, cfg)
            fsc_expected, knock_on = fsc_ltl_amount(billed_lh, diesel, cfg), 1 + pct
        else:
            pct, knock_on = np.nan, 1.0
            fsc_expected = fsc_tl_amount(r.miles, diesel, cfg)
        lane = lanes.loc[r.lane_id]
        rows.append({
            "invoice_id": r.invoice_id, "carrier_id": r.carrier_id, "invoice_number": r.invoice_number,
            "invoice_type": r.invoice_type, "shipment_id": r.naive_shipment_id, "mode": r.mode,
            "lane": f"{lane['origin_city']} → {lane['destination_city']}", "miles": r.miles,
            "ship_date": r.shipment_ship_date, "rate_version": int(rate["version"]),
            "rate_effective_from": rate["effective_from"], "diesel_price": diesel,
            "diesel_week": ship_week(r.shipment_ship_date),
            "billed_weight_lbs": billed_weight, "reference_weight_lbs": int(r.shipment_weight_lbs),
            "has_certificate": False,
            "billed_lh": billed_lh, "lh_contract_at_billed_wt": lh_at_shipment_wt,
            "lh_contract_at_ref_wt": lh_at_shipment_wt,
            "billed_fsc": billed_fsc, "fsc_pct_expected": pct, "fsc_expected": fsc_expected,
            "rate_impact_estimate": cents((billed_lh - lh_at_shipment_wt) * knock_on),
            "weight_impact_estimate": cents((lh_at_billed_wt - lh_at_shipment_wt) * knock_on) if r.mode == "LTL" else 0.0,
            "fsc_impact_estimate": cents(billed_fsc - fsc_expected), "total": r.total})
    return pd.DataFrame(rows, columns=RERATED_COLUMNS)


def naive_accessorial_frame(matched, lines, auths):
    """Accessorial lines with authorization asked as of each invoice's own date."""
    acc = lines[lines["charge_code"].isin(ACCESSORIAL_CODES)].merge(
        matched[matched["naive_shipment_id"].notna()][["invoice_id", "carrier_id", "invoice_number", "invoice_type",
                                                       "naive_shipment_id", "invoice_date"]]
        .rename(columns={"naive_shipment_id": "shipment_id"}), on="invoice_id")
    acc = attach_authorization(acc, auths, acc["invoice_date"])
    acc["authorization_as_of"] = acc["invoice_date"]
    acc["auth_status"] = authorization_status(acc)
    return acc


def naive_duplicates(inv):
    """Flag every later-received invoice that shares carrier and BOL digits with an earlier one."""
    inv = inv.assign(_bol_digits=bol_digits(inv["bol_raw"])).sort_values(["received_date", "invoice_id"])
    inv = inv[inv["_bol_digits"] != ""]
    inv = inv[inv.duplicated(["carrier_id", "_bol_digits"], keep=False)]
    out = []
    for _, group in inv.groupby(["carrier_id", "_bol_digits"], sort=False):
        first = group.iloc[0]
        out += [duplicate_invoice_flag(r, first, "same BOL digits") for r in group.iloc[1:].itertuples(index=False)]
    return flags_frame(out)


def run_baseline(norm, ref, cfg):
    """Baseline flags in the same columns as the engine's, with `counted_in_recoverable`."""
    inv, lines = norm["invoices"], norm["invoice_lines"]
    matched = match_on_bol_digits(inv, ref["shipments"])
    naive_norm = {"invoices": inv.assign(match_method=np.where(inv["invoice_id"].isin(
        matched.loc[matched["naive_shipment_id"].notna(), "invoice_id"]), "exact", "unmatched"),
        is_superseded=False), "invoice_lines": lines}
    rerated = {"invoices": naive_invoice_frame(matched, lines, ref, cfg),
               "accessorials": naive_accessorial_frame(matched, lines, ref["authorizations"])}
    flags = pd.concat([naive_duplicates(inv), phantom_invoice(naive_norm, rerated, ref, cfg)]
                      + [rule(naive_norm, rerated, ref, cfg)
                         for rule in (rate_overcharge, fsc_mismatch, unauthorized_accessorial, weight_overbilling)],
                      ignore_index=True)
    order = ["duplicate_invoice", "phantom_invoice", "rate_overcharge", "fsc_mismatch",
             "unauthorized_accessorial", "weight_overbilling"]
    flags = (flags.assign(_rule=flags["error_type"].map(order.index)).sort_values(["_rule", "invoice_id"])
             .drop(columns="_rule").reset_index(drop=True))
    flags, recoverable = apply_recoverable(flags, inv)
    return {"flags": flags, "recoverable": recoverable}


def write_baseline(result, out_dir=OUTPUT_DIR):
    """Write outputs/baseline_flags.csv."""
    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(result["flags"], out_dir / "baseline_flags.csv")
