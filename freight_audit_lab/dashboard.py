"""What the dashboard shows, worked out from the files in `outputs/`.

The dashboard adds no numbers of its own. Every figure on the Overview tab is a sum, a ratio or a
lookup over an output file, and every sentence in "Key findings" is a template filled from those
files, so re-running the pipeline changes the page and nothing here has to be edited. Nothing in
this module reads the answer key: precision, recall and false positives come from `eval_*.csv`,
which `evaluate.py` wrote.
"""

import re

import numpy as np
import pandas as pd

from freight_audit_lab.audit.engine import OUTPUT_DIR
from freight_audit_lab.csv_io import read_csv
from freight_audit_lab.exceptions import p_text

OUTPUT_FILES = ["audit_invoice_summary", "eval_by_type", "eval_engine_vs_baseline", "eval_traps", "baseline_fp_causes", "exception_queue",
                "sweep", "systemic_findings", "accrual_accuracy", "accrual_sensitivity", "journal_entries"]

ERROR_LABELS = {"duplicate_invoice": "Duplicate invoice", "phantom_invoice": "Phantom invoice",
                "rate_overcharge": "Rate overcharge", "fsc_mismatch": "Fuel surcharge mismatch",
                "unauthorized_accessorial": "Unauthorized accessorial", "weight_overbilling": "Weight overbilling"}

# Why the naive baseline raises a false flag for each cause. These describe the baseline's shortcuts (ASSUMPTIONS.md,
# Stage 4) and carry no numbers; a cause missing here just gets no explanation in the finding.
CAUSE_WHY = {"rate_amendment": "it prices every shipment at the first rate row and ignores effective dates",
             "reweigh": "it ignores reweigh certificates, so a certified heavier weight looks overbilled",
             "weight_misread_as_rate": "it does not separate weight from rate, so one weight overbilling is also flagged as a rate overcharge",
             "late_authorization": "it asks whether an accessorial was authorized as of the invoice date",
             "rebill_balance_due": "it treats a rebill or a balance-due invoice as a copy of the original",
             "bol_typo_or_zero": "it cannot match a BOL with a typo or dropped leading zeros, so it calls the invoice a phantom"}


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


def demote_headings(markdown):
    """Turn every `# heading` line into a bold line, so an embedded document does not out-shout the page."""
    return re.sub(r"^#{1,6}\s+(.*)$", r"**\1**", markdown, flags=re.MULTILINE)


def strongest_systemic_carrier(o):
    """Carrier id of the systemic finding with the smallest p-value (the one the dispute viewer opens on), or None."""
    found = o["systemic_findings"].sort_values("p_value")
    return None if found.empty else str(found.iloc[0]["carrier_id"])


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
    p = p_text(f["p_value"])
    over = ("" if np.isnan(f["avg_billed_over_expected"])
            else f", billed {pct(f['avg_billed_over_expected'])} above expected on average")
    others = len(found) - 1
    more = f" {others} weaker pattern{'s' if others > 1 else ''} also flagged; see the dispute packs." if others else ""
    return (f"**{carrier_names.get(f['carrier_id'], f['carrier_id'])} ({f['carrier_id']}) has a systemic "
            f"{error_label(f['error_type']).lower()} issue.** {int(f['flagged'])} of {int(f['invoices'])} invoices "
            f"({pct(f['carrier_rate'])}) shipped {span} were flagged, against {pct(f['other_carriers_rate'])} at the "
            f"other {f['mode']} carriers ({p}){over}. Ask the carrier for a corrected table.{more}")


def trap_finding_text(o):
    """Bullet 2: the cause behind the most baseline false flags. Each false flag has exactly one cause
    (baseline_fp_causes.csv), so the counts add up; "other" is never named as the top cause."""
    causes = o["baseline_fp_causes"]
    total = int(causes["false_flags"].sum())
    top = causes[causes["cause"] != "other"].sort_values("false_flags", ascending=False, kind="stable").iloc[0]
    why = CAUSE_WHY.get(top["cause"])
    why = f": {why}" if why else ""
    engine_fp = int(o["eval_engine_vs_baseline"].set_index("error_type").loc["ALL", "engine_fp"])
    return (f"**{top['description'].capitalize()} caused the most baseline false flags.** {int(top['false_flags']):,} of "
            f"its {total:,} false flags ({pct(top['false_flags'] / total, 0)}), each counted under one cause only{why}. "
            f"The engine raised {engine_fp} false flag{'s' if engine_fp != 1 else ''} in total.")


def accrual_finding_text(o, driver_share):
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
    driver = "Accessorials drive accrual error" if accessorial_share_of_error > driver_share else "Linehaul and fuel drive accrual error"
    return (f"**{driver}.** Month-end accruals ran {pct(abs(float(total['error_pct_estimate'])), 2)} {direction} "
            f"eventual payable overall (mean absolute monthly error {pct(mape, 2)}, estimates). Accessorials are "
            f"{pct(accessorial_share_of_accrual)} of the accrual but {pct(accessorial_share_of_error, 0)} of the net {gap}. "
            f"Authorizations recorded after month-end explain {pct(late_share, 0)} of the net {gap}: recording every "
            f"one at delivery would remove that share.")


def key_findings(o, carrier_names, driver_share):
    """The three bullets for the Overview tab, in order: systemic finding, top false-flag cause, accrual drivers."""
    return [systemic_finding_text(o, carrier_names), trap_finding_text(o), accrual_finding_text(o, driver_share)]
