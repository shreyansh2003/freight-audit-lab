"""The answer key: one row per (invoice_id, label), written only to data/ground_truth/.

Only evaluate.py and sweep.py may read this file. Nothing the audit pipeline reads (raw
files, reference data, normalized tables) may contain any of these columns.
"""

import pandas as pd

LABEL_COLUMNS = ["invoice_id", "label_kind", "label", "mode", "true_dollar_impact"]
KIND_ORDER = {"error": 0, "trap": 1, "superseded": 2, "clean": 3}


def build_labels(invoices):
    """Labels for every invoice.

    A superseded original is labeled only `superseded` (evaluation excludes it). Otherwise it
    carries its errors (with true dollar impact) and traps (impact 0), or `clean` if it has none.
    """
    rows = []
    for inv in invoices:
        key = inv["invoice_id"]
        if inv["superseded"]:
            rows.append((key, "superseded", "superseded", "", 0.0))
            continue
        rows += [(key, "error", label, mode, impact) for label, mode, impact in inv["errors"]]
        rows += [(key, "trap", label, mode, 0.0) for label, mode in inv["traps"]]
        if not inv["errors"] and not inv["traps"]:
            rows.append((key, "clean", "clean", "", 0.0))
    df = pd.DataFrame(rows, columns=LABEL_COLUMNS)
    df["_order"] = df["label_kind"].map(KIND_ORDER)
    df = df.sort_values(["invoice_id", "_order", "label"], kind="mergesort")
    return df.drop(columns="_order").reset_index(drop=True)
