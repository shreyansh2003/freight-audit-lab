"""Shipments, their legitimate accessorials (with authorizations), and reweigh certificates.

These are the shipper's own records: what it shipped, what extra services it approved,
and which weights a carrier formally certified. Audit and accruals later compare carrier
invoices against exactly these files.
"""

import math

import numpy as np
import pandas as pd

BOL_DIGITS = 8   # canonical bill of lading numbers are 8 digits, zero-padded


def monthly_counts(n, weights):
    """Split n shipments across months in proportion to weights, summing exactly to n.

    Largest-remainder rounding: floor every share, then give the leftover shipments to the
    months with the biggest fractional parts. Counts don't depend on the seed.
    """
    raw = [n * w for w in weights]
    counts = [math.floor(x) for x in raw]
    leftovers = sorted(range(len(raw)), key=lambda i: raw[i] - counts[i], reverse=True)
    for i in leftovers[: n - sum(counts)]:
        counts[i] += 1
    return counts


def build_shipments(lanes, carrier_lanes, facilities, cfg, rng):
    """Draw shipments_n shipments with date, lane, mode, carrier, weight, transit, BOL.

    Weights: LTL freight is mostly small with a long tail of heavy pallets, so LTL weight
    is lognormal around the median and clipped; a TL load fills most of a trailer, so TL
    weight is uniform over a heavy range. Both arrays are drawn for every shipment and the
    one matching its mode is kept, which keeps the random draws in a fixed order.
    """
    s = cfg["shipments"]
    n = s["n"]
    start = pd.Timestamp(cfg["period"]["start"])

    ship_dates = []
    for m, count in enumerate(monthly_counts(n, s["monthly_weights"])):
        month_start = start + pd.DateOffset(months=m)
        days = month_start.days_in_month
        ship_dates += [month_start + pd.Timedelta(days=int(d)) for d in rng.integers(0, days, size=count)]

    lane_idx = rng.integers(0, len(lanes), size=n)
    is_tl = rng.random(n) < s["tl_share"]
    carrier_pick = rng.random(n)
    w_ltl = s["ltl_weight_lbs"]
    ltl_weight = np.clip(rng.lognormal(math.log(w_ltl["median"]), w_ltl["sigma"], size=n),
                         w_ltl["min"], w_ltl["max"])
    tl_weight = rng.integers(s["tl_weight_lbs"]["min"], s["tl_weight_lbs"]["max"] + 1, size=n)
    extra_days = rng.integers(s["transit_extra_days"][0], s["transit_extra_days"][1] + 1, size=n)
    bols = rng.choice(10 ** BOL_DIGITS, size=n, replace=False)

    carriers_for = carrier_lanes.groupby(["lane_id", "mode"])["carrier_id"].apply(list).to_dict()
    cost_center = facilities.set_index("facility_id")["cost_center"]

    rows = []
    for i in range(n):
        lane = lanes.iloc[lane_idx[i]]
        mode = "TL" if is_tl[i] else "LTL"
        options = carriers_for[(lane["lane_id"], mode)]
        transit = math.ceil(lane["miles"] / s["miles_per_day"][mode]) + int(extra_days[i])
        rows.append({
            "bol": str(bols[i]).zfill(BOL_DIGITS),
            "ship_date": ship_dates[i],
            "delivery_date": ship_dates[i] + pd.Timedelta(days=transit),
            "transit_days": transit,
            "lane_id": lane["lane_id"],
            "origin_id": lane["origin_id"],
            "destination_id": lane["destination_id"],
            "miles": int(lane["miles"]),
            "mode": mode,
            "carrier_id": options[int(carrier_pick[i] * len(options))],
            "weight_lbs": int(tl_weight[i]) if is_tl[i] else int(round(ltl_weight[i])),
            "cost_center": cost_center[lane["origin_id"]],
        })
    df = pd.DataFrame(rows).sort_values("ship_date", kind="mergesort").reset_index(drop=True)
    df.insert(0, "shipment_id", [f"SHP{i:05d}" for i in range(1, n + 1)])
    return df


def build_authorizations(shipments, cfg, rng):
    """Legitimate accessorials and the authorization the shipper recorded for each.

    Liftgate and residential delivery are LTL services requested at booking; detention is
    paid when a TL driver waits past the free time, at the hourly rate for billable hours
    (total hours - free hours). Each gets an authorization dated between ship and delivery.
    """
    n = len(shipments)
    rows = []
    for mode_key, mode in (("ltl", "LTL"), ("tl", "TL")):
        for name, spec in cfg["accessorials"][mode_key].items():
            hit = (rng.random(n) < spec["prob"]) & (shipments["mode"] == mode).to_numpy()
            if "hourly" in spec:
                total_hours = rng.integers(spec["free_hours"] + 1, spec["max_hours"] + 1, size=n)
                amounts = (total_hours - spec["free_hours"]) * spec["hourly"]
            else:
                amounts = np.full(n, spec["amount"])
            auth_offset = rng.random(n)
            for i in np.flatnonzero(hit):
                shp = shipments.iloc[i]
                days = int(auth_offset[i] * (shp["transit_days"] + 1))
                rows.append({"shipment_id": shp["shipment_id"], "code": name.upper(),
                             "authorized_amount": round(float(amounts[i]), 2),
                             "authorized_at": shp["ship_date"] + pd.Timedelta(days=days)})
    df = pd.DataFrame(rows).sort_values(["shipment_id", "code"], kind="mergesort").reset_index(drop=True)
    df.insert(0, "auth_id", [f"AUTH{i:05d}" for i in range(1, len(df) + 1)])
    return df


def build_reweighs(shipments, cfg, rng):
    """Reweigh certificates: the carrier weighed the freight and certified a higher weight.

    Billing the certified weight is legitimate, so audit must compare billed weight with
    the certified weight when a certificate exists (this is the documented-reweigh trap).
    """
    t = cfg["traps"]
    ltl_idx = np.flatnonzero((shipments["mode"] == "LTL").to_numpy())
    k = int(round(t["reweigh_share_ltl"] * len(ltl_idx)))
    chosen = np.sort(rng.choice(ltl_idx, size=k, replace=False))
    factors = rng.uniform(*t["reweigh_factor_range"], size=k)
    cert_offset = rng.random(k)
    rows = []
    for j, i in enumerate(chosen):
        shp = shipments.iloc[i]
        days = int(cert_offset[j] * (shp["transit_days"] + 1))
        rows.append({"certificate_id": f"RW{j + 1:05d}", "shipment_id": shp["shipment_id"],
                     "certified_weight_lbs": int(round(shp["weight_lbs"] * factors[j])),
                     "certified_at": shp["ship_date"] + pd.Timedelta(days=days)})
    return pd.DataFrame(rows)
