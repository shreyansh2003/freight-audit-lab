"""Canonical invoices: what each carrier should bill for each shipment, before any noise.

An invoice is a plain dict (see `new_invoice`) holding true values and, later, the labels
that traps and errors attach. Nothing here is written to disk in this form: the messy
carrier files are rendered from it (render.py) and the answer key is derived from it
(ground_truth.py). Pricing always goes through freight_audit_lab.contract.
"""

import numpy as np
import pandas as pd

from freight_audit_lab.contract import (cents, contract_linehaul, diesel_for_ship_date,
                                        fsc_ltl_amount, fsc_tl_amount, lookup_rate)

PRO_DIGITS = 9
CONTROL_DIGITS = 8
INVOICE_NUMBER_DIGITS = 7
INVOICE_NUMBER_BLOCK = 100_000    # carrier k numbers its invoices from (k + 1) x this


def period_bounds(cfg):
    """(first ship date, last ship date, last day an invoice may be received)."""
    p = cfg["period"]
    start = pd.Timestamp(p["start"])
    end = start + pd.DateOffset(months=p["months"]) - pd.Timedelta(days=1)
    return start, end, end + pd.Timedelta(days=p["runoff_days"])


def new_invoice(uid, carrier_id, **fields):
    """A blank invoice record. `lines` is a list of [charge_code, amount]."""
    inv = {"uid": uid, "carrier_id": carrier_id, "shipment_id": "", "invoice_type": "original",
           "parent_uid": None,       # the invoice this one was copied from (duplicate/rebill/balance due)
           "number_uid": uid,        # whose invoice number this one carries (a resend shares it)
           "pro_base": "", "bol_digits": "", "bol_raw": "", "bol_noise": "", "bol_typo": False,
           "ship_date": None, "origin_city": "", "origin_state": "", "dest_city": "",
           "dest_state": "", "weight_lbs": 0, "invoice_date": None, "received_date": None,
           "lines": [], "unknown_lines": [], "traps": [], "errors": [], "superseded": False}
    inv.update(fields)
    return inv


def get_line(inv, code):
    """The [code, amount] line for a charge code, or None."""
    return next((ln for ln in inv["lines"] if ln[0] == code), None)


def total(inv):
    """Invoice total: the sum of its known charge lines, to the cent."""
    return cents(sum(amount for _, amount in inv["lines"]))


def bill(rate_row, mode, weight_lbs, miles, diesel_price, cfg):
    """(linehaul, fsc) the contract says to bill for one shipment at a given weight.

    LTL fuel surcharge is a percentage of linehaul; TL fuel surcharge is $/mile x miles.
    """
    lh = contract_linehaul(rate_row, weight_lbs, miles)
    fsc = (fsc_ltl_amount(lh, diesel_price, cfg) if mode == "LTL"
           else fsc_tl_amount(miles, diesel_price, cfg))
    return lh, fsc


def draw_invoice_dates(delivery_dates, carrier_ids, cfg, rng):
    """Invoice and received dates for invoices raised after delivery.

    Carriers bill some days after delivery: the lag is lognormal around the carrier's median
    (most invoices near the median, a tail of slow ones), capped. The shipper's AP stamps
    receipt a few days later. Anything that would land after the runoff window closes is
    pulled back to its last day, so every invoice arrives inside the window.
    """
    inv_cfg = cfg["invoicing"]
    _, _, runoff_end = period_bounds(cfg)
    medians = np.array([carrier_median(cfg, c) for c in carrier_ids], dtype=float)
    lag = np.clip(np.rint(rng.lognormal(np.log(medians), inv_cfg["lag_sigma"])),
                  1, inv_cfg["max_lag_days"]).astype(int)
    lo, hi = inv_cfg["receipt_delay_days"]
    delay = rng.integers(lo, hi + 1, size=len(lag))
    invoice_dates = [d + pd.Timedelta(days=int(k)) for d, k in zip(delivery_dates, lag)]
    received = [min(d + pd.Timedelta(days=int(k)), runoff_end) for d, k in zip(invoice_dates, delay)]
    invoice_dates = [min(d, r) for d, r in zip(invoice_dates, received)]
    return invoice_dates, received


def carrier_median(cfg, carrier_id):
    """Median billing lag in days for a carrier (a simulator setting, not shipper knowledge)."""
    return next(c["lag_median_days"] for c in cfg["carriers"] if c["id"] == carrier_id)


def new_pro_base(rng, used):
    """A unique 9-digit carrier shipment reference (the PRO number)."""
    while True:
        pro = str(int(rng.integers(0, 10 ** PRO_DIGITS))).zfill(PRO_DIGITS)
        if pro not in used:
            used.add(pro)
            return pro


def build_originals(tables, cfg, rng):
    """One original invoice per shipment: linehaul, fuel surcharge, authorized accessorials.

    Billed weight is the certified reweigh weight when a certificate exists (legitimate, so
    it is labeled the `documented_reweigh` trap), otherwise the shipment weight. Rates come
    from the contract row in force on the ship date, so invoices on an amended lane shipped
    after the effective date carry the new rate (the `rate_amendment` trap).

    Returns (invoices, ctx). ctx[uid] holds the true values the error injector needs
    (contract row, diesel price, reference weight, authorizations).
    """
    shp, lanes = tables["shipments"], tables["lanes"].set_index("lane_id")
    card, diesel = tables["rate_card"], tables["diesel_weekly"]
    certified = tables["reweigh_certificates"].set_index("shipment_id")["certified_weight_lbs"]
    auths = {sid: list(zip(g["code"], g["authorized_amount"]))
             for sid, g in tables["authorizations"].groupby("shipment_id")}

    invoice_dates, received = draw_invoice_dates(shp["delivery_date"].tolist(),
                                                 shp["carrier_id"].tolist(), cfg, rng)
    used_pros = set()
    invoices, ctx = [], {}
    for i, s in enumerate(shp.itertuples(index=False)):
        row = lookup_rate(card, s.carrier_id, s.lane_id, s.ship_date)
        diesel_price = diesel_for_ship_date(diesel, s.ship_date)
        has_cert = s.shipment_id in certified.index
        ref_weight = int(certified[s.shipment_id]) if has_cert else int(s.weight_lbs)
        lh, fsc = bill(row, s.mode, ref_weight, s.miles, diesel_price, cfg)
        lane = lanes.loc[s.lane_id]
        acc = [[code, float(amount)] for code, amount in auths.get(s.shipment_id, [])]
        inv = new_invoice(
            i, s.carrier_id, shipment_id=s.shipment_id, pro_base=new_pro_base(rng, used_pros),
            bol_digits=s.bol, bol_raw=s.bol, ship_date=s.ship_date,
            origin_city=lane["origin_city"], origin_state=lane["origin_state"],
            dest_city=lane["destination_city"], dest_state=lane["destination_state"],
            weight_lbs=ref_weight, invoice_date=invoice_dates[i], received_date=received[i],
            lines=[["LH", lh], ["FSC", fsc]] + acc)
        if has_cert:
            inv["traps"].append(("documented_reweigh", ""))
        if row["version"] == 2:
            inv["traps"].append(("rate_amendment", ""))
        invoices.append(inv)
        ctx[i] = {"shp": s, "row": row, "diesel": diesel_price, "ref_weight": ref_weight,
                  "has_cert": has_cert, "lh_ref": lh,
                  "auth_codes": {code for code, _ in auths.get(s.shipment_id, [])}}
    return invoices, ctx


def assign_ids(invoices, cfg, rng):
    """Invoice numbers and control ids.

    Each carrier numbers its invoices in date order (7 digits, zero-padded, like real invoice
    numbers that pandas would mangle if read as integers). A resent duplicate reuses the
    number of the invoice it repeats. The control id is the transmission id (EDI control
    number / portal document id): unique to every invoice sent, so a resend gets a new one.
    """
    for k, carrier in enumerate(cfg["carriers"]):
        own = sorted((inv for inv in invoices
                      if inv["carrier_id"] == carrier["id"] and inv["number_uid"] == inv["uid"]),
                     key=lambda inv: (inv["invoice_date"], inv["uid"]))
        start = (k + 1) * INVOICE_NUMBER_BLOCK
        for rank, inv in enumerate(own, start=1):
            inv["invoice_number"] = str(start + rank).zfill(INVOICE_NUMBER_DIGITS)
    by_uid = {inv["uid"]: inv for inv in invoices}
    controls = rng.choice(10 ** CONTROL_DIGITS, size=len(invoices), replace=False)
    for inv, control in zip(invoices, controls):
        inv["invoice_number"] = by_uid[inv["number_uid"]]["invoice_number"]
        inv["control_id"] = str(int(control)).zfill(CONTROL_DIGITS)
        inv["invoice_id"] = f"{inv['carrier_id']}:{inv['control_id']}"
    return invoices


def invoice_frame(invoices):
    """One row per invoice with the scalar fields (no line items) as a DataFrame."""
    cols = ["invoice_id", "carrier_id", "invoice_number", "pro_base", "control_id", "invoice_type",
            "shipment_id", "bol_digits", "bol_raw", "ship_date", "origin_city", "dest_city",
            "weight_lbs", "invoice_date", "received_date", "superseded"]
    df = pd.DataFrame([{c: inv[c] for c in cols} for inv in invoices])
    df["total"] = [total(inv) for inv in invoices]
    return df


def line_frame(invoices):
    """One row per known charge line: invoice_id, charge_code, amount."""
    return pd.DataFrame([{"invoice_id": inv["invoice_id"], "charge_code": code, "amount": amount}
                         for inv in invoices for code, amount in inv["lines"]])
