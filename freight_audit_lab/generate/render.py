"""Render invoices as messy carrier files in four layouts (A-D).

Real carriers each send their own format, so the audit has to normalize them. Every cell is
written as text exactly as the carrier's system would print it. Nothing from ground truth
appears here (no labels, modes, impacts, or true shipment ids). The AP receipt date is not in
the carrier files either; it goes to data/reference/ap_receipt_log.csv.
"""

import shutil
from pathlib import Path

import pandas as pd

HEADERS = {
    "A": ["Invoice No", "Pro #", "BOL", "EDI Ctrl #", "Inv Date", "Ship Date", "Origin", "Dest",
          "Weight (lbs)", "Linehaul", "Fuel Surcharge", "Acc1 Code", "Acc1 Amt", "Acc2 Code",
          "Acc2 Amt", "Total"],
    "B": ["invoice_number", "pro_number", "bill_of_lading", "doc_id", "invoice_date", "ship_date",
          "origin_city", "dest_city", "weight_cwt", "charge_code", "amount", "invoice_type",
          "supersedes", "invoice_total"],
    "C": ["Carrier", "Invoice #", "PRO Number", "BOL Number", "Transmission ID", "Invoice Date",
          "Ship Date", "Origin", "Destination", "Weight", "Linehaul", "FSC", "Acc 1", "Acc 1 Amount",
          "Acc 2", "Acc 2 Amount", "Total Due"],
    "D": ["Inv Number", "Pro Number", "BOL", "Control No", "Inv Date", "Ship Date", "Orig", "Dest",
          "Weight Lbs", "Charge Description", "Amount", "Type", "Orig Invoice Ref", "Invoice Total"],
}
MAX_ACCESSORIAL_SLOTS = 2
PRO_SUFFIX = {"A": {"rebill": "-C", "balance_due": "-BD"}, "C": {"rebill": "R", "balance_due": "B"}}
TYPE_TEXT = {"B": {"original": "original", "rebill": "rebill", "balance_due": "balance_due"},
             "D": {"original": "ORIGINAL", "rebill": "REBILL", "balance_due": "BAL DUE"}}


def city_text(city, state, fmt, rng):
    """City as each carrier prints it. Format D is deliberately inconsistent in case and
    punctuation, but always recoverable by lowercasing and dropping punctuation."""
    if fmt == "A":
        return f"{city}, {state}"
    if fmt == "B":
        return city
    if fmt == "C":
        return f"{city.upper()} {state}"
    styles = [f"{city}, {state}", f"{city.upper()} {state}", f"{city.lower()},{state.lower()}",
              f"{city.replace('.', '')} {state}", f"{city.upper()}, {state}.", f"{city}  {state}"]
    return styles[int(rng.integers(len(styles)))]


def date_text(ts, fmt):
    return ts.strftime({"A": "%m/%d/%Y", "B": "%Y-%m-%d", "C": "%Y-%m-%d 00:00:00", "D": "%d-%b-%y"}[fmt])


def amount_text(x, fmt):
    return f"${x:,.2f}" if fmt == "C" else f"{x:.2f}"


def weight_text(lbs, fmt):
    return {"A": str(lbs), "B": f"{lbs / 100:.2f}", "C": f"{lbs:,} LB", "D": str(lbs)}[fmt]


def bol_text(inv, fmt):
    """The BOL field. Format D always prefixes `BOL#` unless noise already dressed it."""
    raw = inv["bol_raw"]
    return "BOL#" + raw if fmt == "D" and raw[:3].lower() != "bol" else raw


def wide_row(inv, fmt, carrier, rng):
    """One row per invoice (formats A and C). Charge lines beyond LH/FSC go in accessorial slots."""
    acc = [ln for ln in inv["lines"] if ln[0] not in ("LH", "FSC")]
    assert len(acc) <= MAX_ACCESSORIAL_SLOTS, "more accessorials than the wide layout has slots"
    lh = next((a for c, a in inv["lines"] if c == "LH"), None)
    fsc = next((a for c, a in inv["lines"] if c == "FSC"), None)
    money = lambda x: "" if x is None else amount_text(x, fmt)
    slots = [(c, money(a)) for c, a in acc] + [("", "")] * (MAX_ACCESSORIAL_SLOTS - len(acc))
    pro = inv["pro_base"] + PRO_SUFFIX[fmt].get(inv["invoice_type"], "")
    values = [inv["invoice_number"], pro, bol_text(inv, fmt), inv["control_id"],
              date_text(inv["invoice_date"], fmt), date_text(inv["ship_date"], fmt),
              city_text(inv["origin_city"], inv["origin_state"], fmt, rng),
              city_text(inv["dest_city"], inv["dest_state"], fmt, rng),
              weight_text(inv["weight_lbs"], fmt), money(lh), money(fsc),
              slots[0][0], slots[0][1], slots[1][0], slots[1][1],
              amount_text(round(sum(a for _, a in inv["lines"]), 2), fmt)]
    if fmt == "C":
        variant = carrier["name_variants"][int(rng.integers(len(carrier["name_variants"])))]
        values = [variant] + values
    return [dict(zip(HEADERS[fmt], values))]


def long_rows(inv, fmt, describe, number_of, rng):
    """One row per charge line (formats B and D).

    Long exports usually repeat the invoice-level total on every line, so this does too.
    """
    supersedes = number_of[inv["parent_uid"]] if inv["invoice_type"] == "rebill" else ""
    origin = city_text(inv["origin_city"], inv["origin_state"], fmt, rng)
    dest = city_text(inv["dest_city"], inv["dest_state"], fmt, rng)
    lines = inv["lines"] + inv["unknown_lines"]
    invoice_total = amount_text(round(sum(a for _, a in lines), 2), fmt)
    rows = []
    for code, amount in lines:
        common = [inv["invoice_number"], inv["pro_base"], bol_text(inv, fmt), inv["control_id"],
                  date_text(inv["invoice_date"], fmt), date_text(inv["ship_date"], fmt),
                  origin, dest, weight_text(inv["weight_lbs"], fmt)]
        label = describe(code)
        values = common + [label, amount_text(amount, fmt), TYPE_TEXT[fmt][inv["invoice_type"]],
                           supersedes, invoice_total]
        rows.append(dict(zip(HEADERS[fmt], values)))
    return rows


def render_invoices(invoices, cfg, rng):
    """{(carrier_id, 'YYYY-MM' of received date): DataFrame of text} for every raw file."""
    carriers = {c["id"]: c for c in cfg["carriers"]}
    code_to_text = {v: k for k, v in cfg["normalization"]["charge_code_map"].items()}
    number_of = {inv["uid"]: inv["invoice_number"] for inv in invoices}
    files = {}
    for inv in sorted(invoices, key=lambda x: (x["received_date"], x["control_id"])):
        carrier = carriers[inv["carrier_id"]]
        fmt = carrier["format"]
        if fmt in ("A", "C"):
            rows = wide_row(inv, fmt, carrier, rng)
        elif fmt == "B":
            rows = long_rows(inv, fmt, lambda code: code, number_of, rng)
        else:
            rows = long_rows(inv, fmt, lambda code: code_to_text.get(code, code), number_of, rng)
        files.setdefault((inv["carrier_id"], inv["received_date"].strftime("%Y-%m")), []).extend(rows)
    return {key: pd.DataFrame(rows, columns=HEADERS[carriers[key[0]]["format"]])
            for key, rows in files.items()}


def write_raw_files(files, data_dir):
    """Write data/raw/invoices/<carrier>/<YYYY-MM>.csv, replacing anything from an earlier run."""
    root = Path(data_dir) / "raw" / "invoices"
    if root.exists():
        shutil.rmtree(root)
    for (carrier_id, month), df in sorted(files.items()):
        path = root / carrier_id / f"{month}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(path, index=False, lineterminator="\n")
