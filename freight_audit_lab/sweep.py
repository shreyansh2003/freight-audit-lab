"""Tolerance sweep: how do precision, recall and dollars move as one tolerance changes?

Each tolerance is varied over its grid in `evaluation.sweep` with every other tolerance held at its
`config.yaml` value. A tolerance governs one rule (and for the two fuel tolerances, one carrier
mode), so each point is scored on just the invoices that rule can affect: the swept rule's flags
against the answer key's labels for that error type. Scoring the whole engine instead would bury a
change in one rule under the unchanged flags of the others, and the precision floor would then say
nothing about the rule being tuned.

Business model behind the net value (all three are *estimates* from config):
  review_cost_estimate         = flags x review_minutes_per_flag / 60 x analyst_cost_per_hour
  false_dispute_cost_estimate  = false positives x false_dispute_cost   (carrier friction)
  net_value_estimate           = TP dollars - both costs
TP dollars are the answer key's true dollars on the true positives.

The *best* point maximizes net value among the points whose precision is at least `min_precision`.
It is *recommended* only if it beats the current setting by more than `min_material_gain` dollars;
otherwise the recommendation is to keep the current value (a few dollars on a noisy estimate is not a
reason to change a control). This module only reports; it never edits config.yaml.

Like evaluate.py, this module may read the answer key (it does so only through evaluate.load_labels).
"""

import copy
import json

import numpy as np
import pandas as pd

from freight_audit_lab.audit.rules import fsc_mismatch, rate_overcharge, weight_overbilling
from freight_audit_lab.csv_io import write_csv
from freight_audit_lab.evaluate import OUTPUT_DIR, metrics, outcomes

# tolerance -> (rule that uses it, error type it produces, carrier mode it applies to or None for both)
SWEEPS = {
    "rate_pct": (rate_overcharge, "rate_overcharge", None),
    "fsc_ltl_pp": (fsc_mismatch, "fsc_mismatch", "LTL"),
    "fsc_tl_pct": (fsc_mismatch, "fsc_mismatch", "TL"),
    "weight_pct": (weight_overbilling, "weight_overbilling", None),
}
RULE_WORDS = {"rate_overcharge": "linehaul rate", "fsc_mismatch": "fuel surcharge", "weight_overbilling": "weight"}
SWEEP_COLUMNS = ["tolerance", "value", "error_type", "carrier_mode", "is_current", "flags", "tp", "fp", "fn",
                 "precision", "recall", "tp_dollars", "review_cost_estimate", "false_dispute_cost_estimate",
                 "net_value_estimate", "meets_precision_floor", "is_best", "is_recommended"]
CAVEATS = {"weight_pct": "Caveat: the synthetic data has no scale noise (a billed weight is exact unless an error was "
                         "injected), so it cannot calibrate the weight tolerance; use real scale-ticket differences."}


def grid_with_current(grid, current):
    """The configured grid, plus the current setting if it is not already on it, sorted."""
    return sorted({round(float(v), 6) for v in list(grid) + [current]})


def score_point(rule, error_type, scope, norm, rerated, ref, cfg, labels):
    """Run one rule at the config's tolerances and score it on `scope` (a set of invoice ids)."""
    flags = rule(norm, rerated, ref, cfg)
    flags = flags[flags["error_type"] == error_type]
    o = outcomes(flags.assign(counted_in_recoverable=True), labels, scope)      # one rule, so nothing to dedupe
    return metrics(o[o["error_type"] == error_type])


def net_value(m, eval_cfg):
    """(review cost, false-dispute cost, net value) estimates for a scored point."""
    review = m["flags"] * eval_cfg["review_minutes_per_flag"] / 60 * eval_cfg["analyst_cost_per_hour"]
    false_dispute = m["fp"] * eval_cfg["false_dispute_cost"]
    return round(review, 2), round(false_dispute, 2), round(m["tp_dollars"] - review - false_dispute, 2)


def sweep_one(name, norm, rerated, ref, cfg, labels):
    """Sweep one tolerance. Returns a DataFrame with one row per grid point (SWEEP_COLUMNS, no recommendation yet)."""
    rule, error_type, mode = SWEEPS[name]
    eval_cfg = cfg["evaluation"]
    current = cfg["audit"]["tolerances"][name]
    inv = norm["invoices"]
    carrier_mode = {c["id"]: c["mode"] for c in cfg["carriers"]}
    in_mode = inv["carrier_id"].map(carrier_mode) == mode if mode else pd.Series(True, index=inv.index)
    scope = set(inv.loc[in_mode, "invoice_id"])
    rows = []
    for value in grid_with_current(eval_cfg["sweep"][name], current):
        variant = copy.deepcopy(cfg)
        variant["audit"]["tolerances"][name] = value
        m = score_point(rule, error_type, scope, norm, rerated, ref, variant, labels)
        review, false_dispute, net = net_value(m, eval_cfg)
        rows.append({"tolerance": name, "value": value, "error_type": error_type, "carrier_mode": mode or "ALL",
                     "is_current": bool(np.isclose(value, current)),
                     "flags": m["flags"], "tp": m["tp"], "fp": m["fp"], "fn": m["fn"],
                     "precision": m["precision"], "recall": m["recall"], "tp_dollars": m["tp_dollars"],
                     "review_cost_estimate": review, "false_dispute_cost_estimate": false_dispute,
                     "net_value_estimate": net})
    table = pd.DataFrame(rows)
    table["meets_precision_floor"] = table["precision"] >= eval_cfg["min_precision"]
    return table


def best_point(table):
    """Index of the best row: highest net value among rows meeting the precision floor.

    Ties go to the point closest to the current setting. If no row meets the floor, the highest-precision
    row is returned (and the rationale says so).
    """
    eligible = table[table["meets_precision_floor"]]
    if eligible.empty:
        return table["precision"].fillna(-1).idxmax()
    best = eligible[eligible["net_value_estimate"] == eligible["net_value_estimate"].max()]
    current = table.loc[table["is_current"], "value"].iloc[0]
    return (best["value"] - current).abs().idxmin()


def recommend(table, min_precision, min_material_gain):
    """(best index, recommended index).

    The recommendation is the best point only if it beats the current setting's net value by MORE than
    `min_material_gain`; otherwise it is the current setting. One exception: if the current setting
    itself fails the precision floor, the floor is a hard constraint, so the best point is recommended
    whatever the gain.
    """
    best = best_point(table)
    cur = table[table["is_current"]].iloc[0]
    gain = table.at[best, "net_value_estimate"] - cur["net_value_estimate"]
    if table.at[best, "value"] == cur["value"]:
        return best, best
    if not cur["meets_precision_floor"] and table.at[best, "meets_precision_floor"]:
        return best, best
    return (best, best) if gain > min_material_gain else (best, table.index[table["is_current"]][0])


def money(x):
    return f"-${abs(x):,.0f}" if x < 0 else f"${x:,.0f}"


def rationale(table, best, rec, min_precision, min_material_gain, caveat=""):
    """Sentences built only from the numbers in the sweep table, ending with any caveat for this tolerance."""
    b, r = table.loc[best], table.loc[rec]
    cur = table[table["is_current"]].iloc[0]
    words = RULE_WORDS[b["error_type"]] + (f" ({b['carrier_mode']})" if b["carrier_mode"] != "ALL" else "")
    gain = b["net_value_estimate"] - cur["net_value_estimate"]
    if not b["meets_precision_floor"]:
        first = (f"No point on the grid reaches {min_precision:.0%} precision for the {words} check; {b['value']:g} has "
                 f"the highest ({b['precision']:.1%}, recall {b['recall']:.1%}, net value estimate {money(b['net_value_estimate'])}).")
    elif b["value"] == cur["value"]:
        n_ok = int(table["meets_precision_floor"].sum())
        first = (f"Keep {cur['value']:g}: at that setting the {words} check flags {cur['flags']} invoices (precision "
                 f"{cur['precision']:.1%}, recall {cur['recall']:.1%}), the highest net value estimate ({money(cur['net_value_estimate'])}) "
                 f"of the {n_ok} grid points that meet the {min_precision:.0%} precision floor.")
    elif rec == best:
        first = (f"Change to {b['value']:g}: the {words} check then flags {b['flags']} invoices (precision {b['precision']:.1%}, "
                 f"recall {b['recall']:.1%}) for a net value estimate of {money(b['net_value_estimate'])}, {money(gain)} above the "
                 f"current {cur['value']:g} ({money(cur['net_value_estimate'])}), more than the {money(min_material_gain)} materiality threshold.")
        if not cur["meets_precision_floor"]:
            first += f" The current setting is below the {min_precision:.0%} precision floor ({cur['precision']:.1%})."
    else:
        first = (f"Keep {cur['value']:g}: the best point, {b['value']:g}, has a net value estimate of {money(b['net_value_estimate'])}, "
                 f"only {money(gain)} above the current {money(cur['net_value_estimate'])} (precision {cur['precision']:.1%}, recall "
                 f"{cur['recall']:.1%}), which is not more than the {money(min_material_gain)} materiality threshold.")
    lower = table[table["value"] < r["value"]]
    if not lower.empty:
        o = lower.iloc[-1]
        third = (f"Going lower to {o['value']:g} would add {o['tp'] - r['tp']} true and {o['fp'] - r['fp']} false flags "
                 f"(precision {o['precision']:.1%}), for a net value estimate of {money(o['net_value_estimate'])}.")
    else:
        higher = table[table["value"] > r["value"]]
        o = higher.iloc[0] if not higher.empty else None
        third = ("" if o is None else f"Going higher to {o['value']:g} would drop {r['tp'] - o['tp']} true and {r['fp'] - o['fp']} "
                 f"false flags, for a net value estimate of {money(o['net_value_estimate'])}.")
    return " ".join(x for x in (first, third, caveat) if x)


def run_sweep(norm, rerated, ref, cfg, labels):
    """Sweep every tolerance. Returns {"sweep": DataFrame, "recommended": dict}."""
    tables, recommended = [], {}
    ev = cfg["evaluation"]
    for name in SWEEPS:
        table = sweep_one(name, norm, rerated, ref, cfg, labels)
        best, rec = recommend(table, ev["min_precision"], ev["min_material_gain"])
        table["is_best"] = table.index == best
        table["is_recommended"] = table.index == rec
        tables.append(table)
        cur = table[table["is_current"]].iloc[0]
        recommended[name] = {"error_type": table.at[rec, "error_type"], "carrier_mode": table.at[rec, "carrier_mode"],
                             "current": float(cur["value"]), "recommended": float(table.at[rec, "value"]),
                             "changed": bool(rec != table.index[table["is_current"]][0]),
                             "best_point": float(table.at[best, "value"]),
                             "min_precision": ev["min_precision"], "min_material_gain": ev["min_material_gain"],
                             "current_net_value_estimate": float(cur["net_value_estimate"]),
                             "best_net_value_estimate": float(table.at[best, "net_value_estimate"]),
                             "rationale": rationale(table, best, rec, ev["min_precision"], ev["min_material_gain"],
                                                    CAVEATS.get(name, ""))}
    return {"sweep": pd.concat(tables, ignore_index=True)[SWEEP_COLUMNS], "recommended": recommended}


def write_sweep(result, out_dir=OUTPUT_DIR):
    """Write outputs/sweep.csv and outputs/recommended_tolerances.json."""
    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(result["sweep"].round({"precision": 4, "recall": 4}), out_dir / "sweep.csv")
    (out_dir / "recommended_tolerances.json").write_text(json.dumps(result["recommended"], indent=2) + "\n")
