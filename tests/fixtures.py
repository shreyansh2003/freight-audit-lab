"""A tiny hand-priced world for testing the re-rater and audit rules (no generator involved).

Prices are round numbers so every expected value can be checked by hand:

  LTL lane L1 (CARA, Chicago -> Atlanta, 500 mi), rate v1 to 2025-06-30, v2 (10% lower) from 2025-07-01:
      $/cwt by weight break 0-499 / 500-999 / 1000-1999 / 2000-4999 / 5000-7500
      v1: 40 / 30 / 20 / 15 / 12       v2: 36 / 27 / 18 / 13.5 / 10.8       minimum charge $100
  LTL lane L3 (CARA) is amended *upward* 10% on 2025-07-01 (v2 = 44 / 33 / 22 / 16.5 / 13.2)
  TL lane L2 (CARF, Chicago -> Dallas, 600 mi): $2.50/mile, minimum $450
  Diesel is $2.20 every week: LTL FSC = (2.20 - 1.20) / 0.10 = 10 steps x 0.9% = 9.0% of linehaul,
  TL FSC = (2.20 - 1.25) / 6.0 = $0.1583/mile x 600 mi = $95.00.

  Shipment  mode  weight  ship date    clean linehaul   clean FSC
  S1        LTL   1,000   2025-03-10   $200.00 (L1 v1)  $18.00
  S2        LTL   1,000   2025-08-04   $180.00 (L1 v2)  $16.20     shipped after the amendment
  S3        LTL   1,000   2025-03-10   certified at 1,100 lb: $220.00, $19.80
  S4        TL    30,000  2025-03-10   $1,500.00        $95.00
  S5        LTL   1,000   2025-03-10   liftgate authorized 2025-03-25, after the invoice date
  S6        LTL   5,000   2025-03-10   $600.00          $54.00     big enough for percentage tolerances
  S7        LTL   1,000   2025-08-04   $220.00 (L3 v2)  $19.80     lane amended upward
"""

import copy

import pandas as pd

from freight_audit_lab.audit.baseline import run_baseline
from freight_audit_lab.audit.engine import audit
from freight_audit_lab.config import load_config
from freight_audit_lab.csv_io import load_reference
from freight_audit_lab.normalize import normalize
from freight_audit_lab.rerate import rerate

TS = pd.Timestamp
BREAKS = ["cwt_0_499", "cwt_500_999", "cwt_1000_1999", "cwt_2000_4999", "cwt_5000_7500"]

# shipment_id: (bol, carrier, lane, mode, weight, ship date, clean LH, clean FSC)
SHIPMENTS = {
    "S1": ("00000001", "CARA", "L1", "LTL", 1000, "2025-03-10", 200.00, 18.00),
    "S2": ("00000002", "CARA", "L1", "LTL", 1000, "2025-08-04", 180.00, 16.20),
    "S3": ("00000003", "CARA", "L1", "LTL", 1000, "2025-03-10", 220.00, 19.80),
    "S4": ("00000004", "CARF", "L2", "TL", 30000, "2025-03-10", 1500.00, 95.00),
    "S5": ("00000005", "CARA", "L1", "LTL", 1000, "2025-03-10", 200.00, 18.00),
    "S6": ("00000006", "CARA", "L1", "LTL", 5000, "2025-03-10", 600.00, 54.00),
    "S7": ("00000007", "CARA", "L3", "LTL", 1000, "2025-08-04", 220.00, 19.80),
}
CERTIFIED_WEIGHT = {"S3": 1100}
DEFAULT_AUTHS = [("S1", "RESIDENTIAL", 125.0, "2025-03-05"),     # a different code than the one billed
                 ("S5", "LIFTGATE", 95.0, "2025-03-25")]         # recorded after the invoice date


def make_cfg(**tolerances):
    """config.yaml with some audit tolerances replaced, e.g. make_cfg(rate_pct=0.02)."""
    cfg = copy.deepcopy(load_config())
    cfg["audit"]["tolerances"].update(tolerances)
    return cfg


def rate_row(carrier, lane, mode, version, start, end, per_cwt=None, per_mile=None, min_charge=100.0):
    row = {"carrier_id": carrier, "lane_id": lane, "mode": mode, "version": version,
           "effective_from": TS(start), "effective_to": TS(end), "min_charge": min_charge,
           "rate_per_mile": per_mile, "deficit_weight_rating": False}
    row.update(dict(zip(BREAKS, per_cwt or [None] * 5)))
    return row


def make_ref(extra_auths=()):
    """The reference tables the re-rater reads. `extra_auths`: more (shipment, code, amount, date)."""
    shipments = pd.DataFrame([
        {"shipment_id": sid, "bol": bol, "ship_date": TS(day), "lane_id": lane, "mode": mode,
         "miles": 600 if lane == "L2" else 500, "carrier_id": carrier, "weight_lbs": weight}
        for sid, (bol, carrier, lane, mode, weight, day, _, _) in SHIPMENTS.items()])
    lanes = pd.DataFrame([
        {"lane_id": "L1", "origin_city": "Chicago", "destination_city": "Atlanta", "miles": 500},
        {"lane_id": "L2", "origin_city": "Chicago", "destination_city": "Dallas", "miles": 600},
        {"lane_id": "L3", "origin_city": "Chicago", "destination_city": "Memphis", "miles": 500}])
    v1, v2 = [40, 30, 20, 15, 12], [36, 27, 18, 13.5, 10.8]
    rate_card = pd.DataFrame([
        rate_row("CARA", "L1", "LTL", 1, "2025-01-01", "2025-06-30", v1),
        rate_row("CARA", "L1", "LTL", 2, "2025-07-01", "2099-12-31", v2),
        rate_row("CARA", "L3", "LTL", 1, "2025-01-01", "2025-06-30", v1),
        rate_row("CARA", "L3", "LTL", 2, "2025-07-01", "2099-12-31", [x * 1.1 for x in v1]),
        rate_row("CARF", "L2", "TL", 1, "2025-01-01", "2099-12-31", per_mile=2.5, min_charge=450.0)])
    diesel = pd.DataFrame({"week_start": pd.date_range("2025-01-06", "2025-12-29", freq="W-MON"),
                           "price_per_gallon": 2.20})
    certs = pd.DataFrame([{"certificate_id": f"RW{i}", "shipment_id": sid, "certified_weight_lbs": w,
                           "certified_at": TS("2025-03-12")} for i, (sid, w) in enumerate(CERTIFIED_WEIGHT.items())])
    auths = pd.DataFrame([{"auth_id": f"A{i}", "shipment_id": sid, "code": code, "authorized_amount": amount,
                           "authorized_at": TS(day)}
                          for i, (sid, code, amount, day) in enumerate(list(DEFAULT_AUTHS) + list(extra_auths))],
                         columns=["auth_id", "shipment_id", "code", "authorized_amount", "authorized_at"])
    return {"shipments": shipments, "lanes": lanes, "rate_card": rate_card, "diesel_weekly": diesel,
            "reweigh_certificates": certs, "authorizations": auths}


def invoice(iid, shipment="S1", *, lh="clean", fsc="clean", acc=(), weight="clean", itype="original",
            invoice_date="2025-03-15", received="2025-03-18", number=None, superseded=False, match="exact",
            bol=None, carrier=None):
    """One normalized invoice as a spec dict. lh/fsc default to the clean contract amounts of the
    shipment; pass None to leave the line off (balance-due invoices); `acc` is [(code, amount)]."""
    bol_text, ship_carrier, _, _, ship_weight, day, clean_lh, clean_fsc = SHIPMENTS[shipment]
    return {"invoice_id": f"{carrier or ship_carrier}:{iid}", "carrier_id": carrier or ship_carrier,
            "invoice_number": number or f"N{iid}", "pro_number": f"PRO{iid}", "bol_raw": bol or bol_text,
            "bol_canonical": bol_text, "invoice_type": itype, "supersedes_invoice_number": "",
            "invoice_date": TS(invoice_date), "received_date": TS(received), "ship_date": TS(day),
            "origin": "Chicago", "destination": "Atlanta",
            "billed_weight_lbs": ship_weight if weight == "clean" else weight,
            "is_superseded": superseded, "shipment_id": "" if match == "unmatched" else shipment,
            "match_method": match,
            "_lh": clean_lh if lh == "clean" else lh, "_fsc": clean_fsc if fsc == "clean" else fsc,
            "_acc": list(acc)}


def build_norm(specs):
    """(normalized invoices, invoice lines) frames from invoice specs."""
    lines, rows = [], []
    for spec in specs:
        lines_here = ([("LH", spec["_lh"])] if spec["_lh"] is not None else []) \
            + ([("FSC", spec["_fsc"])] if spec["_fsc"] is not None else []) + list(spec["_acc"])
        lines += [{"invoice_id": spec["invoice_id"], "charge_code": code, "amount": amount}
                  for code, amount in lines_here]
        total = round(sum(amount for _, amount in lines_here), 2)
        rows.append({**{k: v for k, v in spec.items() if not k.startswith("_")}, "total": total,
                     "lines_total": total, "totals_ok": True, "source_file": "raw/test.csv"})
    inv = pd.DataFrame(rows)
    inv["billed_weight_lbs"] = inv["billed_weight_lbs"].astype("Int64")
    return {"invoices": inv, "invoice_lines": pd.DataFrame(lines, columns=["invoice_id", "charge_code", "amount"])}


def audit_inputs(specs, cfg=None, extra_auths=()):
    """(norm, rerated, ref, cfg): the arguments every rule takes."""
    cfg = cfg or make_cfg()
    ref = make_ref(extra_auths)
    norm = build_norm(specs)
    return norm, rerate(norm, ref, cfg), ref, cfg


def run_pipeline(data_dir, cfg):
    """normalize -> rerate -> audit -> baseline on a generated data directory. Returns every result frame."""
    norm = normalize(cfg, data_dir)
    ref = load_reference(data_dir)
    rerated = rerate(norm, ref, cfg)
    return {"invoices": norm["invoices"], "invoice_lines": norm["invoice_lines"],
            "rerated_invoices": rerated["invoices"], "rerated_accessorials": rerated["accessorials"],
            **{f"audit_{k}": v for k, v in audit(norm, rerated, ref, cfg).items()},
            "baseline_flags": run_baseline(norm, ref, cfg)["flags"]}
