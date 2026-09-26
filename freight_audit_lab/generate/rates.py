"""Contract rate card, the one linehaul pricing function, and the fuel surcharge schedule.

Pricing logic lives only here. The generator, the re-rater, and accruals all call
`contract_linehaul` and the FSC functions, so an invoice can never be generated with one
formula and audited with another.
"""

import math

import pandas as pd

CWT_PREFIX = "cwt_"          # rate card columns cwt_<min>_<max> hold $/cwt per weight break
STEP_ROUNDING_DECIMALS = 6   # guards floor() against 20.999999... float artifacts


def break_column(weight_break):
    """Rate card column name for a weight break, e.g. cwt_1000_1999."""
    return f"{CWT_PREFIX}{weight_break['min']}_{weight_break['max']}"


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


def lookup_rate(rate_card, carrier_id, lane_id, ship_date):
    """Return the one rate row in force for this carrier-lane on the ship date.

    Freight is billed at the contract rate effective when it shipped, not when it was
    invoiced, so an amendment dated July 1 does not reprice a June shipment.
    """
    ship_date = pd.Timestamp(ship_date)
    hit = rate_card[(rate_card["carrier_id"] == carrier_id)
                    & (rate_card["lane_id"] == lane_id)
                    & (rate_card["effective_from"] <= ship_date)
                    & (rate_card["effective_to"] >= ship_date)]
    if len(hit) != 1:
        raise LookupError(f"Expected 1 rate row for {carrier_id}/{lane_id} on "
                          f"{ship_date.date()}, found {len(hit)}")
    return hit.iloc[0]


# ---------------------------------------------------------------- linehaul


def ltl_breaks(rate_row):
    """(min_lbs, $/cwt) for each weight break on an LTL rate row, lightest first."""
    breaks = []
    for col, rate in rate_row.items():
        if isinstance(col, str) and col.startswith(CWT_PREFIX):
            min_lbs = int(col[len(CWT_PREFIX):].split("_")[0])
            breaks.append((min_lbs, float(rate)))
    return sorted(breaks)


def contract_linehaul(rate_row, weight_lbs, miles):
    """Contract linehaul charge in dollars for one shipment. The only pricing formula.

    TL: the truck is priced by distance, so linehaul = max(min_charge, $/mile x miles);
    weight does not matter.

    LTL: find the weight break the shipment falls in (the one with the largest minimum
    weight at or below it) and charge $/cwt x weight / 100. With deficit weight rating on,
    also price the shipment as if it weighed the minimum of each heavier break and take the
    cheapest, so adding weight never lowers the bill (otherwise 999 lb at the 500-999 rate
    costs more than 1,000 lb at the cheaper 1,000-1,999 rate). The minimum charge is the
    floor either way.
    """
    if rate_row["mode"] == "TL":
        return round(max(rate_row["min_charge"], rate_row["rate_per_mile"] * miles), 2)

    breaks = ltl_breaks(rate_row)
    applicable = [rate for min_lbs, rate in breaks if min_lbs <= weight_lbs]
    as_weight = applicable[-1] * weight_lbs / 100
    charge = as_weight
    if rate_row["deficit_weight_rating"]:
        for min_lbs, rate in breaks:
            if min_lbs > weight_lbs:
                charge = min(charge, rate * min_lbs / 100)
    return round(max(rate_row["min_charge"], charge), 2)


# ---------------------------------------------------------------- fuel surcharge


def ship_week(date):
    """The Monday on or before a date. FSC uses the diesel price for the ship week."""
    date = pd.Timestamp(date).normalize()
    return date - pd.Timedelta(days=date.weekday())


def diesel_for_ship_date(diesel_weekly, ship_date):
    """Diesel price ($/gal) published for the ship week of this date."""
    week = ship_week(ship_date)
    hit = diesel_weekly.loc[diesel_weekly["week_start"] == week, "price_per_gallon"]
    if hit.empty:
        raise LookupError(f"No diesel price for the week of {week.date()}")
    return float(hit.iloc[0])


def fsc_ltl_pct(diesel_price, cfg):
    """LTL fuel surcharge as a fraction of linehaul.

    LTL carriers publish a step table: every `step` dollars that diesel sits above
    `base_price` adds `pct_per_step` to the surcharge. Below the base there is no surcharge.
    """
    f = cfg["fsc"]["ltl"]
    steps = math.floor(round((diesel_price - f["base_price"]) / f["step"], STEP_ROUNDING_DECIMALS))
    return round(max(0, steps) * f["pct_per_step"], STEP_ROUNDING_DECIMALS)


def fsc_ltl_amount(linehaul, diesel_price, cfg):
    """LTL fuel surcharge in dollars: FSC % applied to linehaul."""
    return round(linehaul * fsc_ltl_pct(diesel_price, cfg), 2)


def fsc_tl_per_mile(diesel_price, cfg):
    """TL fuel surcharge in $/mile: the fuel cost above the peg price, per mile driven.

    A truck gets about `mpg` miles per gallon, so each mile burns 1/mpg gallons, and the
    shipper reimburses the part of the diesel price above the peg built into the base rate.
    """
    f = cfg["fsc"]["tl"]
    return max(0.0, (diesel_price - f["peg_price"]) / f["mpg"])


def fsc_tl_amount(miles, diesel_price, cfg):
    """TL fuel surcharge in dollars: $/mile x lane miles."""
    return round(fsc_tl_per_mile(diesel_price, cfg) * miles, 2)


def build_fsc_ltl_table(diesel_weekly, cfg):
    """The LTL FSC step table over the diesel range actually seen, for readers."""
    f = cfg["fsc"]["ltl"]
    lo, hi = diesel_weekly["price_per_gallon"].min(), diesel_weekly["price_per_gallon"].max()

    def step_of(price):
        return math.floor(round((price - f["base_price"]) / f["step"], STEP_ROUNDING_DECIMALS))

    decimals = cfg["diesel"]["price_decimals"]
    rows = []
    for k in range(max(0, step_of(lo)), step_of(hi) + 1):
        price_from = round(f["base_price"] + k * f["step"], decimals)
        rows.append({"diesel_from": price_from,
                     "diesel_to": round(price_from + f["step"] - 10 ** -decimals, decimals),
                     "fsc_pct": round(k * f["pct_per_step"], STEP_ROUNDING_DECIMALS)})
    return pd.DataFrame(rows)
