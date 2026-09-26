"""Run the pipeline end to end: python -m freight_audit_lab.run

Stages 1-2: generate reference data, invoices, raw carrier files, and the answer key. Later
stages append normalize, rerate, audit, and so on.
"""

import time

from freight_audit_lab.config import load_config
from freight_audit_lab.contract import (contract_linehaul, diesel_for_ship_date, fsc_ltl_amount,
                                        fsc_tl_amount, lookup_rate)
from freight_audit_lab.generate import generate_all


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


if __name__ == "__main__":
    main()
