"""What the dashboard shows, worked out from the files in `outputs/`.

The dashboard adds no numbers of its own. Every figure on the Overview tab is a sum, a ratio or a
lookup over an output file, and every sentence in "Key findings" is a template filled from those
files, so re-running the pipeline changes the page and nothing here has to be edited. Nothing in
this module reads the answer key: precision, recall and false positives come from `eval_*.csv`,
which `evaluate.py` wrote.
"""

import numpy as np
import pandas as pd

from freight_audit_lab.audit.engine import OUTPUT_DIR
from freight_audit_lab.csv_io import read_csv

OUTPUT_FILES = ["audit_invoice_summary", "eval_by_type", "eval_engine_vs_baseline", "eval_traps", "exception_queue",
                "sweep", "systemic_findings", "accrual_accuracy", "accrual_sensitivity", "journal_entries"]

ERROR_LABELS = {"duplicate_invoice": "Duplicate invoice", "phantom_invoice": "Phantom invoice",
                "rate_overcharge": "Rate overcharge", "fsc_mismatch": "Fuel surcharge mismatch",
                "unauthorized_accessorial": "Unauthorized accessorial", "weight_overbilling": "Weight overbilling"}

# Why the naive baseline trips on each trap. These describe the baseline's shortcuts (ASSUMPTIONS.md, Stage 4)
# and carry no numbers; a trap missing here just gets no explanation in the finding.
TRAP_WHY = {"rate_amendment": "it prices every shipment at the first rate row and ignores effective dates",
            "balance_due": "it treats a balance-due invoice as a copy of the original",
            "rebill": "it audits the superseded original as well as its rebill",
            "documented_reweigh": "it ignores reweigh certificates",
            "late_authorization": "it asks whether an accessorial was authorized as of the invoice date"}


def outputs_ready(out_dir=OUTPUT_DIR):
    """True if every output file the dashboard reads exists."""
    return all((out_dir / f"{name}.csv").exists() for name in OUTPUT_FILES)


def load_outputs(out_dir=OUTPUT_DIR):
    """Every output CSV the dashboard reads, as {file stem: DataFrame}."""
    return {name: read_csv(out_dir / f"{name}.csv") for name in OUTPUT_FILES}


# ---------------------------------------------------------------- formatting


def usd(x, cents=False):
    """Dollar formatting: $12,033,605 (or $1,234.57 with cents=True)."""
    return f"${x:,.2f}" if cents else f"${x:,.0f}"


def pct(x, digits=1):
    """Percent formatting from a fraction: 0.9991 -> 99.9%."""
    return f"{x:.{digits}%}"


def md_escape(text):
    """Escape `$` so Streamlit does not read a pair of dollar amounts as a LaTeX formula."""
    return text.replace("$", r"\$")


def error_label(error_type):
    """Display name for an error type, e.g. fsc_mismatch -> Fuel surcharge mismatch."""
    return ERROR_LABELS.get(error_type, error_type.replace("_", " "))


# ---------------------------------------------------------------- overview


def overview_metrics(o):
    """The four headline tiles: audit scale, precision, false disputes avoided, recoverable estimate.

    Spend is what the audited (non-superseded) invoices billed, duplicates and balance-due invoices
    included. False disputes avoided is the baseline's false positives minus the engine's, counted at the
    (invoice, error type) level like every other evaluation number.
    """
    summary = o["audit_invoice_summary"]
    overall = o["eval_engine_vs_baseline"].set_index("error_type").loc["ALL"]
    spend, recoverable = float(summary["total"].sum()), float(summary["recoverable_estimate"].sum())
    return {"invoices_audited": len(summary), "billed_spend": spend,
            "invoices_flagged": int((summary["n_flags"] > 0).sum()),
            "recoverable_estimate": recoverable, "recoverable_pct_of_spend_estimate": recoverable / spend,
            "engine_precision": float(overall["engine_precision"]), "baseline_precision": float(overall["baseline_precision"]),
            "engine_recall": float(overall["engine_recall"]), "baseline_recall": float(overall["baseline_recall"]),
            "engine_false_positives": int(overall["engine_fp"]), "baseline_false_positives": int(overall["baseline_fp"]),
            "false_disputes_avoided": int(overall["baseline_fp"] - overall["engine_fp"])}


def recoverable_by_carrier(o, carrier_names):
    """Recoverable estimate per carrier, largest first. `carrier_names` maps carrier_id to a display name."""
    by = o["audit_invoice_summary"].groupby("carrier_id")["recoverable_estimate"].sum()
    out = by.rename("recoverable_estimate").reset_index()
    out["carrier"] = out["carrier_id"].map(carrier_names).fillna(out["carrier_id"])
    return out.sort_values("recoverable_estimate", ascending=False).reset_index(drop=True)


def recoverable_by_error_type(o):
    """Recoverable estimate per error type from the engine's counted flags, largest first."""
    ev = o["eval_by_type"]
    out = ev[(ev["system"] == "engine") & (ev["error_type"] != "ALL")][["error_type", "flagged_dollars_estimate"]]
    out = out.rename(columns={"flagged_dollars_estimate": "recoverable_estimate"})
    out["label"] = out["error_type"].map(error_label)
    return out.sort_values("recoverable_estimate", ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------- key findings


def systemic_finding_text(o, carrier_names):
    """Bullet 1: the strongest systemic finding (smallest p-value), or a line saying there is none."""
    found = o["systemic_findings"].sort_values("p_value")
    if found.empty:
        return "**No systemic carrier pattern** cleared the effect-size and significance filters."
    f = found.iloc[0]
    start, end = pd.Timestamp(f["window_start"]), pd.Timestamp(f["window_end"])
    span = f"{start:%b %Y} to {end:%b %Y}"
    p = "p < 1e-300" if f["p_value"] < 1e-300 else f"p = {f['p_value']:.1e}"
    over = ("" if np.isnan(f["avg_billed_over_expected"])
            else f", billed {pct(f['avg_billed_over_expected'])} above expected on average")
    others = len(found) - 1
    more = f" {others} weaker pattern{'s' if others > 1 else ''} also flagged; see the dispute packs." if others else ""
    return (f"**{carrier_names.get(f['carrier_id'], f['carrier_id'])} ({f['carrier_id']}) has a systemic "
            f"{error_label(f['error_type']).lower()} issue.** {int(f['flagged'])} of {int(f['invoices'])} invoices "
            f"({pct(f['carrier_rate'])}) shipped {span} were flagged, against {pct(f['other_carriers_rate'])} at the "
            f"other {f['mode']} carriers ({p}){over}. Ask the carrier for a corrected table.{more}")


def trap_finding_text(o):
    """Bullet 2: the trap that caused the most baseline false positives, and how the engine did on it."""
    traps = o["eval_traps"]
    traps = traps[~traps["trap"].str.startswith("(none")]
    worst = traps.sort_values("baseline_fp", ascending=False).iloc[0]
    name = worst["trap"].replace("_", " ")
    by_type = (part.split(":") for part in worst["baseline_fp_by_type"].split(";"))
    top_type = max(by_type, key=lambda kv: int(kv[1]))
    why = TRAP_WHY.get(worst["trap"])
    why = f": {why}" if why else ""
    return (f"**The {name} trap fooled the baseline most.** It raised {int(worst['baseline_fp']):,} false flags across "
            f"{int(worst['n_invoices']):,} such invoices ({top_type[1]} of them {error_label(top_type[0]).lower()} flags){why}. "
            f"The engine raised {int(worst['engine_fp'])}.")


def accrual_finding_text(o):
    """Bullet 3: what drives accrual error, including the share due to authorizations recorded late.

    Shares are of the net (signed) error over the whole period, from the ALL rows of accrual_accuracy.csv and
    accrual_sensitivity.csv. The late-authorization share is the sensitivity's accrual change divided by the net error.
    """
    accuracy = o["accrual_accuracy"]
    monthly, total = accuracy[accuracy["month_end"] != "ALL"], accuracy[accuracy["month_end"] == "ALL"].iloc[0]
    sens = o["accrual_sensitivity"].set_index("month_end").loc["ALL"]
    error = float(total["error_estimate"])
    direction, gap = ("below", "shortfall") if error < 0 else ("above", "excess")
    accessorial_share_of_error = float(total["accessorial_error_estimate"]) / error
    accessorial_share_of_accrual = float(total["accessorial_accrual_estimate"]) / float(total["accrual_estimate"])
    late_share = float(sens["late_auth_effect_estimate"]) / abs(error)
    mape = float(monthly["error_pct_estimate"].abs().mean())
    driver = "Accessorials drive accrual error" if accessorial_share_of_error > 0.5 else "Linehaul and fuel drive accrual error"
    return (f"**{driver}.** Month-end accruals ran {pct(abs(float(total['error_pct_estimate'])), 2)} {direction} "
            f"eventual payable overall (mean absolute monthly error {pct(mape, 2)}, estimates). Accessorials are "
            f"{pct(accessorial_share_of_accrual)} of the accrual but {pct(accessorial_share_of_error, 0)} of the net {gap}. "
            f"Authorizations recorded after month-end explain {pct(late_share, 0)} of the net {gap}: recording every "
            f"one at delivery would remove that share.")


def key_findings(o, carrier_names):
    """The three bullets for the Overview tab, in order: systemic finding, worst trap, accrual drivers."""
    return [systemic_finding_text(o, carrier_names), trap_finding_text(o), accrual_finding_text(o)]
