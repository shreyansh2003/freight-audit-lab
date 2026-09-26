"""Audit rules: one function per error type.

Every rule has the same signature, `(norm, rerated, ref, cfg) -> DataFrame`:
  norm     {"invoices", "invoice_lines"} from normalize.py
  rerated  {"invoices", "accessorials"} from rerate.py
  ref      the shipper's reference tables (data/reference/)
  cfg      config.yaml; tolerances are read from cfg["audit"]["tolerances"]
and returns one row per flag with the columns in FLAG_COLUMNS. `reason` is a readable sentence
with the numbers. Every dollar figure here is an *estimate* of what the carrier overbilled;
whether it is recoverable is decided by the engine.

The rules only ever flag overbilling. Undercharges are out of scope (see README limitations).
"""

import numpy as np
import pandas as pd

FLAG_COLUMNS = ["invoice_id", "error_type", "reason", "dollar_impact_estimate", "shipment_id",
                "billed_value", "expected_value", "related_invoice_id"]
MONEY_DECIMALS = 2           # money is compared at cent precision so float noise never decides a flag


def usd(x):
    """$1,234.50 (with a leading minus if negative)."""
    return f"-${abs(x):,.2f}" if x < 0 else f"${x:,.2f}"


def flag(invoice_id, error_type, reason, impact, shipment_id="", billed=np.nan, expected=np.nan, related=""):
    """One flag as a dict; `shipment_id` may be blank for unmatched invoices."""
    return {"invoice_id": invoice_id, "error_type": error_type, "reason": reason,
            "dollar_impact_estimate": round(float(impact), 2), "shipment_id": shipment_id or "",
            "billed_value": billed, "expected_value": expected, "related_invoice_id": related}


def flags_frame(rows):
    """Flags as a DataFrame with the standard columns (empty is fine)."""
    return pd.DataFrame(rows, columns=FLAG_COLUMNS)


# ---------------------------------------------------------------- 1. duplicates


def duplicate_key(inv):
    """What makes two invoices 'about the same freight': the matched shipment, or for invoices
    that matched no shipment the carrier plus canonical BOL. Invoices with neither get a key
    of their own, so they can never be grouped."""
    by_shipment = inv["shipment_id"].where(inv["match_method"] != "unmatched")
    by_bol = ("BOL:" + inv["carrier_id"] + ":" + inv["bol_canonical"]).where(inv["bol_canonical"].notna())
    return by_shipment.fillna(by_bol).fillna("INV:" + inv["invoice_id"])


def duplicate_invoice(norm, rerated, ref, cfg):
    """Flag the second and later copies of the same bill.

    A carrier can resend an invoice or key it in again, and AP pays both if nobody looks. Among
    live (non-superseded) original and rebill invoices for the same freight, a later copy is a
    duplicate when its total is within `duplicate_amount` of an earlier one and it arrived
    within `duplicate_window_days`; the earliest received is kept. Any repeat of the same
    carrier + invoice number is also flagged. A rebill replaces its parent (the parent is
    superseded, so is not in the pool) and a balance-due invoice is a different charge on the
    same shipment, so neither is ever a duplicate of its parent.
    """
    tol = cfg["audit"]["tolerances"]
    inv = norm["invoices"]
    pool = inv[~inv["is_superseded"] & inv["invoice_type"].isin(["original", "rebill"])].copy()
    pool["_key"] = duplicate_key(pool)
    pool = pool.sort_values(["received_date", "invoice_id"])
    rows = {}
    shared = pool[pool.duplicated("_key", keep=False)]          # only groups of 2+ can hold a duplicate
    for _, group in shared.groupby("_key", sort=False):
        earlier = []
        for r in group.itertuples(index=False):
            same_bill = next((e for e in earlier
                              if round(abs(e.total - r.total), MONEY_DECIMALS) <= tol["duplicate_amount"]
                              and (r.received_date - e.received_date).days <= tol["duplicate_window_days"]), None)
            if same_bill is not None:
                rows[r.invoice_id] = (same_bill, "same shipment or BOL, total within tolerance")
            earlier.append(r)
    shared = pool[pool.duplicated(["carrier_id", "invoice_number"], keep=False)]
    for _, group in shared.groupby(["carrier_id", "invoice_number"], sort=False):
        for r in group.iloc[1:].itertuples(index=False):
            rows.setdefault(r.invoice_id, (group.iloc[0], "same carrier and invoice number"))

    return flags_frame([duplicate_invoice_flag(r, *rows[r.invoice_id])
                        for r in pool[pool["invoice_id"].isin(rows)].itertuples(index=False)])


def duplicate_invoice_flag(dup, first, why):
    """The flag for invoice `dup` being a repeat of the earlier invoice `first`."""
    days = (dup.received_date - first.received_date).days
    return flag(dup.invoice_id, "duplicate_invoice",
                f"Duplicate of invoice {first.invoice_number} ({why}): total {usd(dup.total)} vs "
                f"{usd(first.total)}, received {days} days after the first copy ({first.received_date.date()})",
                dup.total, dup.shipment_id if dup.match_method != "unmatched" else "",
                dup.total, first.total, first.invoice_id)


# ---------------------------------------------------------------- 2. phantoms


def phantom_invoice(norm, rerated, ref, cfg):
    """Flag live invoices that match no shipment.

    If neither the BOL nor the carrier/lane/date/weight fallback finds the freight, the shipper
    has no record of receiving it, so the whole bill is in question.
    """
    inv = norm["invoices"]
    hit = inv[~inv["is_superseded"] & (inv["match_method"] == "unmatched")]
    return flags_frame([
        flag(r.invoice_id, "phantom_invoice",
             f"No shipment matches BOL {r.bol_raw} ({r.origin} → {r.destination}, shipped "
             f"{r.ship_date.date()}, {r.billed_weight_lbs:,} lb); whole invoice {usd(r.total)} in question",
             r.total, "", r.total, 0.0)
        for r in hit.itertuples(index=False)])


# ---------------------------------------------------------------- 3. rate


def rate_overcharge(norm, rerated, ref, cfg):
    """Flag linehaul billed above the contract linehaul at the billed weight.

    Carriers are held to the rate row in force on the ship date. The gap must beat both a
    dollar floor and a percentage of the contract linehaul, so carrier rounding noise passes.
    The dollar estimate adds the LTL fuel surcharge the overcharge drags along with it.
    """
    tol = cfg["audit"]["tolerances"]
    r = rerated["invoices"]
    gap = (r["billed_lh"] - r["lh_contract_at_billed_wt"]).round(MONEY_DECIMALS)
    allowed = np.maximum(tol["rate_abs"], tol["rate_pct"] * r["lh_contract_at_billed_wt"]).round(6)
    out = []
    for x, g in zip(r[gap > allowed].itertuples(index=False), gap[gap > allowed]):
        fuel_note = f"; est. {usd(x.rate_impact_estimate)} with fuel surcharge" if x.mode == "LTL" else ""
        out.append(flag(x.invoice_id, "rate_overcharge",
                        f"Linehaul {usd(x.billed_lh)} vs contract {usd(x.lh_contract_at_billed_wt)} ({x.lane}, "
                        f"rate effective {x.rate_effective_from.date()}): +{usd(g)} "
                        f"(+{g / x.lh_contract_at_billed_wt:.1%}){fuel_note}",
                        x.rate_impact_estimate, x.shipment_id, x.billed_lh, x.lh_contract_at_billed_wt))
    return flags_frame(out)


# ---------------------------------------------------------------- 4. fuel surcharge


def fsc_mismatch(norm, rerated, ref, cfg):
    """Flag a fuel surcharge above what the contract schedule gives for the billed linehaul.

    LTL carriers bill FSC as a percentage of linehaul from a diesel step table, so the test is
    the implied percentage vs the expected one (in percentage points) plus a dollar floor. TL
    carriers bill $/mile x miles, so the test is the dollar gap against the larger of a dollar
    floor and a percentage of the expected fuel: carrier rounding noise scales with the size of the
    fuel line, so the tolerance does too (same idea as the rate rule).
    """
    tol = cfg["audit"]["tolerances"]
    r = rerated["invoices"]
    impact = r["fsc_impact_estimate"]
    implied = (r["billed_fsc"] / r["billed_lh"].where(r["billed_lh"] > 0)) * 100
    pp_over = (implied - r["fsc_pct_expected"] * 100).round(4)
    ltl_hit = (r["mode"] == "LTL") & (pp_over > tol["fsc_ltl_pp"]) & (impact > tol["fsc_min_dollars"])
    tl_allowed = np.maximum(tol["fsc_tl_abs"], tol["fsc_tl_pct"] * r["fsc_expected"]).round(6)
    tl_hit = (r["mode"] == "TL") & (impact > tl_allowed)
    out = []
    for x, over in zip(r[ltl_hit | tl_hit].itertuples(index=False), pp_over[ltl_hit | tl_hit]):
        if x.mode == "LTL":
            detail = (f"{x.billed_fsc / x.billed_lh:.1%} of linehaul vs expected {x.fsc_pct_expected:.1%} "
                      f"(+{over:.2f} pp)")
        else:
            detail = (f"${x.billed_fsc / x.miles:.3f}/mi vs expected ${x.fsc_expected / x.miles:.3f}/mi "
                      f"over {x.miles:,} mi")
        out.append(flag(x.invoice_id, "fsc_mismatch",
                        f"Fuel surcharge {usd(x.billed_fsc)} vs expected {usd(x.fsc_expected)} ({x.mode}, "
                        f"diesel ${x.diesel_price:.3f}/gal for week of {x.diesel_week.date()}; {detail}): "
                        f"+{usd(x.fsc_impact_estimate)}",
                        x.fsc_impact_estimate, x.shipment_id, x.billed_fsc, x.fsc_expected))
    return flags_frame(out)


# ---------------------------------------------------------------- 5. accessorials


def unauthorized_accessorial(norm, rerated, ref, cfg):
    """Flag accessorial charges (liftgate, residential, detention) the shipper never authorized.

    The test is whether an authorization for that shipment and code was recorded on or before
    the as-of date in the accessorial table. Authorizations recorded after the invoice date
    are legitimate late paperwork and are counted as a process metric by the engine.
    """
    acc = rerated["accessorials"]
    return flags_frame([
        flag(a.invoice_id, "unauthorized_accessorial",
             f"{a.charge_code.title()} {usd(a.amount)} billed with no authorization on file for "
             f"{a.shipment_id} as of {a.authorization_as_of.date()}",
             a.amount, a.shipment_id, a.amount, 0.0)
        for a in acc[acc["auth_status"] == "unauthorized"].itertuples(index=False)])


# ---------------------------------------------------------------- 6. weight


def weight_overbilling(norm, rerated, ref, cfg):
    """Flag LTL invoices billed at more than the reference weight.

    LTL is priced by weight, so a heavier billed weight raises the bill. The reference weight
    is the certified reweigh weight when a certificate exists (a documented reweigh is
    legitimate) and the shipment's own weight otherwise. TL is priced by distance, so weight
    does not apply.
    """
    tol = cfg["audit"]["tolerances"]
    r = rerated["invoices"]
    hit = r[(r["mode"] == "LTL") & (r["billed_weight_lbs"] > r["reference_weight_lbs"] * (1 + tol["weight_pct"]))]
    out = []
    for x in hit.itertuples(index=False):
        source = "certified reweigh weight" if x.has_certificate else "shipment weight, no reweigh certificate"
        out.append(flag(x.invoice_id, "weight_overbilling",
                        f"Billed weight {x.billed_weight_lbs:,} lb vs reference {x.reference_weight_lbs:,} lb "
                        f"({source}): +{x.billed_weight_lbs / x.reference_weight_lbs - 1:.1%}; linehaul at billed "
                        f"weight {usd(x.lh_contract_at_billed_wt)} vs {usd(x.lh_contract_at_ref_wt)}, "
                        f"est. +{usd(x.weight_impact_estimate)} with fuel surcharge",
                        x.weight_impact_estimate, x.shipment_id, x.billed_weight_lbs, x.reference_weight_lbs))
    return flags_frame(out)


RULES = [duplicate_invoice, phantom_invoice, rate_overcharge, fsc_mismatch, unauthorized_accessorial,
         weight_overbilling]
