"""Contract pricing: rate lookup, the one linehaul formula, and the fuel surcharge schedule.

Pricing logic lives only here. The generator, the re-rater, the audit, and accruals all call
`lookup_rate`, `contract_linehaul`, and the FSC functions, so an invoice can never be
generated with one formula and audited with another. This module deliberately imports
nothing from `generate/`: audit and accrual code need pricing but must never depend on the
synthetic-data generator (a test enforces that).
"""

import math

import pandas as pd

CWT_PREFIX = "cwt_"          # rate card columns cwt_<min>_<max> hold $/cwt per weight break
STEP_ROUNDING_DECIMALS = 6   # guards floor() against 20.999999... float artifacts


def break_column(weight_break):
    """Rate card column name for a weight break, e.g. cwt_1000_1999."""
    return f"{CWT_PREFIX}{weight_break['min']}_{weight_break['max']}"


# ---------------------------------------------------------------- rate lookup


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


def fsc_ltl_steps(diesel_price, cfg):
    """Whole diesel steps above the LTL base price (negative below the base).

    Rounded before floor() because e.g. (3.30 - 1.20) / 0.10 is 20.999... in floating point.
    """
    f = cfg["fsc"]["ltl"]
    return math.floor(round((diesel_price - f["base_price"]) / f["step"], STEP_ROUNDING_DECIMALS))


def fsc_ltl_pct(diesel_price, cfg):
    """LTL fuel surcharge as a fraction of linehaul.

    LTL carriers publish a step table: every `step` dollars that diesel sits above
    `base_price` adds `pct_per_step` to the surcharge. Below the base there is no surcharge.
    """
    f = cfg["fsc"]["ltl"]
    return round(max(0, fsc_ltl_steps(diesel_price, cfg)) * f["pct_per_step"], STEP_ROUNDING_DECIMALS)


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
