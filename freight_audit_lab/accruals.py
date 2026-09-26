"""Month-end freight accruals: book what the shipper owes for freight the carriers have not billed yet.

The problem. Freight expense belongs to the month the shipment is delivered, but carriers invoice
10 to 20 days later (or more). At month-end M the books would be short by every delivered-but-unbilled
shipment, so finance accrues an *estimate*: Dr Freight Expense, Cr Accrued Freight. On day 1 of M+1
the entry is reversed in full; when the real invoices are then posted to AP they carry the actual cost,
so the reversal plus the invoice posting is the true-up. (Posting invoices is out of scope here.)

No look-ahead (CLAUDE.md rule 3). An accrual at M may use only what was known at M:
  - invoices *received* on or before M (a later invoice changes neither the population nor the allowance);
  - authorizations recorded on or before M;
  - supersession known at M: an invoice counts as replaced only if its replacing rebill was received on or
    before M (`superseded_on`). An invoice received by M bills its shipment even if a rebill received after
    M replaces it, because the shipper had the bill in hand at M;
  - diesel published on or before M (the ship-week price, published by the week start) and the rate row
    effective on the ship date.
The *accuracy* step is the one place that looks forward on purpose: it compares each accrual with what the
shipment eventually cost.

Nothing here reads the answer key; the "actual payable" side uses the audit's own recoverable estimate.
Every dollar figure is an estimate.
"""

import numpy as np
import pandas as pd

from freight_audit_lab.audit.engine import OUTPUT_DIR, WHOLE_INVOICE_TYPES
from freight_audit_lab.contract import (cents, contract_linehaul, diesel_for_ship_date, fsc_ltl_amount,
                                        fsc_tl_amount, lookup_rate)
from freight_audit_lab.csv_io import write_csv
from freight_audit_lab.rerate import ACCESSORIAL_CODES, RATED_TYPES, attach_authorization

DETAIL_COLUMNS = ["month_end", "shipment_id", "carrier_id", "cost_center", "mode", "ship_date", "delivery_date",
                  "lh_estimate", "fsc_estimate", "accessorial_estimate", "accessorial_basis", "accrual_estimate"]
ACTUAL_COLUMNS = ["invoices_billed", "actual_billed", "actual_lh_fsc_billed", "actual_accessorial_billed",
                  "actual_recoverable_estimate", "actual_payable_estimate", "actual_payable_lh_fsc_estimate",
                  "actual_payable_accessorial_estimate"]
JE_COLUMNS = ["je_id", "date", "type", "account", "cost_center", "debit", "credit", "memo"]
MEMO_PREFIX = "ESTIMATE – synthetic data"


def month_ends(cfg):
    """The last day of each of the `period.months` months from `period.start`."""
    starts = pd.date_range(pd.Timestamp(cfg["period"]["start"]), periods=cfg["period"]["months"], freq="MS")
    return list(starts + pd.offsets.MonthEnd(0))


# ---------------------------------------------------------------- the estimate


def price_shipments(shipments, ref, cfg):
    """Contract linehaul and expected fuel surcharge for every shipment, from the rate row in force on
    the ship date and the ship-week diesel price. Neither depends on the month-end, so this runs once.
    Rounded to cents per charge line."""
    rows = []
    for s in shipments.itertuples(index=False):
        rate = lookup_rate(ref["rate_card"], s.carrier_id, s.lane_id, s.ship_date)
        lh = cents(contract_linehaul(rate, s.weight_lbs, s.miles))
        diesel = diesel_for_ship_date(ref["diesel_weekly"], s.ship_date)
        fsc = cents(fsc_ltl_amount(lh, diesel, cfg) if s.mode == "LTL" else fsc_tl_amount(s.miles, diesel, cfg))
        rows.append((s.shipment_id, lh, fsc))
    priced = pd.DataFrame(rows, columns=["shipment_id", "lh_estimate", "fsc_estimate"])
    keep = ["shipment_id", "carrier_id", "cost_center", "mode", "ship_date", "delivery_date"]
    return shipments[keep].merge(priced, on="shipment_id")


def billed_shipments(invoices, month_end):
    """Shipments already billed at `month_end`: a matched original or rebill received on or before it.

    Superseded or not makes no difference: an invoice in hand at M bills its shipment, and a rebill
    received later would only replace it. A balance-due invoice bills one extra charge, not the shipment.
    """
    hit = invoices[invoices["invoice_type"].isin(RATED_TYPES) & invoices["shipment_id"].notna()
                   & (invoices["received_date"] <= month_end)]
    return set(hit["shipment_id"])


def accessorial_allowance(norm, authorizations, cfg, month_end):
    """Each carrier's average authorized accessorial dollars per billed shipment, as known at `month_end`.

    Carriers add liftgate, residential and detention charges the contract rate does not cover, and the
    shipper cannot see them until the invoice arrives, so the accrual adds an allowance: over invoices
    received in the trailing `trailing_days` up to M (and not already known at M to be superseded), the
    accessorial charge lines that had an authorization recorded by M, divided by the number of distinct
    shipments those carriers billed. A carrier with no invoices in the window gets the config default for
    its mode. Returns a DataFrame [carrier_id, accessorial_allowance, accessorial_basis].
    """
    acfg = cfg["accruals"]
    window = pd.Timedelta(days=acfg["trailing_days"])
    inv = norm["invoices"]
    live = inv[inv["shipment_id"].notna() & (inv["received_date"] <= month_end)
               & (inv["received_date"] > month_end - window) & ~(inv["superseded_on"] <= month_end)]
    shipments = live[live["invoice_type"].isin(RATED_TYPES)].groupby("carrier_id")["shipment_id"].nunique()
    lines = norm["invoice_lines"]
    acc = lines[lines["charge_code"].isin(ACCESSORIAL_CODES)].merge(
        live[["invoice_id", "carrier_id", "shipment_id"]], on="invoice_id")
    acc = acc.rename(columns={"charge_code": "charge_code"})
    if len(acc):
        acc = attach_authorization(acc, authorizations, pd.Series([month_end] * len(acc), dtype="datetime64[ns]"))
        authorized = acc[acc["authorized_at"].notna()].groupby("carrier_id")["amount"].sum()
    else:
        authorized = pd.Series(dtype=float)
    rows = []
    for c in cfg["carriers"]:
        n = int(shipments.get(c["id"], 0))
        if n:
            rows.append((c["id"], float(authorized.get(c["id"], 0.0)) / n, "history"))
        else:
            rows.append((c["id"], float(acfg["default_accessorial_per_shipment"][c["mode"]]), "default"))
    return pd.DataFrame(rows, columns=["carrier_id", "accessorial_allowance", "accessorial_basis"])


def accrue_at(month_end, priced, norm, ref, cfg):
    """The accrual detail at one month-end: one row per delivered-but-unbilled shipment.

    Population: delivered on or before M (expense is recognized at delivery, so in-transit shipments are
    not accrued) and not billed at M. Estimate = contract linehaul + expected fuel surcharge + the
    carrier's accessorial allowance, each rounded to cents.
    """
    month_end = pd.Timestamp(month_end)
    billed = billed_shipments(norm["invoices"], month_end)
    pop = priced[(priced["delivery_date"] <= month_end) & ~priced["shipment_id"].isin(billed)].copy()
    allowance = accessorial_allowance(norm, ref["authorizations"], cfg, month_end)
    pop = pop.merge(allowance, on="carrier_id", how="left")
    pop["accessorial_estimate"] = pop.pop("accessorial_allowance").map(cents)
    pop["accrual_estimate"] = (pop["lh_estimate"] + pop["fsc_estimate"] + pop["accessorial_estimate"]).round(2)
    pop.insert(0, "month_end", month_end)
    return pop[DETAIL_COLUMNS].reset_index(drop=True)


# ---------------------------------------------------------------- what it eventually cost


def shipment_actuals(norm, engine):
    """Per shipment: what it eventually cost, from the final (non-superseded) matched invoices.

    `actual_billed` is what the carrier billed, balance-due invoices included. `actual_payable_estimate`
    is billed less the audit's recoverable estimate, since the shipper owes the right amount and not the
    billed amount. Both are split into linehaul + fuel and accessorials. A recoverable dollar is assigned
    to accessorials if it comes from an unauthorized-accessorial flag, to linehaul + fuel if it comes from
    a rate, fuel or weight flag, and split by the invoice's own lines when the whole invoice is recoverable
    (duplicate or phantom). Shipments never billed have no row here.
    """
    inv = norm["invoices"]
    live = inv[~inv["is_superseded"] & inv["shipment_id"].notna()][["invoice_id", "shipment_id", "total"]].copy()
    lines = norm["invoice_lines"]
    core = lines[lines["charge_code"].isin(["LH", "FSC"])].groupby("invoice_id")["amount"].sum()
    live["billed_core"] = live["invoice_id"].map(core).fillna(0.0)
    live["billed_acc"] = (live["total"] - live["billed_core"]).round(2)
    counted = engine["flags"][engine["flags"]["counted_in_recoverable"]]
    whole = set(counted.loc[counted["error_type"].isin(WHOLE_INVOICE_TYPES), "invoice_id"])
    unauth = (counted[counted["error_type"] == "unauthorized_accessorial"]
              .groupby("invoice_id")["dollar_impact_estimate"].sum())
    rec = live["invoice_id"].map(engine["invoice_summary"].set_index("invoice_id")["recoverable_estimate"]).fillna(0.0)
    acc_rec = np.minimum(np.minimum(live["invoice_id"].map(unauth).fillna(0.0), live["billed_acc"]), rec)
    is_whole = live["invoice_id"].isin(whole)
    live["rec_acc"] = np.where(is_whole, live["billed_acc"], acc_rec).round(2)
    live["rec_core"] = np.where(is_whole, live["billed_core"], rec - acc_rec).round(2)
    live["actual_billed"] = live["total"]
    live["actual_payable_estimate"] = (live["total"] - live["rec_core"] - live["rec_acc"]).round(2)
    grouped = live.groupby("shipment_id").agg(
        invoices_billed=("invoice_id", "size"), actual_billed=("total", "sum"),
        actual_lh_fsc_billed=("billed_core", "sum"), actual_accessorial_billed=("billed_acc", "sum"),
        rec_core=("rec_core", "sum"), rec_acc=("rec_acc", "sum"),
        actual_payable_estimate=("actual_payable_estimate", "sum"))
    grouped["actual_recoverable_estimate"] = grouped["rec_core"] + grouped["rec_acc"]
    grouped["actual_payable_lh_fsc_estimate"] = grouped["actual_lh_fsc_billed"] - grouped["rec_core"]
    grouped["actual_payable_accessorial_estimate"] = grouped["actual_accessorial_billed"] - grouped["rec_acc"]
    return grouped[ACTUAL_COLUMNS].round(2).reset_index()


# ---------------------------------------------------------------- accuracy


def accuracy_by_month(detail, cfg):
    """Accrual vs eventual payable by month-end, in total and split into linehaul + fuel vs accessorials,
    plus an ALL row. Error = accrual - payable (positive = over-accrued); % is of payable. A shipment
    the carrier never billed has payable 0 and is counted in `never_billed_shipments`.

    The monthly error is a *net* figure: over- and under-accrued shipments cancel inside a month. The
    `shipment_*` columns measure the error one shipment-month at a time, without that netting: the mean and
    median of |accrual - payable| / payable, and the share of shipment-months off by more than
    `accruals.large_shipment_error_pct`. Shipments with no payable (never billed) have no percentage and are left out."""
    band = cfg["accruals"]["large_shipment_error_pct"]
    d = detail.assign(
        payable=detail["actual_payable_estimate"].fillna(0.0), billed=detail["actual_billed"].fillna(0.0),
        pay_core=detail["actual_payable_lh_fsc_estimate"].fillna(0.0),
        pay_acc=detail["actual_payable_accessorial_estimate"].fillna(0.0),
        acc_core=detail["lh_estimate"] + detail["fsc_estimate"],
        never=detail["invoices_billed"].isna())

    def summarize(g):
        out = {"shipments_accrued": len(g), "never_billed_shipments": int(g["never"].sum()),
               "accrual_estimate": g["accrual_estimate"].sum(), "actual_billed": g["billed"].sum(),
               "actual_payable_estimate": g["payable"].sum()}
        out["error_estimate"] = out["accrual_estimate"] - out["actual_payable_estimate"]
        out["error_pct_estimate"] = out["error_estimate"] / out["actual_payable_estimate"]
        for name, est, pay in (("lh_fsc", g["acc_core"], g["pay_core"]),
                               ("accessorial", g["accessorial_estimate"], g["pay_acc"])):
            out[f"{name}_accrual_estimate"], out[f"{name}_payable_estimate"] = est.sum(), pay.sum()
            out[f"{name}_error_estimate"] = est.sum() - pay.sum()
            out[f"{name}_error_pct_estimate"] = (est.sum() - pay.sum()) / pay.sum() if pay.sum() else np.nan
        billed = g[g["payable"] > 0]
        ape = (billed["accrual_estimate"] - billed["payable"]).abs() / billed["payable"]
        out["shipment_mape_pct_estimate"] = ape.mean()
        out["shipment_median_ape_pct_estimate"] = ape.median()
        out["shipment_share_over_band_pct_estimate"] = (ape > band).mean()
        return pd.Series(out)

    monthly = d.groupby("month_end").apply(summarize, include_groups=False).reset_index()
    total = summarize(d).to_frame().T.assign(month_end="ALL")
    return pd.concat([monthly, total], ignore_index=True)


def accuracy_summary(accuracy):
    """Headline accuracy: MAPE (mean absolute monthly error % of payable) and bias (mean signed monthly error %)."""
    monthly = accuracy[accuracy["month_end"] != "ALL"]
    return {"mape_pct_estimate": float(monthly["error_pct_estimate"].abs().mean()),
            "bias_pct_estimate": float(monthly["error_pct_estimate"].mean())}


# ---------------------------------------------------------------- sensitivity: late authorizations

SENSITIVITY_METRICS = ["accrual_estimate", "error_estimate", "error_pct_estimate",
                       "accessorial_accrual_estimate", "accessorial_error_estimate", "accessorial_error_pct_estimate"]


def authorizations_at_delivery(ref):
    """The what-if books: every authorization recorded on the day the shipment was delivered.

    Some accessorial paperwork is recorded weeks after the carrier's invoice (`late_authorization_share`).
    At month-end that paperwork is not on file yet, so those real charges drop out of the accessorial
    history and the allowance runs low. Setting every `authorized_at` to the delivery date shows how much
    of the accrual error that lateness explains. Which shipment-and-code pairs are authorized does not
    change, only when.
    """
    delivered = ref["shipments"].set_index("shipment_id")["delivery_date"]
    auths = ref["authorizations"].copy()
    auths["authorized_at"] = auths["shipment_id"].map(delivered)
    return auths


def accrual_sensitivity(built_accuracy, scenario_accuracy):
    """Monthly accrual error as built next to the same error with every authorization recorded at delivery.

    Both runs use the same no-look-ahead rules and the same shipments, so `actual_payable_estimate` is
    identical: only the accrual (through the accessorial allowance) moves. `late_auth_effect_estimate` is
    the scenario error minus the built error in dollars (positive = late paperwork made the accrual lower).
    """
    keep = ["month_end", "shipments_accrued", "actual_payable_estimate"]
    out = built_accuracy[keep].copy()
    for metric in SENSITIVITY_METRICS:
        out[f"{metric}_as_built"] = built_accuracy[metric].to_numpy()
        out[f"{metric}_auth_at_delivery"] = scenario_accuracy[metric].to_numpy()
    out["late_auth_effect_estimate"] = out["accrual_estimate_auth_at_delivery"] - out["accrual_estimate_as_built"]
    return out


# ---------------------------------------------------------------- journal entries


def journal_entries(detail, cfg):
    """Accrual and reversal entries. At each month-end M: Dr Freight Expense (one line per cost center),
    Cr Accrued Freight (the total). On day 1 of M+1 the exact mirror image. Built in whole cents so every
    entry balances to the cent."""
    accounts = cfg["accruals"]["accounts"]
    rows = []
    for month_end, grp in detail.groupby("month_end"):
        by_cc = (grp.groupby("cost_center")["accrual_estimate"].sum() * 100).round().astype(int).sort_index()
        total = int(by_cc.sum())
        accrual_id, reversal_id = f"ACR-{month_end:%Y%m}", f"REV-{month_end:%Y%m}"
        memo = f"{MEMO_PREFIX}: accrual of freight delivered through {month_end:%Y-%m-%d} and not yet invoiced"
        reverse_memo = f"{MEMO_PREFIX}: reversal of {accrual_id}"
        reverse_date = month_end + pd.Timedelta(days=1)
        for cc, amount in by_cc.items():
            rows.append((accrual_id, month_end, "accrual", accounts["expense"], cc, amount / 100, 0.0, memo))
            rows.append((reversal_id, reverse_date, "reversal", accounts["expense"], cc, 0.0, amount / 100, reverse_memo))
        rows.append((accrual_id, month_end, "accrual", accounts["liability"], "", 0.0, total / 100, memo))
        rows.append((reversal_id, reverse_date, "reversal", accounts["liability"], "", total / 100, 0.0, reverse_memo))
    je = pd.DataFrame(rows, columns=JE_COLUMNS)
    order = je["account"].map({accounts["expense"]: 0, accounts["liability"]: 1})
    return (je.assign(_o=order).sort_values(["date", "je_id", "_o", "cost_center"], kind="stable")
            .drop(columns="_o").reset_index(drop=True))


# ---------------------------------------------------------------- the run


def accrue_all_months(priced, norm, ref, cfg, actuals):
    """The accrual detail at every month-end, each shipment joined to what it eventually cost."""
    detail = pd.concat([accrue_at(m, priced, norm, ref, cfg) for m in month_ends(cfg)], ignore_index=True)
    detail = detail.merge(actuals, on="shipment_id", how="left")
    detail["error_estimate"] = (detail["accrual_estimate"] - detail["actual_payable_estimate"].fillna(0.0)).round(2)
    return detail


def run_accruals(norm, ref, cfg, engine):
    """Accrue at every month-end. Returns {"detail", "accuracy", "sensitivity", "journal_entries"}.

    The sensitivity reruns the same accruals on books where every authorization was recorded at delivery
    (`authorizations_at_delivery`); the "actual" side is the same in both runs."""
    priced = price_shipments(ref["shipments"], ref, cfg)
    actuals = shipment_actuals(norm, engine)
    detail = accrue_all_months(priced, norm, ref, cfg, actuals)
    accuracy = accuracy_by_month(detail, cfg)
    what_if = accrue_all_months(priced, norm, dict(ref, authorizations=authorizations_at_delivery(ref)), cfg, actuals)
    return {"detail": detail, "accuracy": accuracy,
            "sensitivity": accrual_sensitivity(accuracy, accuracy_by_month(what_if, cfg)),
            "journal_entries": journal_entries(detail, cfg)}


def write_accruals(result, out_dir=OUTPUT_DIR):
    """Write outputs/accruals_detail.csv, accrual_accuracy.csv, accrual_sensitivity.csv, journal_entries.csv."""
    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(result["detail"], out_dir / "accruals_detail.csv")
    acc = result["accuracy"].copy()
    acc["month_end"] = acc["month_end"].map(lambda m: m if isinstance(m, str) else m.strftime("%Y-%m-%d"))
    counts = ["shipments_accrued", "never_billed_shipments"]
    numeric = [c for c in acc.columns if c != "month_end"]
    acc = acc.astype({c: float for c in numeric}).round({c: 4 if c.endswith("_pct_estimate") else 2 for c in numeric})
    write_csv(acc.astype({c: int for c in counts}), out_dir / "accrual_accuracy.csv")
    sens = result["sensitivity"].copy()
    sens["month_end"] = sens["month_end"].map(lambda m: m if isinstance(m, str) else m.strftime("%Y-%m-%d"))
    numeric = [c for c in sens.columns if c != "month_end"]
    sens = sens.astype({c: float for c in numeric}).round({c: 4 if "_pct_" in c else 2 for c in numeric})
    write_csv(sens.astype({"shipments_accrued": int}), out_dir / "accrual_sensitivity.csv")
    write_csv(result["journal_entries"], out_dir / "journal_entries.csv")
