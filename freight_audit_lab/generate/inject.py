"""Billing errors injected onto original invoices, each labeled with a type, mode, and true impact.

Errors go on original invoices only, and never on an original that a rebill supersedes. Each
draw's dollar impact is computed from true values (what the contract says vs what was
billed). An injection whose impact is <= $0.01 is re-drawn up to `errors.max_redraws` times
and then skipped, so a $0 error is never labeled.

Impacts follow the same decomposition the Stage 4 re-rater uses, so nothing double counts:
  weight impact = (LH at billed weight - LH at reference weight)          x (1 + FSC% for LTL)
  rate impact   = (billed LH - LH at billed weight)                       x (1 + FSC% for LTL)
  fsc impact    = billed FSC - expected FSC on the billed LH
"""

import copy
import math

import numpy as np
import pandas as pd

from freight_audit_lab.contract import (cents, contract_linehaul, diesel_for_ship_date, fsc_ltl_amount,
                                        fsc_ltl_pct, fsc_tl_amount, lookup_rate)
from freight_audit_lab.generate.invoices import (bill, draw_invoice_dates, get_line, new_invoice,
                                                 new_pro_base, period_bounds, total)

MIN_IMPACT = 0.01   # an error must be worth more than this to be labeled


# ------------------------------------------------------------ choosing an error and its mode


def systemic_overrides(cfg, carrier_id, error_type, ship_date):
    """{mode: rate} for systemic issues covering this carrier, error type, and ship date.

    A systemic issue is a carrier having a bad stretch: inside its window the rate for that
    error type and mode is replaced by `rate`. The dispute summary should later spot it.
    """
    return {s["mode"]: s["rate"] for s in cfg["errors"]["systemic_issues"]
            if s["carrier"] == carrier_id and s["error_type"] == error_type
            and pd.Timestamp(s["start"]) <= ship_date <= pd.Timestamp(s["end"])}


def pick_mode(probs, u_hit, u_mode):
    """Choose a mode from {mode: probability}, or None if this invoice has no error.

    The invoice has the error if `u_hit` falls under the summed probability; `u_mode` then
    chooses the mode in proportion to its probability. Both are uniform draws made up front.
    """
    total_p = sum(probs.values())
    if total_p <= 0 or u_hit >= min(total_p, 1.0):
        return None
    x = u_mode * total_p
    for mode, p in probs.items():
        x -= p
        if x < 0:
            return mode
    return mode


def error_probs(cfg, error_type, carrier_id, shares, ship_date):
    """{mode: probability} for one invoice: config rate x carrier multiplier x mode share,
    with any systemic issue replacing that mode's probability."""
    mult = next(c["error_multiplier"] for c in cfg["carriers"] if c["id"] == carrier_id)
    base = cfg["errors"][error_type] * mult
    probs = {mode: base * share for mode, share in shares.items()}
    probs.update(systemic_overrides(cfg, carrier_id, error_type, ship_date))
    return probs


# ------------------------------------------------------------ one draw per error type


def draw_weight(c, mode, cfg, rng):
    """Inflate the billed weight (LTL, no certificate). Returns (weight, LH at that weight, impact)."""
    key = "weight_sub_tolerance" if mode == "sub_tolerance" else "weight_inflation"   # mode: inflation | sub_tolerance
    for _ in range(cfg["errors"]["max_redraws"]):
        weight = int(round(c["ref_weight"] * (1 + rng.uniform(*cfg["errors"]["magnitudes"][key]))))
        lh = contract_linehaul(c["row"], weight, c["shp"].miles)
        impact = cents((lh - c["lh_ref"]) * (1 + c["pct"]))
        if impact > MIN_IMPACT:
            return weight, lh, impact
    return None


def draw_rate(c, mode, lh_at_weight, cfg, rng):
    """Overcharge linehaul. Returns (billed LH, impact)."""
    mult = 1 + c["pct"] if c["shp"].mode == "LTL" else 1.0
    if mode == "stale_rate":            # the pre-amendment (higher) rate, billed after the cut
        billed = contract_linehaul(c["old_row"], c["billed_weight"], c["shp"].miles)
        impact = cents((billed - lh_at_weight) * mult)
        return (billed, impact) if impact > MIN_IMPACT else None
    key = "rate_sub_tolerance" if mode == "sub_tolerance" else "rate_markup"
    for _ in range(cfg["errors"]["max_redraws"]):
        billed = cents(lh_at_weight * (1 + rng.uniform(*cfg["errors"]["magnitudes"][key])))
        impact = cents((billed - lh_at_weight) * mult)
        if impact > MIN_IMPACT:
            return billed, impact
    return None


def draw_fsc(c, mode, billed_lh, expected_fsc, tables, cfg, rng):
    """Bill the wrong fuel surcharge. Returns (billed FSC, impact)."""
    shp, mag = c["shp"], cfg["errors"]["magnitudes"]
    is_ltl = shp.mode == "LTL"
    for _ in range(cfg["errors"]["max_redraws"]):
        if mode == "wrong_week":        # a later week's diesel price
            k = int(rng.integers(mag["fsc_wrong_week_offset"][0], mag["fsc_wrong_week_offset"][1] + 1))
            price = diesel_for_ship_date(tables["diesel_weekly"], shp.ship_date + pd.Timedelta(weeks=k))
            billed = (fsc_ltl_amount(billed_lh, price, cfg) if is_ltl
                      else fsc_tl_amount(shp.miles, price, cfg))
        elif mode == "wrong_step" and is_ltl:   # extra steps on the carrier's FSC table
            k = int(rng.integers(mag["fsc_wrong_steps"][0], mag["fsc_wrong_steps"][1] + 1))
            billed = cents(billed_lh * (c["pct"] + k * cfg["fsc"]["ltl"]["pct_per_step"]))
        elif mode == "wrong_step":              # TL: an inflated diesel price
            price = c["diesel"] * (1 + rng.uniform(*mag["fsc_tl_price_inflation"]))
            billed = fsc_tl_amount(shp.miles, price, cfg)
        elif is_ltl:                            # sub_tolerance, LTL: a few hundredths of a point
            billed = cents(billed_lh * (c["pct"] + rng.uniform(*mag["fsc_sub_tolerance_pp"]) / 100))
        else:                                   # sub_tolerance, TL: a dollar or so
            billed = cents(expected_fsc + rng.uniform(*mag["fsc_sub_tolerance_tl_dollars"]))
        impact = cents(billed - expected_fsc)
        if impact > MIN_IMPACT:
            return billed, impact
    return None


def draw_accessorial(c, code, cfg, rng):
    """Amount of an accessorial the shipper never authorized (liftgate/residential/detention)."""
    spec = cfg["accessorials"]["ltl" if code != "DETENTION" else "tl"][code.lower()]
    if code == "DETENTION":
        billable = int(rng.integers(1, spec["max_hours"] - spec["free_hours"] + 1))
        return cents(billable * spec["hourly"])
    return cents(spec["amount"])


# ------------------------------------------------------------ the injection pass


def inject_errors(invoices, ctx, tables, cfg, rng):
    """Inject rate, FSC, weight, and unauthorized-accessorial errors on eligible originals.

    Order per invoice: weight, then rate, then fuel surcharge, then accessorial, each
    building on the last so one invoice can carry several errors with additive impacts.
    All hit/mode uniforms are drawn up front so the random stream has a fixed layout.
    """
    n = len(invoices)
    s = cfg["errors"]["sub_tolerance_share"]
    u = {name: rng.random(n) for name in ("w_hit", "w_mode", "r_hit", "r_mode", "f_hit", "f_mode",
                                         "a_hit", "a_pick")}
    amend_date = pd.Timestamp(cfg["rates"]["amendments"]["effective_date"])
    for i, inv in enumerate(invoices):
        if inv["superseded"]:
            continue
        c = ctx[i]
        shp, is_ltl = c["shp"], c["shp"].mode == "LTL"
        c["pct"] = fsc_ltl_pct(c["diesel"], cfg) if is_ltl else 0.0
        c["billed_weight"] = c["ref_weight"]
        lh_at_weight, billed_lh = c["lh_ref"], c["lh_ref"]

        # weight overbilling: LTL only, and only where no reweigh certificate justifies the weight
        if is_ltl and not c["has_cert"]:
            probs = error_probs(cfg, "weight_overbilling", shp.carrier_id,
                                {"inflation": 1 - s, "sub_tolerance": s}, shp.ship_date)
            mode = pick_mode(probs, u["w_hit"][i], u["w_mode"][i])
            drawn = draw_weight(c, mode, cfg, rng) if mode else None
            if drawn:
                c["billed_weight"], lh_at_weight, impact = drawn
                billed_lh = lh_at_weight
                inv["errors"].append(("weight_overbilling", mode, impact))

        # rate overcharge; stale_rate needs an amended lane whose new rate is lower
        lowered = c["row"]["version"] == 2 and shp.ship_date >= amend_date
        if lowered:
            c["old_row"] = lookup_rate(tables["rate_card"], shp.carrier_id, shp.lane_id,
                                       amend_date - pd.Timedelta(days=1))
            lowered = contract_linehaul(c["old_row"], c["billed_weight"], shp.miles) > lh_at_weight
        stale = cfg["errors"]["stale_rate_share"] if lowered else 0.0
        probs = error_probs(cfg, "rate_overcharge", shp.carrier_id,
                            {"sub_tolerance": s, "stale_rate": (1 - s) * stale,
                             "markup": (1 - s) * (1 - stale)}, shp.ship_date)
        mode = pick_mode(probs, u["r_hit"][i], u["r_mode"][i])
        drawn = draw_rate(c, mode, lh_at_weight, cfg, rng) if mode else None
        if drawn:
            billed_lh, impact = drawn
            inv["errors"].append(("rate_overcharge", mode, impact))
            if mode == "stale_rate":
                inv["traps"] = [t for t in inv["traps"] if t[0] != "rate_amendment"]

        # fuel surcharge: the carrier applies its FSC to whatever linehaul it billed
        expected_fsc = (fsc_ltl_amount(billed_lh, c["diesel"], cfg) if is_ltl
                        else fsc_tl_amount(shp.miles, c["diesel"], cfg))
        probs = error_probs(cfg, "fsc_mismatch", shp.carrier_id,
                            {"sub_tolerance": s, "wrong_week": (1 - s) * cfg["errors"]["fsc_wrong_week_share"],
                             "wrong_step": (1 - s) * (1 - cfg["errors"]["fsc_wrong_week_share"])},
                            shp.ship_date)
        mode = pick_mode(probs, u["f_hit"][i], u["f_mode"][i])
        drawn = draw_fsc(c, mode, billed_lh, expected_fsc, tables, cfg, rng) if mode else None
        billed_fsc = expected_fsc
        if drawn:
            billed_fsc, impact = drawn
            inv["errors"].append(("fsc_mismatch", mode, impact))

        # accessorial with no authorization on file
        codes = ["LIFTGATE", "RESIDENTIAL"] if is_ltl else ["DETENTION"]
        free = [code for code in codes if code not in c["auth_codes"]]
        mult = next(x["error_multiplier"] for x in cfg["carriers"] if x["id"] == shp.carrier_id)
        extra = None
        if free and u["a_hit"][i] < cfg["errors"]["unauthorized_accessorial"] * mult:
            code = free[int(u["a_pick"][i] * len(free))]
            extra = [code, draw_accessorial(c, code, cfg, rng)]
            inv["errors"].append(("unauthorized_accessorial", code.lower(), extra[1]))

        get_line(inv, "LH")[1] = billed_lh
        get_line(inv, "FSC")[1] = billed_fsc
        inv["weight_lbs"] = c["billed_weight"]
        if extra:
            inv["lines"].append(extra)


# ------------------------------------------------------------ duplicates and phantoms


def build_duplicates(invoices, ctx, cfg, rng):
    """Resend or re-key some clean originals, received 5-45 days after the first copy.

    A resend reuses the invoice number (a new control id, same document); a re-keyed copy gets
    a new invoice number and a new invoice date. Duplicates are drawn only from originals that
    carry no other injected error, so a duplicate's label is just `duplicate_invoice`.
    True impact is the duplicate's whole total.
    """
    e = cfg["errors"]
    lo, hi = e["duplicate_lag_days"]
    _, _, runoff_end = period_bounds(cfg)
    n = len(invoices)
    u_hit, u_mode = rng.random(n), rng.random(n)
    d_lo, d_hi = cfg["invoicing"]["receipt_delay_days"]
    for i in range(n):
        orig = invoices[i]
        if (orig["invoice_type"] != "original" or orig["superseded"] or orig["errors"]
                or orig["received_date"] + pd.Timedelta(days=hi) > runoff_end):
            continue
        mult = next(x["error_multiplier"] for x in cfg["carriers"] if x["id"] == orig["carrier_id"])
        if u_hit[i] >= e["duplicate_invoice"] * mult:
            continue
        resend = u_mode[i] < e["duplicate_resend_share"]
        dup = copy.deepcopy(orig)
        dup.update(uid=len(invoices), parent_uid=i, traps=[t for t in orig["traps"]
                                                            if t[0] in ("bol_format", "bol_typo")],
                   errors=[], unknown_lines=[])
        dup["number_uid"] = orig["uid"] if resend else dup["uid"]
        dup["received_date"] = orig["received_date"] + pd.Timedelta(days=int(rng.integers(lo, hi + 1)))
        if not resend:
            dup["invoice_date"] = max(dup["received_date"] - pd.Timedelta(days=int(rng.integers(d_lo, d_hi + 1))),
                                      orig["invoice_date"])
        dup["errors"] = [("duplicate_invoice", "resend" if resend else "rekeyed", total(dup))]
        invoices.append(dup)


def build_phantoms(invoices, tables, cfg, rng):
    """Invoices for a BOL that matches no shipment, on a lane the carrier really serves.

    One chance per shipment slot at `phantom_invoice x carrier multiplier`. The BOL is a random
    8-digit number not in shipments.csv. The date and weight are re-drawn if any real shipment
    would satisfy the fallback match (same carrier and lane, ship date within +/-N days, weight
    within X%), so a phantom is unambiguously phantom. True impact is the invoice total.
    """
    shp, card, lanes = tables["shipments"], tables["rate_card"], tables["lanes"].set_index("lane_id")
    fb = cfg["normalization"]["fallback_match"]
    start, end, _ = period_bounds(cfg)
    n_days = (end - start).days + 1
    mult = {c["id"]: c["error_multiplier"] for c in cfg["carriers"]}
    mode_of = {c["id"]: c["mode"] for c in cfg["carriers"]}
    lanes_of = card.groupby("carrier_id")["lane_id"].unique().to_dict()
    same_lane = {k: g[["ship_date", "weight_lbs"]].to_numpy()
                 for k, g in shp.groupby(["carrier_id", "origin_id", "destination_id"])}
    existing = set(shp["bol"])
    used_pros = {inv["pro_base"] for inv in invoices}
    w_ltl, w_tl = cfg["shipments"]["ltl_weight_lbs"], cfg["shipments"]["tl_weight_lbs"]

    hit = rng.random(len(shp)) < np.array([cfg["errors"]["phantom_invoice"] * mult[c] for c in shp["carrier_id"]])
    for carrier_id in shp["carrier_id"][hit]:
        mode = mode_of[carrier_id]
        for _ in range(cfg["errors"]["max_redraws"] * 4):
            lane_id = str(rng.choice(lanes_of[carrier_id]))
            lane = lanes.loc[lane_id]
            ship_date = start + pd.Timedelta(days=int(rng.integers(0, n_days)))
            weight = (int(rng.integers(w_tl["min"], w_tl["max"] + 1)) if mode == "TL" else
                      int(np.clip(round(rng.lognormal(math.log(w_ltl["median"]), w_ltl["sigma"])),
                                  w_ltl["min"], w_ltl["max"])))
            near = same_lane.get((carrier_id, lane["origin_id"], lane["destination_id"]), [])
            if any(abs((d - ship_date).days) <= fb["ship_date_days"]
                   and abs(w - weight) <= fb["weight_pct"] * max(w, weight) for d, w in near):
                continue
            break
        else:
            continue
        bol = str(int(rng.integers(0, 10 ** 8))).zfill(8)
        while bol in existing:
            bol = str(int(rng.integers(0, 10 ** 8))).zfill(8)
        existing.add(bol)
        transit = math.ceil(lane["miles"] / cfg["shipments"]["miles_per_day"][mode]) + int(
            rng.integers(cfg["shipments"]["transit_extra_days"][0], cfg["shipments"]["transit_extra_days"][1] + 1))
        (invoice_date,), (received,) = draw_invoice_dates([ship_date + pd.Timedelta(days=transit)],
                                                           [carrier_id], cfg, rng)
        row = lookup_rate(card, carrier_id, lane_id, ship_date)
        lh, fsc = bill(row, mode, weight, int(lane["miles"]),
                       diesel_for_ship_date(tables["diesel_weekly"], ship_date), cfg)
        inv = new_invoice(len(invoices), carrier_id, pro_base=new_pro_base(rng, used_pros),
                          bol_digits=bol, bol_raw=bol, ship_date=ship_date,
                          origin_city=lane["origin_city"], origin_state=lane["origin_state"],
                          dest_city=lane["destination_city"], dest_state=lane["destination_state"],
                          weight_lbs=weight, invoice_date=invoice_date, received_date=received,
                          lines=[["LH", lh], ["FSC", fsc]])
        inv["errors"] = [("phantom_invoice", "", total(inv))]
        invoices.append(inv)
