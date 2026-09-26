"""Evaluation: score the audit (and the baseline) against the answer key.

This module and sweep.py are the only code allowed to read `data/ground_truth/`; everything
upstream (normalize, rerate, audit, exceptions) is blind to it.

Unit of scoring. One *outcome* is an (invoice_id, error_type) pair, because the audit's question is
"does this invoice have this kind of error?", and an invoice can hold several. A flag with a matching
error label is a true positive (TP), a flag with none is a false positive (FP), and an error label
with no flag is a false negative (FN). Superseded invoices are excluded from both sides: the rebill
replaced them, so nobody should be working them.

Dollars. "Flagged $" is the audit's *estimate* and counts only flags that count toward recoverable
dollars (`counted_in_recoverable`), so a duplicate that also carries a rate flag is not counted twice.
"True $" comes from the answer key. Dollar recall is the true $ of the TPs over all true $.
"""

from pathlib import Path

import numpy as np
import pandas as pd

from freight_audit_lab.audit.rules import RULES
from freight_audit_lab.config import REPO_ROOT
from freight_audit_lab.csv_io import read_csv, write_csv

DATA_DIR = REPO_ROOT / "data"
OUTPUT_DIR = REPO_ROOT / "outputs"
ERROR_TYPES = [rule.__name__ for rule in RULES]
NO_TRAP = "(none: clean invoice)"
KEY = ["invoice_id", "error_type"]
SYSTEMS = ["engine", "baseline"]


def load_labels(data_dir=DATA_DIR):
    """The answer key: one row per (invoice_id, label). Blank `mode` becomes ''."""
    labels = read_csv(Path(data_dir) / "ground_truth" / "labels.csv")
    labels["mode"] = labels["mode"].fillna("")
    return labels


def superseded_ids(labels):
    """Invoices a rebill replaced. They are out of scope for scoring."""
    return set(labels.loc[labels["label_kind"] == "superseded", "invoice_id"])


def outcomes(flags, labels, scope=None):
    """One row per (invoice_id, error_type) with `status` TP / FP / FN and its dollars.

    `flags` needs invoice_id, error_type, dollar_impact_estimate, counted_in_recoverable.
    `scope` optionally limits scoring to a set of invoice ids (used to score one carrier mode).
    """
    truth = (labels[labels["label_kind"] == "error"].groupby(["invoice_id", "label"], as_index=False)
             ["true_dollar_impact"].sum().rename(columns={"label": "error_type", "true_dollar_impact": "true_dollars"}))
    counted = flags["dollar_impact_estimate"].where(flags["counted_in_recoverable"], 0.0)
    flagged = (flags.assign(_counted=counted).groupby(KEY, as_index=False)
               .agg(flagged_dollars_estimate=("_counted", "sum")))
    out = flagged.merge(truth, on=KEY, how="outer", indicator=True)
    out["status"] = out["_merge"].astype(str).map({"both": "TP", "left_only": "FP", "right_only": "FN"})
    out = out.drop(columns="_merge").fillna({"flagged_dollars_estimate": 0.0, "true_dollars": 0.0})
    out = out[~out["invoice_id"].isin(superseded_ids(labels))]
    if scope is not None:
        out = out[out["invoice_id"].isin(scope)]
    return out.reset_index(drop=True)


def safe_ratio(num, den):
    return num / den if den else np.nan


def metrics(o):
    """Counts, precision, recall, F1 and dollars for a set of outcome rows."""
    tp, fp, fn = (int((o["status"] == s).sum()) for s in ("TP", "FP", "FN"))
    precision, recall = safe_ratio(tp, tp + fp), safe_ratio(tp, tp + fn)
    f1 = safe_ratio(2 * tp, 2 * tp + fp + fn)           # same as 2PR / (P + R)
    true_dollars = float(o["true_dollars"].sum())
    tp_dollars = float(o.loc[o["status"] == "TP", "true_dollars"].sum())
    return {"flags": tp + fp, "tp": tp, "fp": fp, "fn": fn,
            "precision": precision, "recall": recall, "f1": f1,
            "flagged_dollars_estimate": round(float(o["flagged_dollars_estimate"].sum()), 2),
            "true_dollars": round(true_dollars, 2), "tp_dollars": round(tp_dollars, 2),
            "dollar_recall": safe_ratio(tp_dollars, true_dollars)}


def by_type(o, system):
    """One row per error type plus an ALL row, for one system (engine or baseline)."""
    rows = [{"system": system, "error_type": t, **metrics(o[o["error_type"] == t])} for t in ERROR_TYPES]
    return pd.DataFrame(rows + [{"system": system, "error_type": "ALL", **metrics(o)}])


def by_mode(labels, outs):
    """Recall for each (error_type, mode) label group, for every system in `outs`.

    The `sub_tolerance` rows are errors injected below the tolerances on purpose, so the engine
    is expected to miss them; showing them separately keeps that from looking like a defect.
    """
    errors = labels[(labels["label_kind"] == "error") & ~labels["invoice_id"].isin(superseded_ids(labels))]
    errors = errors.rename(columns={"label": "error_type"})
    table = (errors.groupby(["error_type", "mode"], as_index=False)
             .agg(n_labeled=("invoice_id", "size"), true_dollars=("true_dollar_impact", "sum")))
    for system, o in outs.items():
        hit = o.loc[o["status"] == "TP", KEY].assign(_hit=True)
        marked = errors.merge(hit, on=KEY, how="left")
        marked["_hit"] = marked["_hit"].fillna(False).astype(bool)
        marked["_hit_dollars"] = marked["true_dollar_impact"].where(marked["_hit"], 0.0)
        agg = marked.groupby(["error_type", "mode"], as_index=False).agg(
            detected=("_hit", "sum"), _hit_dollars=("_hit_dollars", "sum"))
        table = table.merge(agg, on=["error_type", "mode"])
        table[f"{system}_detected"] = table.pop("detected")
        table[f"{system}_recall"] = table[f"{system}_detected"] / table["n_labeled"]
        table[f"{system}_dollar_recall"] = (table.pop("_hit_dollars") / table["true_dollars"].where(table["true_dollars"] > 0))
    table["true_dollars"] = table["true_dollars"].round(2)
    order = table["error_type"].map(ERROR_TYPES.index)
    return table.assign(_o=order).sort_values(["_o", "mode"]).drop(columns="_o").reset_index(drop=True)


def trap_table(labels, outs):
    """For each trap type: invoices carrying it and the false positives each system raised on them.

    A false positive here is any (invoice, error_type) flag with no matching error label, on an invoice
    that carries the trap. An invoice with two traps counts under both. The last row is the same
    count over clean invoices (no error, trap, or supersession).
    """
    live = labels[~labels["invoice_id"].isin(superseded_ids(labels))]
    groups = {t: set(g["invoice_id"]) for t, g in live[live["label_kind"] == "trap"].groupby("label")}
    groups[NO_TRAP] = set(live.loc[live["label_kind"] == "clean", "invoice_id"])
    rows = []
    for name, ids in groups.items():
        row = {"trap": name, "n_invoices": len(ids)}
        for system, o in outs.items():
            fp = o[(o["status"] == "FP") & o["invoice_id"].isin(ids)]
            row[f"{system}_fp"] = len(fp)
            row[f"{system}_fp_by_type"] = ";".join(f"{t}:{n}" for t, n in fp["error_type"].value_counts().sort_index().items())
        rows.append(row)
    table = pd.DataFrame(rows)
    return table.assign(_c=table["trap"] == NO_TRAP).sort_values(["_c", "trap"]).drop(columns="_c").reset_index(drop=True)


def engine_vs_baseline(type_tables):
    """Headline metrics side by side, one row per error type plus ALL."""
    cols = ["flags", "tp", "fp", "fn", "precision", "recall", "f1", "dollar_recall", "flagged_dollars_estimate"]
    wide = None
    for system, table in type_tables.items():
        part = table.set_index("error_type")[cols].add_prefix(f"{system}_")
        wide = part if wide is None else wide.join(part)
    return wide.reset_index()


def evaluate(engine_flags, baseline_flags, labels):
    """Score both systems. Returns {"by_type", "by_mode", "traps", "engine_vs_baseline", "outcomes"}."""
    outs = {"engine": outcomes(engine_flags, labels), "baseline": outcomes(baseline_flags, labels)}
    type_tables = {system: by_type(o, system) for system, o in outs.items()}
    return {"by_type": pd.concat(type_tables.values(), ignore_index=True),
            "by_mode": by_mode(labels, outs),
            "traps": trap_table(labels, outs),
            "engine_vs_baseline": engine_vs_baseline(type_tables),
            "outcomes": outs}


def round_for_csv(df):
    """Ratios to 4 decimals so reruns write identical files."""
    return df.round({c: 4 for c in df.columns if "precision" in c or "recall" in c or c.endswith("f1")})


def write_evaluation(result, out_dir=OUTPUT_DIR):
    """Write outputs/eval_by_type.csv, eval_by_mode.csv, eval_traps.csv, eval_engine_vs_baseline.csv."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in ("by_type", "by_mode", "traps", "engine_vs_baseline"):
        write_csv(round_for_csv(result[name]), out_dir / f"eval_{name}.csv")
