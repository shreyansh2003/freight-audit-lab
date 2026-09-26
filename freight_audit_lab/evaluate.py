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

# Baseline false flags, one cause each (see baseline_fp_causes). (key, plain-English label), in precedence order.
FP_CAUSES = [("rate_amendment", "rate amendment"),
             ("reweigh", "reweigh"),
             ("weight_misread_as_rate", "weight error misread as rate"),
             ("late_authorization", "late authorization"),
             ("rebill_balance_due", "rebill / balance due"),
             ("bol_typo_or_zero", "BOL typo or dropped leading zero"),
             ("other", "other")]
CAUSE_LABEL = dict(FP_CAUSES)


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


def fp_cause(error_type, traps, errors, bol_zero_dropped):
    """The one cause of a false flag, from the rule that raised it and what the invoice's paper trail says.

    Precedence, first match wins (this is the order the causes are listed in FP_CAUSES):
      1. rate flag on an invoice at an amended carrier-lane rate           -> rate amendment
      2. rate or weight flag on an invoice billed at a certified reweigh weight -> reweigh
      3. rate flag on an invoice whose weight really was overbilled         -> weight error misread as rate
      4. accessorial flag where the authorization was recorded after the invoice date -> late authorization
      5. duplicate flag on a rebill or balance-due invoice                  -> rebill / balance due
      6. phantom flag where the BOL has a typo or lost its leading zeros    -> BOL typo or dropped leading zero
      7. anything else                                                      -> other
    The rule that raised the flag settles most cases (only rate flags can reach 1, 2 or 3); precedence matters
    only when a rate flag qualifies for more than one, e.g. an amended lane that was also reweighed: the
    amendment is counted, not the reweigh. Causes are read from the invoice's trap and error labels, so a
    resent duplicate (which copies no trap label but its BOL's) of an amended-lane invoice falls to "other".
    """
    if error_type == "rate_overcharge":
        if "rate_amendment" in traps:
            return "rate_amendment"
        if "documented_reweigh" in traps:
            return "reweigh"
        if "weight_overbilling" in errors:
            return "weight_misread_as_rate"
    elif error_type == "weight_overbilling" and "documented_reweigh" in traps:
        return "reweigh"
    elif error_type == "unauthorized_accessorial" and "late_authorization" in traps:
        return "late_authorization"
    elif error_type == "duplicate_invoice" and traps & {"rebill", "balance_due"}:
        return "rebill_balance_due"
    elif error_type == "phantom_invoice" and ("bol_typo" in traps or bol_zero_dropped):
        return "bol_typo_or_zero"
    return "other"


def baseline_fp_causes(labels, baseline_outcomes):
    """Attribute each baseline false flag to exactly one cause, so the causes add up to the total.

    The trap table (`trap_table`) counts a false flag under every trap its invoice carries, which double
    counts: a rate flag on an amended-lane BOL-noise invoice appears under both. This table answers the
    question "why did the baseline raise this flag?" once per flag, using the precedence in `fp_cause`.
    The unit is the same as everywhere else: one (invoice_id, error_type) outcome with status FP.
    """
    traps = labels[labels["label_kind"] == "trap"]
    trap_sets = traps.groupby("invoice_id")["label"].agg(set)
    zero_ids = set(traps.loc[(traps["label"] == "bol_format") & (traps["mode"] == "zeros_dropped"), "invoice_id"])
    error_sets = labels[labels["label_kind"] == "error"].groupby("invoice_id")["label"].agg(set)
    fp = baseline_outcomes[baseline_outcomes["status"] == "FP"]
    cause = [fp_cause(row.error_type, trap_sets.get(row.invoice_id, set()), error_sets.get(row.invoice_id, set()),
                      row.invoice_id in zero_ids) for row in fp.itertuples()]
    counts = pd.Series(cause, dtype="object").value_counts()
    total = len(fp)
    table = pd.DataFrame({"cause": [k for k, _ in FP_CAUSES], "description": [v for _, v in FP_CAUSES]})
    table["false_flags"] = table["cause"].map(counts).fillna(0).astype(int)
    table["share_of_false_flags"] = table["false_flags"] / total if total else 0.0
    by_rule = pd.DataFrame({"cause": cause, "error_type": fp["error_type"].to_numpy()})
    table["by_rule"] = table["cause"].map(lambda c: ";".join(
        f"{t}:{n}" for t, n in by_rule.loc[by_rule["cause"] == c, "error_type"].value_counts().sort_index().items()))
    assert table["false_flags"].sum() == total          # every false flag has exactly one cause
    table = table.assign(_o=table["cause"] == "other").sort_values(["_o", "false_flags"], ascending=[True, False], kind="stable")
    return table.drop(columns="_o").reset_index(drop=True)


def engine_vs_baseline(type_tables):
    """Headline metrics side by side, one row per error type plus ALL."""
    cols = ["flags", "tp", "fp", "fn", "precision", "recall", "f1", "dollar_recall", "flagged_dollars_estimate"]
    wide = None
    for system, table in type_tables.items():
        part = table.set_index("error_type")[cols].add_prefix(f"{system}_")
        wide = part if wide is None else wide.join(part)
    return wide.reset_index()


def evaluate(engine_flags, baseline_flags, labels):
    """Score both systems. Returns {"by_type", "by_mode", "traps", "baseline_fp_causes", "engine_vs_baseline", "outcomes"}."""
    outs = {"engine": outcomes(engine_flags, labels), "baseline": outcomes(baseline_flags, labels)}
    type_tables = {system: by_type(o, system) for system, o in outs.items()}
    return {"by_type": pd.concat(type_tables.values(), ignore_index=True),
            "by_mode": by_mode(labels, outs),
            "traps": trap_table(labels, outs),
            "baseline_fp_causes": baseline_fp_causes(labels, outs["baseline"]),
            "engine_vs_baseline": engine_vs_baseline(type_tables),
            "outcomes": outs}


def round_for_csv(df):
    """Ratios to 4 decimals so reruns write identical files."""
    return df.round({c: 4 for c in df.columns if "precision" in c or "recall" in c or c.endswith("f1")})


def write_evaluation(result, out_dir=OUTPUT_DIR):
    """Write outputs/eval_by_type.csv, eval_by_mode.csv, eval_traps.csv, eval_engine_vs_baseline.csv and
    baseline_fp_causes.csv."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in ("by_type", "by_mode", "traps", "engine_vs_baseline"):
        write_csv(round_for_csv(result[name]), out_dir / f"eval_{name}.csv")
    write_csv(result["baseline_fp_causes"].round({"share_of_false_flags": 4}), out_dir / "baseline_fp_causes.csv")
