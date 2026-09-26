"""Build the contract rate card the generator writes to data/reference/rate_card.csv.

Building and amending contracts is generator work. *Reading* a contract (rate lookup,
linehaul, fuel surcharge) lives in `freight_audit_lab.contract`, which audit and accruals
import; they never import from this package.
"""

import pandas as pd

from freight_audit_lab.contract import (CWT_PREFIX, STEP_ROUNDING_DECIMALS, break_column,
                                        fsc_ltl_steps)


# ---------------------------------------------------------------- rate card


def build_rate_card(carrier_lanes, lanes, cfg, rng):
    """One contract row per carrier-lane, split in two where the contract was amended.

    LTL carriers price per hundredweight (cwt = 100 lb), and the $/cwt rate falls as the
    shipment gets heavier, so each LTL row carries one rate per weight break. The
    1,000-1,999 lb break is the base: intercept + per_mile x miles (longer lanes cost more
    per cwt), scaled by a carrier factor (some carriers are dearer across the board) and a
    lane noise factor (each lane was negotiated separately). TL carriers price the whole
    truck per mile.
    """
    r = cfg["rates"]
    carriers = [c["id"] for c in cfg["carriers"]]
    carrier_factor = dict(zip(carriers, rng.uniform(*r["carrier_factor_range"], size=len(carriers))))
    lane_noise = rng.uniform(*r["lane_noise_range"], size=len(carrier_lanes))
    miles_by_lane = lanes.set_index("lane_id")["miles"]
    start = pd.Timestamp(cfg["period"]["start"])
    open_end = pd.Timestamp(r["open_ended_to"])

    rows = []
    for i, cl in enumerate(carrier_lanes.itertuples(index=False)):
        factor = carrier_factor[cl.carrier_id] * lane_noise[i]
        row = {"carrier_id": cl.carrier_id, "lane_id": cl.lane_id, "mode": cl.mode,
               "version": 1, "effective_from": start, "effective_to": open_end}
        if cl.mode == "LTL":
            base = (r["ltl"]["base_cwt"]["intercept"]
                    + r["ltl"]["base_cwt"]["per_mile"] * miles_by_lane[cl.lane_id]) * factor
            row["min_charge"] = r["ltl"]["min_charge"]
            row["rate_per_mile"] = float("nan")
            row["deficit_weight_rating"] = bool(r["ltl"]["deficit_weight_rating"])
            for wb in r["ltl"]["weight_breaks"]:
                row[break_column(wb)] = round(base * wb["mult"], 2)
        else:
            row["min_charge"] = r["tl"]["min_charge"]
            row["rate_per_mile"] = round(r["tl"]["rate_per_mile"] * factor, 2)
            row["deficit_weight_rating"] = False
            for wb in r["ltl"]["weight_breaks"]:
                row[break_column(wb)] = float("nan")
        rows.append(row)
    card = pd.DataFrame(rows)
    return apply_amendments(card, cfg, rng)


def apply_amendments(card, cfg, rng):
    """Split amended carrier-lanes into an old row and a new row at the effective date.

    Contracts get renegotiated mid-year. The old rate applies to freight shipped before
    the effective date and the new rate after, which is why every lookup is by ship date.
    """
    am = cfg["rates"]["amendments"]
    effective = pd.Timestamp(am["effective_date"])
    n_amend = int(round(am["share_of_carrier_lanes"] * len(card)))
    amended_idx = sorted(rng.choice(len(card), size=n_amend, replace=False))
    changes = rng.uniform(*am["change_range"], size=n_amend)
    rate_cols = ["rate_per_mile"] + [c for c in card.columns if c.startswith(CWT_PREFIX)]

    new_rows = []
    for idx, change in zip(amended_idx, changes):
        old = card.loc[idx]
        new = old.copy()
        card.loc[idx, "effective_to"] = effective - pd.Timedelta(days=1)
        new["version"] = 2
        new["effective_from"] = effective
        for col in rate_cols:
            if pd.notna(old[col]):
                new[col] = round(old[col] * (1 + change), 2)
        new_rows.append(new)
    card = pd.concat([card, pd.DataFrame(new_rows)], ignore_index=True)
    return card.sort_values(["carrier_id", "lane_id", "effective_from"]).reset_index(drop=True)


def build_fsc_ltl_table(diesel_weekly, cfg):
    """The LTL FSC step table over the diesel range actually seen, for readers."""
    f = cfg["fsc"]["ltl"]
    lo, hi = diesel_weekly["price_per_gallon"].min(), diesel_weekly["price_per_gallon"].max()

    decimals = cfg["diesel"]["price_decimals"]
    rows = []
    for k in range(max(0, fsc_ltl_steps(lo, cfg)), fsc_ltl_steps(hi, cfg) + 1):
        price_from = round(f["base_price"] + k * f["step"], decimals)
        rows.append({"diesel_from": price_from,
                     "diesel_to": round(price_from + f["step"] - 10 ** -decimals, decimals),
                     "fsc_pct": round(k * f["pct_per_step"], STEP_ROUNDING_DECIMALS)})
    return pd.DataFrame(rows)
