"""Run the pipeline end to end: python -m freight_audit_lab.run

Stages 1-2: generate reference data, invoices, raw carrier files, and the answer key.
Stage 3: normalize the raw files and match invoices to shipments.
Stage 4: re-rate against the contract, audit with the rules, run the naive baseline.
Stage 5: score against the answer key, sweep tolerances, build the exception queue and disputes.
Stage 6: month-end accruals, journal entries, and accrual accuracy.
"""

import time

from freight_audit_lab.accruals import accuracy_summary, run_accruals, write_accruals
from freight_audit_lab.audit.baseline import run_baseline, write_baseline
from freight_audit_lab.audit.engine import audit, write_audit
from freight_audit_lab.audit.rules import RULES
from freight_audit_lab.config import REPO_ROOT, load_config
from freight_audit_lab.evaluate import evaluate, load_labels, write_evaluation
from freight_audit_lab.exceptions import build_disputes, build_exception_queue, write_exceptions
from freight_audit_lab.sweep import run_sweep, write_sweep
from freight_audit_lab.contract import (contract_linehaul, diesel_for_ship_date, fsc_ltl_amount,
                                        fsc_tl_amount, lookup_rate)
from freight_audit_lab.csv_io import load_reference
from freight_audit_lab.generate import generate_all
from freight_audit_lab.normalize import normalize, write_normalized
from freight_audit_lab.rerate import rerate


def contract_spend_check(tables, cfg):
    """Contract linehaul + FSC over all shipments (no accessorials), as a sanity check on
    the rate assumptions. An estimate of spend, not an invoice total."""
    total = {"LTL": 0.0, "TL": 0.0}
    for shp in tables["shipments"].itertuples(index=False):
        row = lookup_rate(tables["rate_card"], shp.carrier_id, shp.lane_id, shp.ship_date)
        lh = contract_linehaul(row, shp.weight_lbs, shp.miles)
        diesel = diesel_for_ship_date(tables["diesel_weekly"], shp.ship_date)
        fsc = (fsc_ltl_amount(lh, diesel, cfg) if shp.mode == "LTL"
               else fsc_tl_amount(shp.miles, diesel, cfg))
        total[shp.mode] += lh + fsc
    return total


def main():
    cfg = load_config()
    t0 = time.perf_counter()
    tables = generate_all(cfg)
    print(f"generate: {time.perf_counter() - t0:.1f}s")
    for name, df in tables.items():
        if name != "raw_files":
            print(f"  {name:<22} {len(df):>6} rows")
    print(f"  {'raw carrier files':<22} {len(tables['raw_files']):>6} files")
    spend = contract_spend_check(tables, cfg)
    print(f"contract LH+FSC estimate: LTL ${spend['LTL']:,.0f}  TL ${spend['TL']:,.0f}  "
          f"total ${sum(spend.values()):,.0f}")

    t0 = time.perf_counter()
    normalized = normalize(cfg)
    write_normalized(normalized)
    inv = normalized["invoices"]
    print(f"normalize: {time.perf_counter() - t0:.1f}s")
    for name, df in normalized.items():
        print(f"  {name:<26} {len(df):>6} rows")
    print(f"  match_method: {inv['match_method'].value_counts().to_dict()}  "
          f"totals_ok: {int(inv['totals_ok'].sum())}/{len(inv)}  superseded: {int(inv['is_superseded'].sum())}")

    t0 = time.perf_counter()
    ref = load_reference(REPO_ROOT / "data")
    rerated = rerate(normalized, ref, cfg)
    print(f"rerate: {time.perf_counter() - t0:.1f}s  ({len(rerated['invoices'])} invoices, "
          f"{len(rerated['accessorials'])} accessorial lines)")

    t0 = time.perf_counter()
    engine = audit(normalized, rerated, ref, cfg)
    baseline = run_baseline(normalized, ref, cfg)
    write_audit(engine)
    write_baseline(baseline)
    print(f"audit + baseline: {time.perf_counter() - t0:.1f}s")
    print_flag_comparison(engine, baseline)

    t0 = time.perf_counter()
    labels = load_labels(REPO_ROOT / "data")
    evaluation = evaluate(engine["flags"], baseline["flags"], labels)
    write_evaluation(evaluation)
    swept = run_sweep(normalized, rerated, ref, cfg, labels)
    write_sweep(swept)
    print(f"evaluate + sweep: {time.perf_counter() - t0:.1f}s")
    overall = evaluation["engine_vs_baseline"].set_index("error_type").loc["ALL"]
    print(f"  engine   precision {overall['engine_precision']:.1%}  recall {overall['engine_recall']:.1%}   "
          f"baseline precision {overall['baseline_precision']:.1%}  recall {overall['baseline_recall']:.1%}")
    for name, rec in swept["recommended"].items():
        print(f"  sweep {name:<11} current {rec['current']:g} -> recommended {rec['recommended']:g}")

    t0 = time.perf_counter()
    queue = build_exception_queue(normalized, engine["flags"], engine["invoice_summary"], cfg)
    disputes, findings = build_disputes(normalized, engine["flags"], queue, cfg)
    write_exceptions(queue, disputes, findings)
    print(f"exceptions: {time.perf_counter() - t0:.1f}s  ({len(queue)} open exceptions, "
          f"{len(findings)} systemic findings, {len(disputes)} dispute packs)")

    t0 = time.perf_counter()
    accruals = run_accruals(normalized, ref, cfg, engine)
    write_accruals(accruals)
    total = accruals["accuracy"].iloc[-1]
    headline = accuracy_summary(accruals["accuracy"])
    print(f"accruals: {time.perf_counter() - t0:.1f}s  ({int(total['shipments_accrued'])} shipment-month accruals, "
          f"{len(accruals['journal_entries'])} journal lines)")
    print(f"  accrual vs payable (estimates): error {total['error_pct_estimate']:+.2%} overall, "
          f"MAPE {headline['mape_pct_estimate']:.2%}, bias {headline['bias_pct_estimate']:+.2%}")


def print_flag_comparison(engine, baseline):
    """Flag counts by error type, engine vs baseline, and total recoverable estimate for each."""
    eng, base = engine["flags"], baseline["flags"]
    print(f"  {'error type':<26}{'engine':>8}{'baseline':>10}")
    for rule in RULES:
        name = rule.__name__
        print(f"  {name:<26}{(eng['error_type'] == name).sum():>8}{(base['error_type'] == name).sum():>10}")
    print(f"  {'total flags':<26}{len(eng):>8}{len(base):>10}")
    print(f"  recoverable estimate: engine ${engine['invoice_summary']['recoverable_estimate'].sum():,.0f}  "
          f"baseline ${baseline['recoverable'].sum():,.0f}")


if __name__ == "__main__":
    main()
