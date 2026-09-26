"""Weekly diesel price series: synthetic by default, or the public EIA series.

Fuel surcharges are indexed to the weekly national diesel price, so every FSC in the
project comes from this one Monday-dated series.
"""

import csv
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from freight_audit_lab.generate.rates import ship_week

EIA_DATE_FORMATS = ["%b %d, %Y", "%m/%d/%Y", "%Y-%m-%d", "%b-%d-%Y", "%d-%b-%Y", "%Y%m%d"]


def diesel_weeks(cfg):
    """Mondays from lead_weeks before the period start through the end of runoff.

    The lead weeks cover shipments in the first days of the period (their ship-week Monday
    can fall in the prior year) and the Stage 2 "wrong week" error needs later weeks too.
    """
    p = cfg["period"]
    start = pd.Timestamp(p["start"])
    period_end = start + pd.DateOffset(months=p["months"]) - pd.Timedelta(days=1)
    runoff_end = period_end + pd.Timedelta(days=p["runoff_days"])
    first = ship_week(start) - pd.Timedelta(weeks=cfg["diesel"]["lead_weeks"])
    return pd.date_range(first, ship_week(runoff_end), freq="7D")


def synthetic_prices(weeks, cfg, rng):
    """Mean-reverting random walk, clipped to [min, max], plus an optional price shock.

    Each week the price drifts a fraction `mean_reversion` of the way back toward
    `long_run_mean` and takes a normal step of size `weekly_sd`. The shock is added on top
    (not fed into the walk, so mean reversion doesn't cancel it): a linear rise of
    `total_rise` over `weeks_up` weeks, then a linear fall back over `weeks_down` weeks.
    """
    s = cfg["diesel"]["synthetic"]
    steps = rng.normal(0.0, s["weekly_sd"], size=len(weeks) - 1)
    walk = [s["start_price"]]
    for step in steps:
        prev = walk[-1]
        nxt = prev + s["mean_reversion"] * (s["long_run_mean"] - prev) + step
        walk.append(min(max(nxt, s["min"]), s["max"]))

    shock = np.zeros(len(weeks))
    sh = cfg["diesel"]["shock"]
    if sh["enabled"]:
        shock_start = ship_week(sh["start"])          # first Monday on or after the start date
        if shock_start < pd.Timestamp(sh["start"]):
            shock_start += pd.Timedelta(weeks=1)
        for i, week in enumerate(weeks):
            j = (week - shock_start).days // 7
            if 0 <= j <= sh["weeks_up"]:
                shock[i] = sh["total_rise"] * j / sh["weeks_up"]
            elif sh["weeks_up"] < j <= sh["weeks_up"] + sh["weeks_down"]:
                shock[i] = sh["total_rise"] * (1 - (j - sh["weeks_up"]) / sh["weeks_down"])

    prices = np.clip(np.array(walk) + shock, s["min"], s["max"])
    return np.round(prices, cfg["diesel"]["price_decimals"])


def _parse_date(text):
    """Parse a date cell in any format EIA downloads use; None if it isn't a date."""
    for fmt in EIA_DATE_FORMATS:
        try:
            return datetime.strptime(text.strip(), fmt)
        except ValueError:
            continue
    return None


def _parse_float(text):
    try:
        return float(text.strip())
    except ValueError:
        return None


def load_eia_csv(path):
    """Read an EIA weekly diesel CSV (series EMD_EPD2D_PTE_NUS_DPG) into week_start, price.

    EIA downloads start with title and source-key lines before the data. Rather than
    assume a fixed number of header lines, a row counts as data when its first cell is a
    date and its second a number; everything else is skipped. Dates are snapped to the
    Monday on or before, since EIA sometimes publishes on Tuesday after a holiday.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"EIA diesel CSV not found at {path}. Download series "
                                f"EMD_EPD2D_PTE_NUS_DPG from eia.gov or set diesel.source: synthetic.")
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for cells in csv.reader(f):
            if len(cells) < 2:
                continue
            date, price = _parse_date(cells[0]), _parse_float(cells[1])
            if date is not None and price is not None:
                rows.append({"week_start": ship_week(date), "price_per_gallon": price})
    if not rows:
        raise ValueError(f"{path}: found no rows with a date in column 1 and a price in column 2")
    df = pd.DataFrame(rows).drop_duplicates("week_start", keep="last")
    return df.sort_values("week_start").reset_index(drop=True)


def eia_prices(weeks, cfg):
    """EIA prices aligned to the required Mondays, failing if the file doesn't cover them.

    A week missing inside the covered range keeps the last published price, which is what
    a carrier's FSC table would still be using.
    """
    eia = load_eia_csv(cfg["diesel"]["eia_csv_path"])
    first, last = eia["week_start"].min(), eia["week_start"].max()
    if first > weeks[0] or last < weeks[-1]:
        raise ValueError(f"EIA diesel file covers {first.date()} to {last.date()} but the "
                         f"project needs {weeks[0].date()} to {weeks[-1].date()}.")
    series = eia.set_index("week_start")["price_per_gallon"]
    series = series.reindex(series.index.union(weeks)).ffill().reindex(weeks)
    return series.round(cfg["diesel"]["price_decimals"]).to_numpy()


def build_diesel(cfg, rng):
    """The weekly diesel table used for every FSC: week_start, price_per_gallon, source."""
    weeks = diesel_weeks(cfg)
    if cfg["diesel"]["source"] == "eia_csv":
        prices, source = eia_prices(weeks, cfg), "eia"
    else:
        prices, source = synthetic_prices(weeks, cfg, rng), "synthetic"
    return pd.DataFrame({"week_start": weeks, "price_per_gallon": prices, "source": source})
