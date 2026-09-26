"""outputs/summary.json: every headline number in one file.

The README, WALKTHROUGH.md and outputs/findings.md quote numbers only through this file (see docs.py), so a
rerun of the pipeline cannot leave a stale figure in the prose. Nothing here computes a new result: each value is
a lookup, sum or ratio over files the earlier stages already wrote to `outputs/`. It never reads the answer key;
precision, recall and false positives come from `eval_*.csv`, which `evaluate.py` wrote.

There is no wall-clock run date, on purpose: the same config must give byte-identical files, so the file carries
the seed and the audit as-of date instead.
"""

import json

import pandas as pd

from freight_audit_lab.accruals import accuracy_summary
from freight_audit_lab.audit.engine import OUTPUT_DIR
from freight_audit_lab.config import REPO_ROOT, get
from freight_audit_lab.csv_io import read_csv

SYSTEMS = ["engine", "baseline"]

# Config values the prose quotes (inputs, not results), so a changed config changes the documents too.
ASSUMPTION_KEYS = {"min_precision": "evaluation.min_precision", "min_material_gain": "evaluation.min_material_gain",
                   "false_dispute_cost": "evaluation.false_dispute_cost",
                   "review_minutes_per_flag": "evaluation.review_minutes_per_flag",
                   "analyst_cost_per_hour": "evaluation.analyst_cost_per_hour",
                   "systemic_multiple": "evaluation.systemic.multiple", "systemic_alpha": "evaluation.systemic.alpha"}
COUNT_KEYS = ["flags", "tp", "fp", "fn"]
RATE_KEYS = ["precision", "recall", "dollar_recall", "flagged_dollars_estimate"]


def _ratio(numerator, denominator, digits=4):
    return round(float(numerator) / float(denominator), digits) if denominator else 0.0


def scoring_block(row, system):
    """One system's counts and rates from a row of eval_engine_vs_baseline.csv."""
    block = {k: int(row[f"{system}_{k}"]) for k in COUNT_KEYS}
    block.update({k: float(row[f"{system}_{k}"]) for k in RATE_KEYS})
    return block


def engine_vs_baseline(ev):
    """Overall and by-type scoring for both systems: {"overall": {...}, "by_type": {type: {...}}}."""
    by_type = {}
    for row in ev[ev["error_type"] != "ALL"].to_dict("records"):
        by_type[row["error_type"]] = {s: scoring_block(row, s) for s in SYSTEMS}
    overall = ev[ev["error_type"] == "ALL"].iloc[0]
    return {"overall": {s: scoring_block(overall, s) for s in SYSTEMS}, "by_type": by_type}


def parse_counts(cell):
    """'duplicate_invoice:100;phantom_invoice:3' -> {'duplicate_invoice': 100, 'phantom_invoice': 3}; a blank cell -> {}."""
    return {} if pd.isna(cell) else {k: int(v) for k, v in (part.split(":") for part in cell.split(";"))}


def trap_summary(traps):
    """False flags per trap for both systems, the baseline's by rule, and the clean-invoice row on its own."""
    out = {"traps": {}, "clean_invoices": {}}
    for row in traps.to_dict("records"):
        entry = {"invoices": int(row["n_invoices"]), "engine_false_flags": int(row["engine_fp"]),
                 "baseline_false_flags": int(row["baseline_fp"]),
                 "engine_false_flags_by_type": parse_counts(row["engine_fp_by_type"]),
                 "baseline_false_flags_by_type": parse_counts(row["baseline_fp_by_type"])}
        if row["trap"].startswith("(none"):
            out["clean_invoices"] = {k: entry[k] for k in ("invoices", "engine_false_flags", "baseline_false_flags")}
        else:
            out["traps"][row["trap"]] = entry
    return out


def fp_cause_summary(causes):
    """The baseline's false flags with one cause each (baseline_fp_causes.csv): {"total": n, "causes": {key: {...}}}.
    The causes add up to `total`, unlike the trap table, where an invoice with two traps is counted under both."""
    return {"total": int(causes["false_flags"].sum()),
            "causes": {r["cause"]: {"label": r["description"], "false_flags": int(r["false_flags"]),
                                    "share": float(r["share_of_false_flags"]), "by_rule": parse_counts(r["by_rule"])}
                       for r in causes.to_dict("records")}}


def baseline_duplicates(by_type, trap_block, cause_block):
    """How many of the baseline's duplicate flags were wrong, and how many of those came from rebills and
    balance-due invoices (a rebill and its original, or a balance-due invoice and its original, share a BOL).
    The two per-trap counts cannot overlap (an invoice is a rebill or a balance-due, never both); the share is
    the duplicate false flags attributed to the rebill / balance-due cause, so it cannot exceed 100%."""
    row = by_type[(by_type["system"] == "baseline") & (by_type["error_type"] == "duplicate_invoice")].iloc[0]
    from_trap = {t: trap_block["traps"][t]["baseline_false_flags_by_type"].get("duplicate_invoice", 0)
                 for t in ("rebill", "balance_due")}
    false_flags = int(row["fp"])
    return {"flags": int(row["flags"]), "false_flags": false_flags, "false_share": _ratio(false_flags, row["flags"]),
            "false_from_rebills": from_trap["rebill"], "false_from_balance_due": from_trap["balance_due"],
            "share_explained_by_rebills_and_balance_due": _ratio(
                cause_block["causes"]["rebill_balance_due"]["by_rule"].get("duplicate_invoice", 0), false_flags)}


def engine_misses(by_type, by_mode):
    """Where the engine misses: how many injected errors it did not flag, how many of those sit below the audit's own
    tolerance, and what is left. Dollars are the answer key's (from eval_by_type.csv), not estimates."""
    eng = by_type[(by_type["system"] == "engine") & (by_type["error_type"] == "ALL")].iloc[0]
    by_mode = by_mode.assign(missed=by_mode["n_labeled"] - by_mode["engine_detected"])
    below = by_mode[by_mode["mode"] == "sub_tolerance"]
    other = by_mode[(by_mode["mode"] != "sub_tolerance") & (by_mode["missed"] > 0)]
    return {"missed": int(eng["fn"]), "below_tolerance": int(below["missed"].sum()),
            "other": int(other["missed"].sum()),
            "other_detail": {f"{r.error_type}_{r.mode}": int(r.missed) for r in other.itertuples()},
            "missed_true_dollars": round(float(eng["true_dollars"] - eng["tp_dollars"]), 2),
            "engine_false_positives": int(eng["fp"])}


def systemic_block(findings, flags, invoices, carrier_names):
    """Every systemic finding, strongest first, and the strongest one's mean flagged dollars per invoice (an estimate)."""
    found = findings.sort_values("p_value").reset_index(drop=True)
    keep = ["carrier_id", "error_type", "mode", "window_start", "window_end", "invoices", "flagged", "carrier_rate",
            "other_carriers_rate", "p_value", "alpha_adjusted", "avg_billed_over_expected"]
    rows = []
    for f in found.to_dict("records"):
        row = {k: (f[k].strftime("%Y-%m-%d") if isinstance(f[k], pd.Timestamp) else f[k]) for k in keep}
        row.update({k: int(row[k]) for k in ("invoices", "flagged")})
        row["carrier_name"] = carrier_names[f["carrier_id"]]
        mine = flags[flags["error_type"] == f["error_type"]].merge(invoices[["invoice_id", "carrier_id", "ship_date"]], on="invoice_id")
        mine = mine[(mine["carrier_id"] == f["carrier_id"]) & mine["ship_date"].between(f["window_start"], f["window_end"])]
        row["mean_flag_dollars_estimate"] = round(float(mine["dollar_impact_estimate"].mean()), 2) if len(mine) else 0.0
        rows.append(row)
    return {"n_findings": len(rows), "n_others": max(len(rows) - 1, 0), "strongest": rows[0] if rows else None,
            "others": rows[1:]}


def accrual_block(accuracy, sensitivity):
    """Accrual accuracy over the whole period, the share of error that is accessorial, and the late-authorization effect.
    Shares are of the net (signed) error, as on the dashboard."""
    total = accuracy[accuracy["month_end"] == "ALL"].iloc[0]
    what_if = sensitivity[sensitivity["month_end"] == "ALL"].iloc[0]
    monthly = accuracy[accuracy["month_end"] != "ALL"]
    worst = monthly.loc[monthly["error_pct_estimate"].abs().idxmax()]
    error = float(total["error_estimate"])
    headline = accuracy_summary(accuracy)
    return {"shipment_month_accruals": int(total["shipments_accrued"]),
            "accrual_estimate": float(total["accrual_estimate"]),
            "actual_payable_estimate": float(total["actual_payable_estimate"]),
            "actual_billed": float(total["actual_billed"]),
            "net_error_pct_vs_billed_estimate": _ratio(total["accrual_estimate"] - total["actual_billed"], total["actual_billed"]),
            "net_error_estimate": error, "net_error_pct_estimate": float(total["error_pct_estimate"]),
            "direction": "below" if error < 0 else "above", "gap_word": "shortfall" if error < 0 else "excess",
            "mape_pct_estimate": round(headline["mape_pct_estimate"], 4),
            "bias_pct_estimate": round(headline["bias_pct_estimate"], 4),
            "worst_month": {"month_end": str(worst["month_end"]), "error_pct_estimate": float(worst["error_pct_estimate"])},
            "linehaul_fuel_error_pct_estimate": float(total["lh_fsc_error_pct_estimate"]),
            "accessorial_error_pct_estimate": float(total["accessorial_error_pct_estimate"]),
            "accessorial_share_of_accrual": _ratio(total["accessorial_accrual_estimate"], total["accrual_estimate"]),
            "accessorial_share_of_net_error": _ratio(total["accessorial_error_estimate"], error),
            "late_authorization_effect_estimate": float(what_if["late_auth_effect_estimate"]),
            "late_authorization_share_of_net_error": _ratio(what_if["late_auth_effect_estimate"], abs(error)),
            "net_error_pct_if_authorized_at_delivery_estimate": float(what_if["error_pct_estimate_auth_at_delivery"])}


def tolerance_block(recommended):
    """Current, best-scoring and recommended value of each swept tolerance, and whether the recommendation changes it."""
    each = {name: {k: rec[k] for k in ("error_type", "carrier_mode", "current", "best_point", "recommended", "changed")}
            for name, rec in recommended.items()}
    return {"swept": len(each), "changed": sum(bool(v["changed"]) for v in each.values()), "each": each}


def build_summary(cfg, out_dir=OUTPUT_DIR, data_dir=REPO_ROOT / "data"):
    """Assemble the summary dict from outputs/ (plus the normalized invoice table for counts and ship dates)."""
    o = {name: read_csv(out_dir / f"{name}.csv") for name in [
        "audit_invoice_summary", "audit_flags", "eval_engine_vs_baseline", "eval_by_type", "eval_by_mode", "eval_traps",
        "baseline_fp_causes", "systemic_findings", "accrual_accuracy", "accrual_sensitivity", "exception_queue"]}
    invoices = read_csv(data_dir / "normalized" / "invoices.csv")
    recommended = json.loads((out_dir / "recommended_tolerances.json").read_text())
    audited = o["audit_invoice_summary"]
    billed, recoverable = float(audited["total"].sum()), float(audited["recoverable_estimate"].sum())
    ev = engine_vs_baseline(o["eval_engine_vs_baseline"])
    traps = trap_summary(o["eval_traps"])
    causes = fp_cause_summary(o["baseline_fp_causes"])
    return {
        "synthetic_data": True,
        "seed": cfg["seed"],
        "audit_as_of": str(get(cfg, "period.audit_as_of")),
        "period": {"start": str(get(cfg, "period.start")), "months": get(cfg, "period.months")},
        "scale": {"carriers": len(cfg["carriers"]), "shipments": len(read_csv(data_dir / "reference" / "shipments.csv")),
                  "invoices_received": len(invoices), "invoices_audited": len(audited), "billed_spend": round(billed, 2),
                  "invoices_flagged": int((audited["n_flags"] > 0).sum()), "exception_queue_rows": len(o["exception_queue"])},
        "recoverable": {"total_estimate": round(recoverable, 2), "pct_of_billed_estimate": _ratio(recoverable, billed),
                        "by_error_type_estimate": {t: v["engine"]["flagged_dollars_estimate"] for t, v in ev["by_type"].items()}},
        "engine_vs_baseline": ev,
        "traps": traps,
        "baseline_fp_causes": causes,
        "baseline_duplicates": baseline_duplicates(o["eval_by_type"], traps, causes),
        "engine_misses": engine_misses(o["eval_by_type"], o["eval_by_mode"]),
        "systemic": systemic_block(o["systemic_findings"], o["audit_flags"], invoices,
                                   {c["id"]: c["name"] for c in cfg["carriers"]}),
        "recommended_tolerances": tolerance_block(recommended),
        "assumptions": {k: get(cfg, path) for k, path in ASSUMPTION_KEYS.items()},
        "accruals": accrual_block(o["accrual_accuracy"], o["accrual_sensitivity"]),
    }


def write_summary(summary, out_dir=OUTPUT_DIR):
    """Write outputs/summary.json (fixed key order, two-space indent, trailing newline: reruns hash equal)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
