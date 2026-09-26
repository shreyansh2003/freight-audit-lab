"""Exception queue and carrier dispute summaries: what an operations associate works from.

The audit says *what* looks wrong; this module turns that into a ranked work list (which invoices to
chase first) and one dispute pack per carrier (what to send them, and whether the same problem keeps
recurring). Everything is built from the audit's own outputs and a fixed text template: no answer key,
no LLM calls. Every dollar figure is an *estimate*.
"""

from math import exp, fsum, lgamma, log, log1p

import numpy as np
import pandas as pd

from freight_audit_lab.audit.rules import RULES, usd
from freight_audit_lab.csv_io import write_csv
from freight_audit_lab.audit.engine import OUTPUT_DIR

ERROR_TYPES = [rule.__name__ for rule in RULES]
QUEUE_COLUMNS = ["rank", "invoice_id", "carrier_id", "invoice_number", "pro_number", "bol", "ship_date",
                 "received_date", "error_type", "n_flags", "reason", "dollar_impact_estimate",
                 "recoverable_estimate", "days_open", "status"]
DISPUTE_COLUMNS = ["rank", "invoice_id", "invoice_number", "pro_number", "bol", "ship_date", "received_date",
                   "error_type", "reason", "recoverable_estimate"]
# what the associate asks the carrier for, by error type
ASK = {
    "duplicate_invoice": "Request credit memos for the repeated invoices.",
    "phantom_invoice": "Request proof of delivery before paying.",
    "rate_overcharge": "Request the rate table the carrier is billing from and corrected invoices.",
    "fsc_mismatch": "Request the carrier's fuel surcharge table and corrected invoices.",
    "unauthorized_accessorial": "Request the accessorial authorization or dispatch record.",
    "weight_overbilling": "Request the weight and inspection documents.",
}
# how a flag is described in a sentence about a pattern
PATTERN_WORDS = {
    "duplicate_invoice": "duplicate invoices",
    "phantom_invoice": "invoices with no matching shipment",
    "rate_overcharge": "linehaul billed above contract",
    "fsc_mismatch": "fuel surcharge billed above schedule",
    "unauthorized_accessorial": "accessorials billed with no authorization",
    "weight_overbilling": "billed weight above the reference weight",
}
OVERBILL_TYPES = ["rate_overcharge", "fsc_mismatch", "weight_overbilling"]   # billed_value / expected_value are amounts


# ---------------------------------------------------------------- exception queue


def build_exception_queue(norm, flags, summary, cfg):
    """One row per flagged invoice, ranked by recoverable estimate then age.

    An invoice is the unit of work (one call or email to the carrier), so an invoice with several flags
    is one row: `error_type` lists every flagged type in rule order, `reason` joins the reasons with
    " | ", and `dollar_impact_estimate` is the sum of all its flags. `recoverable_estimate` is the
    de-duplicated figure from the engine (a duplicate that also has a rate flag counts once, at its
    total), so the two can differ. `days_open` is days from the received date to `audit_as_of`.
    """
    as_of = pd.Timestamp(cfg["period"]["audit_as_of"])
    rule_order = {t: i for i, t in enumerate(ERROR_TYPES)}
    flags = flags.assign(_rule=flags["error_type"].map(rule_order)).sort_values(["invoice_id", "_rule"])
    per_invoice = flags.groupby("invoice_id").agg(
        error_type=("error_type", ";".join), n_flags=("error_type", "size"),
        reason=("reason", " | ".join), dollar_impact_estimate=("dollar_impact_estimate", "sum")).reset_index()
    inv = norm["invoices"][["invoice_id", "carrier_id", "invoice_number", "pro_number", "bol_raw", "ship_date",
                            "received_date"]].rename(columns={"bol_raw": "bol"})
    queue = per_invoice.merge(inv, on="invoice_id", how="left").merge(
        summary[["invoice_id", "recoverable_estimate"]], on="invoice_id", how="left")
    queue["dollar_impact_estimate"] = queue["dollar_impact_estimate"].round(2)
    queue["days_open"] = (as_of - queue["received_date"]).dt.days
    queue["status"] = "open"
    queue = queue.sort_values(["recoverable_estimate", "received_date", "invoice_id"],
                              ascending=[False, True, True]).reset_index(drop=True)
    queue["rank"] = queue.index + 1
    return queue[QUEUE_COLUMNS]


# ---------------------------------------------------------------- systemic pattern check


def month_windows(months, width):
    """Rolling windows of `width` consecutive months across a sorted list of month Periods."""
    return [(months[i], months[i + width - 1]) for i in range(len(months) - width + 1)]


def binomial_sf(k, n, p):
    """P(X >= k) for X ~ Binomial(n, p): the chance of seeing at least k flags in n invoices if the
    carrier were flagged at the peer rate p. Summed in log space so large n never overflows."""
    if k <= 0 or p >= 1:
        return 1.0
    if p <= 0:
        return 0.0
    logs = [lgamma(n + 1) - lgamma(i + 1) - lgamma(n - i + 1) + i * log(p) + (n - i) * log1p(-p)
            for i in range(k, n + 1)]
    top = max(logs)
    return min(1.0, exp(top) * fsum(exp(x - top) for x in logs))


FINDING_COLUMNS = ["carrier_id", "error_type", "window_start", "window_end", "invoices", "flagged", "carrier_rate",
                   "other_carriers_rate", "mode", "p_value", "alpha_adjusted", "avg_billed_over_expected"]


def systemic_findings(norm, flags, cfg):
    """Carrier/error-type pairs whose flag rate in some rolling window is far above their peers'.

    A carrier having one bad month is noise; a carrier billing the same wrong way for a quarter is a
    process problem (a stale fuel table, a mis-loaded rate card) worth one escalation instead of many
    invoice disputes. For every rolling window of `window_months` ship months, the carrier's flag count
    for an error type (out of the invoices shipped in the window) is compared with the flag rate of the
    *other carriers of the same mode* (leaving the carrier out, so its own bad stretch cannot inflate its
    yardstick; same mode because weight checks exist only for LTL, fuel is priced differently for TL, and
    accessorials differ).

    A finding needs all of: at least `min_invoices` invoices and `min_flags` flags (no tiny samples); a
    rate at least `multiple` times the peer rate (a big enough effect to matter); and a one-sided binomial
    p-value at or below `alpha` divided by the number of tests run (Bonferroni: every carrier x error
    type x window is a test, and with hundreds of them some look bad by chance). Per carrier and error
    type the most significant window is kept.
    """
    sysc = cfg["evaluation"]["systemic"]
    inv = norm["invoices"]
    inv = inv[~inv["is_superseded"]][["invoice_id", "carrier_id", "ship_date"]].copy()
    inv["mode"] = inv["carrier_id"].map({c["id"]: c["mode"] for c in cfg["carriers"]})
    inv["month"] = inv["ship_date"].dt.to_period("M")
    windows = month_windows(sorted(inv["month"].unique()), sysc["window_months"])
    n_tests = inv["carrier_id"].nunique() * len(ERROR_TYPES) * len(windows)
    alpha_adjusted = sysc["alpha"] / n_tests if n_tests else sysc["alpha"]
    rows = []
    for error_type in ERROR_TYPES:
        hit = flags.loc[flags["error_type"] == error_type]
        inv["_flagged"] = inv["invoice_id"].isin(hit["invoice_id"])
        for start, end in windows:
            win = inv[(inv["month"] >= start) & (inv["month"] <= end)]
            for carrier, grp in win.groupby("carrier_id"):
                n, k = len(grp), int(grp["_flagged"].sum())
                if n < sysc["min_invoices"] or k < sysc["min_flags"]:
                    continue
                others = win[(win["mode"] == grp["mode"].iloc[0]) & (win["carrier_id"] != carrier)]
                other_rate = others["_flagged"].mean() if len(others) else 0.0
                p_value = binomial_sf(k, n, other_rate)
                if k / n >= sysc["multiple"] * other_rate and p_value <= alpha_adjusted:
                    over = hit[hit["invoice_id"].isin(grp.loc[grp["_flagged"], "invoice_id"])]
                    avg_over = ((over["billed_value"] / over["expected_value"] - 1).mean()
                                if error_type in OVERBILL_TYPES else np.nan)
                    rows.append({"carrier_id": carrier, "error_type": error_type, "window_start": start.start_time,
                                 "window_end": end.end_time.normalize(), "invoices": n, "flagged": k,
                                 "carrier_rate": k / n, "other_carriers_rate": other_rate, "mode": grp["mode"].iloc[0],
                                 "p_value": p_value, "alpha_adjusted": alpha_adjusted, "avg_billed_over_expected": avg_over})
    if not rows:
        return pd.DataFrame(columns=FINDING_COLUMNS)
    found = pd.DataFrame(rows).sort_values(["p_value", "window_start"])
    return found.drop_duplicates(["carrier_id", "error_type"]).sort_values(["carrier_id", "error_type"]).reset_index(drop=True)[FINDING_COLUMNS]


def systemic_line(f):
    """The template sentence for one systemic finding, with the significance test's p-value."""
    same_year = f["window_start"].year == f["window_end"].year
    first, last = f["window_start"].strftime("%b"), f["window_end"].strftime("%b %Y")
    span = f"{first}-{last}" if same_year else f"{f['window_start'].strftime('%b %Y')}-{last}"
    over = ("" if np.isnan(f["avg_billed_over_expected"])
            else f" (on average {f['avg_billed_over_expected']:.1%} above expected)")
    p = "< 1e-300" if f["p_value"] < 1e-300 else f"= {f['p_value']:.1e}"
    return (f"Possible systemic issue: {PATTERN_WORDS[f['error_type']]} on {f['carrier_rate']:.0%} of invoices shipped "
            f"{span} ({f['flagged']} of {f['invoices']}){over} vs {f['other_carriers_rate']:.0%} across other {f['mode']} "
            f"carriers (one-sided binomial p {p}, below the Bonferroni-adjusted threshold {f['alpha_adjusted']:.1e}). "
            f"{ASK[f['error_type']]}")


# ---------------------------------------------------------------- dispute summaries


def carrier_by_type(flags):
    """Flagged invoices and estimate dollars by error type. `recoverable_estimate` counts each dollar once."""
    counted = flags.assign(_c=flags["dollar_impact_estimate"].where(flags["counted_in_recoverable"], 0.0))
    table = counted.groupby("error_type").agg(
        flagged_invoices=("invoice_id", "nunique"), flagged_dollars_estimate=("dollar_impact_estimate", "sum"),
        recoverable_estimate=("_c", "sum")).round(2)
    return table.reindex([t for t in ERROR_TYPES if t in table.index]).reset_index()


def dispute_markdown(carrier, name, norm, flags, mine, findings, cfg):
    """The dispute summary for one carrier, as markdown text. `mine` is the carrier's queue rows,
    ranked 1..n within the carrier."""
    top_n = cfg["evaluation"]["top_n_invoices"]
    inv = norm["invoices"]
    audited = inv[(inv["carrier_id"] == carrier) & ~inv["is_superseded"]]
    my_flags = flags[flags["invoice_id"].isin(mine["invoice_id"])]
    table = carrier_by_type(my_flags)
    period = (f"shipments {audited['ship_date'].min():%Y-%m-%d} to {audited['ship_date'].max():%Y-%m-%d}"
              if len(audited) else "no invoices audited")
    lines = [f"# Dispute summary: {name} ({carrier})", "",
             f"Period: {period}; audit as of {cfg['period']['audit_as_of']}.",
             f"Audited {len(audited):,} invoices; {len(mine):,} flagged. "
             f"Total recoverable estimate: {usd(mine['recoverable_estimate'].sum())}. "
             "All dollar figures are estimates.", ""]
    if mine.empty:
        return "\n".join(lines + ["No exceptions found for this carrier."]) + "\n"
    lines += ["## By error type", "", "An invoice with several flags is counted under each type.", "",
              "| Error type | Flagged invoices | Flagged $ (estimate) | Recoverable $ (estimate) |", "|---|---:|---:|---:|"]
    lines += [f"| {r.error_type} | {r.flagged_invoices:,} | {usd(r.flagged_dollars_estimate)} | {usd(r.recoverable_estimate)} |"
              for r in table.itertuples()]
    lines += ["", f"## Top {min(top_n, len(mine))} invoices by recoverable estimate", ""]
    for r in mine.head(top_n).itertuples():
        lines.append(f"{r.rank}. **{r.invoice_number}** (PRO {r.pro_number}, BOL {r.bol}, shipped {r.ship_date:%Y-%m-%d}, "
                     f"received {r.received_date:%Y-%m-%d}): recoverable estimate {usd(r.recoverable_estimate)}. "
                     f"{r.error_type}: {r.reason}")
    lines += ["", "## Systemic pattern check", ""]
    mine_findings = findings[findings["carrier_id"] == carrier]
    lines += ([f"- {systemic_line(f)}" for _, f in mine_findings.iterrows()] if len(mine_findings)
              else ["No error type stands out from the other carriers' rate in any rolling "
                    f"{cfg['evaluation']['systemic']['window_months']}-month window."])
    return "\n".join(lines) + "\n"


def build_disputes(norm, flags, queue, cfg):
    """{carrier_id: (markdown text, DataFrame of that carrier's flagged invoices)} for every carrier."""
    findings = systemic_findings(norm, flags, cfg)
    out = {}
    for c in cfg["carriers"]:
        mine = queue[queue["carrier_id"] == c["id"]]
        mine = mine.assign(rank=np.arange(1, len(mine) + 1))
        out[c["id"]] = (dispute_markdown(c["id"], c["name"], norm, flags, mine, findings, cfg), mine[DISPUTE_COLUMNS])
    return out, findings


def write_exceptions(queue, disputes, findings, out_dir=OUTPUT_DIR):
    """Write outputs/exception_queue.csv, systemic_findings.csv, and disputes/<carrier>.md and .csv."""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "disputes").mkdir(exist_ok=True)
    write_csv(queue, out_dir / "exception_queue.csv")
    write_csv(findings.round({"carrier_rate": 4, "other_carriers_rate": 4, "avg_billed_over_expected": 4}).assign(
        p_value=findings["p_value"].map("{:.3e}".format), alpha_adjusted=findings["alpha_adjusted"].map("{:.3e}".format)),
              out_dir / "systemic_findings.csv")
    for carrier, (text, table) in disputes.items():
        (out_dir / "disputes" / f"{carrier}.md").write_text(text)
        write_csv(table, out_dir / "disputes" / f"{carrier}.csv")
