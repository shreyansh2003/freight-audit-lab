"""Re-rater: what each invoice should have cost, worked out from the contract.

For every invoice the audit will look at (not superseded, matched to a shipment) this module
prices the shipment the way the contract says and sets that next to what the carrier billed.
All pricing goes through `contract.py`; nothing here knows how the invoices were generated.

The decomposition. A carrier can be wrong about the weight, about the rate, or about the fuel
surcharge, and a single invoice can be wrong about all three. To keep each dollar in exactly
one bucket the re-rater peels the bill apart in a fixed order:

    weight impact = (LH at billed weight - LH at reference weight)   x (1 + FSC%)   [LTL]
    rate impact   = (billed LH           - LH at billed weight)      x (1 + FSC%)   [LTL]
    fsc impact    = billed FSC           - expected FSC on the billed LH

LH is linehaul, FSC the fuel surcharge. The reference weight is the certified reweigh weight
when a certificate exists, otherwise the shipment weight. For LTL the fuel surcharge is a
percentage of linehaul, so every linehaul dollar overcharged also drags a fuel surcharge
dollar-fraction with it: the first two impacts carry that knock-on (x (1 + FSC%)), and the FSC
impact is measured on the *billed* linehaul so the knock-on is not counted a second time. TL
fuel is $/mile x miles and TL linehaul ignores weight, so TL impacts have no knock-on and no
weight impact.
"""

import numpy as np
import pandas as pd

from freight_audit_lab.contract import (cents, contract_linehaul, diesel_for_ship_date, fsc_ltl_amount,
                                        fsc_ltl_pct, fsc_tl_amount, lookup_rate, ship_week)

ACCESSORIAL_CODES = ["LIFTGATE", "RESIDENTIAL", "DETENTION"]
RATED_TYPES = ["original", "rebill"]     # balance-due invoices carry no linehaul or fuel line


def rerate(norm, ref, cfg):
    """Re-rate the invoices worth auditing. Returns {"invoices": ..., "accessorials": ...}.

    Superseded invoices are replaced by their rebill and never audited. Unmatched invoices have
    no shipment to price against, so they get no re-rate (the phantom rule handles them).
    `invoices` has one row per original or rebill; `accessorials` has one row per accessorial
    charge line on any audited invoice, including balance-due invoices.
    """
    as_of = pd.Timestamp(cfg["period"]["audit_as_of"])
    inv = norm["invoices"]
    live = inv[~inv["is_superseded"] & (inv["match_method"] != "unmatched")]
    return {"invoices": rerate_invoices(live[live["invoice_type"].isin(RATED_TYPES)],
                                        norm["invoice_lines"], ref, cfg),
            "accessorials": rerate_accessorials(live, norm["invoice_lines"], ref["authorizations"], as_of)}


def rerate_invoices(inv, lines, ref, cfg):
    """One row per invoice: billed vs contract linehaul and fuel surcharge, and the three impacts.

    Freight is priced at the rate row in force on the *ship date*, from the shipper's own
    shipment record (not the carrier's printed date), so an amendment never reprices earlier
    shipments. Diesel is the price for that ship week, as in the contract.
    """
    as_of = pd.Timestamp(cfg["period"]["audit_as_of"])
    certs = ref["reweigh_certificates"]
    certified = certs[certs["certified_at"] <= as_of].set_index("shipment_id")["certified_weight_lbs"]
    shipments = ref["shipments"][["shipment_id", "lane_id", "mode", "miles", "weight_lbs",
                                  "ship_date"]].rename(columns={"ship_date": "shipment_ship_date",
                                                                "weight_lbs": "shipment_weight_lbs"})
    lanes = ref["lanes"][["lane_id", "origin_city", "destination_city"]]
    billed = (lines[lines["charge_code"].isin(["LH", "FSC"])]
              .pivot_table(index="invoice_id", columns="charge_code", values="amount", aggfunc="sum"))
    merged = (inv[["invoice_id", "carrier_id", "invoice_number", "invoice_type", "shipment_id",
                   "billed_weight_lbs", "total"]]
              .merge(shipments, on="shipment_id", how="left").merge(lanes, on="lane_id", how="left"))

    rows = []
    for r in merged.itertuples(index=False):
        rate = lookup_rate(ref["rate_card"], r.carrier_id, r.lane_id, r.shipment_ship_date)
        diesel = diesel_for_ship_date(ref["diesel_weekly"], r.shipment_ship_date)
        billed_lh = float(billed.at[r.invoice_id, "LH"]) if r.invoice_id in billed.index else 0.0
        billed_fsc = float(billed.at[r.invoice_id, "FSC"]) if r.invoice_id in billed.index else 0.0
        billed_weight = int(r.billed_weight_lbs)
        has_cert = r.shipment_id in certified.index
        ref_weight = int(certified[r.shipment_id]) if has_cert else int(r.shipment_weight_lbs)
        lh_billed_wt = contract_linehaul(rate, billed_weight, r.miles)
        lh_ref_wt = contract_linehaul(rate, ref_weight, r.miles)
        if r.mode == "LTL":
            pct = fsc_ltl_pct(diesel, cfg)
            fsc_expected = fsc_ltl_amount(billed_lh, diesel, cfg)
            knock_on = 1 + pct
        else:
            pct, knock_on = np.nan, 1.0
            fsc_expected = fsc_tl_amount(r.miles, diesel, cfg)
        rows.append({
            "invoice_id": r.invoice_id, "carrier_id": r.carrier_id, "invoice_number": r.invoice_number,
            "invoice_type": r.invoice_type, "shipment_id": r.shipment_id, "mode": r.mode,
            "lane": f"{r.origin_city} → {r.destination_city}", "miles": r.miles,
            "ship_date": r.shipment_ship_date, "rate_version": int(rate["version"]),
            "rate_effective_from": rate["effective_from"], "diesel_price": diesel,
            "diesel_week": ship_week(r.shipment_ship_date),
            "billed_weight_lbs": billed_weight, "reference_weight_lbs": ref_weight, "has_certificate": has_cert,
            "billed_lh": billed_lh, "lh_contract_at_billed_wt": lh_billed_wt, "lh_contract_at_ref_wt": lh_ref_wt,
            "billed_fsc": billed_fsc, "fsc_pct_expected": pct, "fsc_expected": fsc_expected,
            "rate_impact_estimate": cents((billed_lh - lh_billed_wt) * knock_on),
            "weight_impact_estimate": cents((lh_billed_wt - lh_ref_wt) * knock_on) if r.mode == "LTL" else 0.0,
            "fsc_impact_estimate": cents(billed_fsc - fsc_expected),
            "total": r.total})
    return pd.DataFrame(rows, columns=RERATED_COLUMNS)


RERATED_COLUMNS = [
    "invoice_id", "carrier_id", "invoice_number", "invoice_type", "shipment_id", "mode", "lane", "miles",
    "ship_date", "rate_version", "rate_effective_from", "diesel_price", "diesel_week",
    "billed_weight_lbs", "reference_weight_lbs", "has_certificate",
    "billed_lh", "lh_contract_at_billed_wt", "lh_contract_at_ref_wt",
    "billed_fsc", "fsc_pct_expected", "fsc_expected",
    "rate_impact_estimate", "weight_impact_estimate", "fsc_impact_estimate", "total"]


def attach_authorization(acc, auths, as_of):
    """Add `authorized_at`: the earliest authorization for this shipment and code recorded on or
    before `as_of` (a Series of dates aligned to `acc`), or NaT if there is none.

    Accessorials need the shipper's sign-off: a liftgate the dock never approved is a bad bill.
    Paperwork is sometimes recorded after the carrier's invoice date, so the answer depends on
    *when you ask*. The audit asks as of `audit_as_of`; a quick spreadsheet pass asks as of the
    invoice date.
    """
    acc = acc.reset_index(drop=True)
    asked = acc[["shipment_id", "charge_code"]].assign(_row=acc.index, _as_of=pd.to_datetime(as_of).to_numpy())
    known = auths.rename(columns={"code": "charge_code"})[["shipment_id", "charge_code", "authorized_at"]]
    pairs = asked.merge(known, on=["shipment_id", "charge_code"])
    pairs = pairs[pairs["authorized_at"] <= pairs["_as_of"]]
    acc["authorized_at"] = pairs.groupby("_row")["authorized_at"].min().reindex(acc.index)
    acc["authorized_at"] = pd.to_datetime(acc["authorized_at"])
    return acc


def authorization_status(acc):
    """`authorized` (on file by the invoice date), `authorized_late` (recorded after the invoice
    date but in time for the as-of date), or `unauthorized` (nothing on file by the as-of date)."""
    late = acc["authorized_at"] > acc["invoice_date"]
    return np.select([acc["authorized_at"].isna(), late], ["unauthorized", "authorized_late"], "authorized")


def rerate_accessorials(live, lines, auths, as_of):
    """One row per accessorial charge line with its authorization status as of `as_of`."""
    acc = lines[lines["charge_code"].isin(ACCESSORIAL_CODES)].merge(
        live[["invoice_id", "carrier_id", "invoice_number", "invoice_type", "shipment_id", "invoice_date"]],
        on="invoice_id")
    acc = attach_authorization(acc, auths, pd.Series([as_of] * len(acc), dtype="datetime64[ns]"))
    acc["authorization_as_of"] = as_of
    acc["auth_status"] = authorization_status(acc)
    return acc
