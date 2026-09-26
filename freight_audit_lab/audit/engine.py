"""Audit engine: run every rule, then decide how many flagged dollars are really recoverable.

One invoice can trip several rules (a heavier billed weight also raises the rate check's
linehaul, a bad linehaul also moves the fuel surcharge). The re-rater already splits those
impacts so they do not overlap, so the invoice's recoverable estimate is the sum of its
impacts, never more than the invoice total. Duplicate and phantom invoices are different:
the *whole* invoice is the claim, so it counts once at its total and any other flags on it
are kept for information at $0.
"""

import pandas as pd

from freight_audit_lab.audit.rules import RULES
from freight_audit_lab.config import REPO_ROOT
from freight_audit_lab.csv_io import write_csv

OUTPUT_DIR = REPO_ROOT / "outputs"
WHOLE_INVOICE_TYPES = ["duplicate_invoice", "phantom_invoice"]   # the claim is the invoice total


def apply_recoverable(flags, invoices):
    """Mark which flags count toward recoverable dollars and total them per invoice.

    On an invoice with a duplicate or phantom flag only the first such flag counts (its impact
    is the invoice total); every other flag on it is kept but not counted. Elsewhere all flags
    count. The invoice's `recoverable_estimate` is the counted impacts, capped at its total.
    Returns (flags with `counted_in_recoverable`, Series indexed by invoice_id).
    """
    flags = flags.copy()
    whole = flags["error_type"].isin(WHOLE_INVOICE_TYPES)
    first_whole = whole & (whole.groupby(flags["invoice_id"]).cumsum() == 1)   # flags are in rule order
    has_whole = flags["invoice_id"].isin(flags.loc[whole, "invoice_id"])
    flags["counted_in_recoverable"] = first_whole | ~has_whole
    counted = flags[flags["counted_in_recoverable"]].groupby("invoice_id")["dollar_impact_estimate"].sum()
    totals = invoices.set_index("invoice_id")["total"]
    recoverable = pd.concat([counted, totals.reindex(counted.index)], axis=1).min(axis=1).round(2)
    return flags, recoverable.rename("recoverable_estimate")


def process_metrics(rerated):
    """Counts that describe the shipper's process rather than carrier errors.

    An accessorial authorized after the carrier's invoice date is not a billing error, but it
    means AP could not have approved the charge when the invoice arrived. A check made as of
    the invoice date would wrongly call these unauthorized.
    """
    acc = rerated["accessorials"]
    status = acc["auth_status"].value_counts()
    return pd.DataFrame([
        ("accessorial_lines_audited", len(acc)),
        ("accessorials_authorized_by_invoice_date", int(status.get("authorized", 0))),
        ("accessorials_authorized_after_invoice_date", int(status.get("authorized_late", 0))),
        ("accessorials_unauthorized", int(status.get("unauthorized", 0)))],
        columns=["metric", "value"])


def invoice_summary(norm, flags, recoverable):
    """One row per audited (non-superseded) invoice, flagged or not."""
    inv = norm["invoices"]
    inv = inv[~inv["is_superseded"]][["invoice_id", "carrier_id", "invoice_number", "invoice_type",
                                      "match_method", "shipment_id", "total"]].copy()
    by_invoice = flags.groupby("invoice_id")
    inv["n_flags"] = inv["invoice_id"].map(by_invoice.size()).fillna(0).astype(int)
    inv["error_types"] = inv["invoice_id"].map(by_invoice["error_type"].agg(lambda s: ";".join(sorted(set(s))))).fillna("")
    inv["recoverable_estimate"] = inv["invoice_id"].map(recoverable).fillna(0.0)
    return inv.reset_index(drop=True)


def audit(norm, rerated, ref, cfg):
    """Run all rules. Returns {"flags", "invoice_summary", "process_metrics"}."""
    order = {rule.__name__: i for i, rule in enumerate(RULES)}
    flags = pd.concat([rule(norm, rerated, ref, cfg) for rule in RULES], ignore_index=True)
    flags = (flags.assign(_rule=flags["error_type"].map(order)).sort_values(["_rule", "invoice_id"])
             .drop(columns="_rule").reset_index(drop=True))
    flags, recoverable = apply_recoverable(flags, norm["invoices"])
    return {"flags": flags, "invoice_summary": invoice_summary(norm, flags, recoverable),
            "process_metrics": process_metrics(rerated)}


def write_audit(result, out_dir=OUTPUT_DIR):
    """Write outputs/audit_flags.csv, audit_invoice_summary.csv, audit_process_metrics.csv."""
    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(result["flags"], out_dir / "audit_flags.csv")
    write_csv(result["invoice_summary"], out_dir / "audit_invoice_summary.csv")
    write_csv(result["process_metrics"], out_dir / "audit_process_metrics.csv")
