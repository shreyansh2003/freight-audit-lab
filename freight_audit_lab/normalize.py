"""Normalization and matching: four messy carrier layouts in, one clean invoice table out.

Every carrier sends its own file layout. This module detects the layout from the header,
parses each one into the same two tables (`invoices`, `invoice_lines`), counts every fix it
had to make, dates each invoice from the AP receipt log, works out which invoices a rebill
supersedes, and matches each invoice to a shipment. Nothing here reads the answer key, and
nothing here knows how the files were generated: the layouts below are what an AP team
learns about each carrier's format.

A row that cannot be parsed (bad date, weight, carrier, or invoice type) is dropped and
logged to `normalization_exceptions`. A bad field that does not stop the invoice from being
audited (BOL, city, amount, receipt date, totals) is logged and the invoice is kept.
"""

import re
from collections import Counter
from pathlib import Path

import pandas as pd

from freight_audit_lab.config import REPO_ROOT
from freight_audit_lab.csv_io import read_csv, read_raw_csv, write_csv

BOL_LENGTH = 8
ISO_DATE = "%Y-%m-%d"
MONEY_SLACK = 1e-9        # floating-point guard so a difference of exactly the tolerance passes

# Each layout: the raw column behind every field, how its dates and weights are written, and how
# it says an invoice is a rebill or balance-due (a pro-number suffix, or an explicit type column).
LAYOUTS = {
    "A": dict(
        shape="wide", date_format="%m/%d/%Y", weight_unit="lbs",
        pro_suffix_types={"": "original", "-C": "rebill", "-BD": "balance_due"},
        columns=dict(invoice_number="Invoice No", pro_number="Pro #", bol="BOL", control_id="EDI Ctrl #",
                     invoice_date="Inv Date", ship_date="Ship Date", origin="Origin", destination="Dest",
                     weight="Weight (lbs)", total="Total"),
        charges={"LH": "Linehaul", "FSC": "Fuel Surcharge"},
        slots=[("Acc1 Code", "Acc1 Amt"), ("Acc2 Code", "Acc2 Amt")]),
    "B": dict(
        shape="long", date_format="%Y-%m-%d", weight_unit="cwt",
        type_map={"original": "original", "rebill": "rebill", "balance_due": "balance_due"},
        columns=dict(invoice_number="invoice_number", pro_number="pro_number", bol="bill_of_lading",
                     control_id="doc_id", invoice_date="invoice_date", ship_date="ship_date",
                     origin="origin_city", destination="dest_city", weight="weight_cwt",
                     charge="charge_code", amount="amount", type="invoice_type",
                     supersedes="supersedes", total="invoice_total")),
    "C": dict(
        shape="wide", date_format="%Y-%m-%d %H:%M:%S", weight_unit="lbs",
        pro_suffix_types={"": "original", "R": "rebill", "B": "balance_due"},
        columns=dict(carrier="Carrier", invoice_number="Invoice #", pro_number="PRO Number",
                     bol="BOL Number", control_id="Transmission ID", invoice_date="Invoice Date",
                     ship_date="Ship Date", origin="Origin", destination="Destination",
                     weight="Weight", total="Total Due"),
        charges={"LH": "Linehaul", "FSC": "FSC"},
        slots=[("Acc 1", "Acc 1 Amount"), ("Acc 2", "Acc 2 Amount")]),
    "D": dict(
        shape="long", date_format="%d-%b-%y", weight_unit="lbs",
        type_map={"ORIGINAL": "original", "REBILL": "rebill", "BAL DUE": "balance_due"},
        columns=dict(invoice_number="Inv Number", pro_number="Pro Number", bol="BOL",
                     control_id="Control No", invoice_date="Inv Date", ship_date="Ship Date",
                     origin="Orig", destination="Dest", weight="Weight Lbs",
                     charge="Charge Description", amount="Amount", type="Type",
                     supersedes="Orig Invoice Ref", total="Invoice Total")),
}

INVOICE_COLUMNS = ["invoice_id", "carrier_id", "invoice_number", "pro_number", "bol_raw", "bol_canonical",
                   "invoice_type", "supersedes_invoice_number", "invoice_date", "received_date", "ship_date",
                   "origin", "destination", "billed_weight_lbs", "total", "source_file",
                   "is_superseded", "shipment_id", "match_method", "lines_total", "totals_ok"]
EXCEPTION_COLUMNS = ["source_file", "csv_row", "carrier_id", "control_id", "exception_type", "raw_value", "detail"]


# ------------------------------------------------------------ layouts and field parsers


def layout_headers(spec):
    """Every raw column a layout uses: its header signature."""
    cols = set(spec["columns"].values()) | set(spec.get("charges", {}).values())
    for code_col, amount_col in spec.get("slots", []):
        cols |= {code_col, amount_col}
    return cols


def detect_layout(header):
    """The layout whose header signature matches exactly, else None. The folder name is never used."""
    for name, spec in LAYOUTS.items():
        if set(header) == layout_headers(spec):
            return name
    return None


def canonical_bol(text):
    """The BOL as its 8 digits: strip everything that is not a digit, then left-pad with zeros.

    Carriers dress the same BOL as `BOL#0048...`, `0048-2913`, or drop its leading zeros, so
    only the digits carry information. Returns None when there are no digits or too many.
    """
    digits = re.sub(r"\D", "", text)
    return digits.zfill(BOL_LENGTH) if 0 < len(digits) <= BOL_LENGTH else None


def city_key(text):
    """Lowercase letters and digits only, single-spaced: 'St. Louis, MO.' -> 'st louis mo'."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def clean_city(text, lookup):
    """Canonical city name for a carrier's city text, or None if it is not a known city.

    Carriers print 'CHICAGO IL', 'chicago,il', 'Chicago, IL.' or plain 'Chicago' (no state).
    Try the whole cleaned text first, then again without a trailing two-letter state.
    """
    key = city_key(text)
    if key in lookup:
        return lookup[key]
    head, _, last = key.rpartition(" ")
    return lookup.get(head) if len(last) == 2 else None


def parse_dates(raw, date_format):
    return pd.to_datetime(raw, format=date_format, errors="coerce")


def parse_money(raw):
    """'$1,234.56' or '1234.56' -> 1234.56; blank or junk -> NaN."""
    return pd.to_numeric(raw.str.replace(r"[$,\s]", "", regex=True), errors="coerce")


def parse_weight(raw, unit):
    """Whole pounds from '1,250 LB', '1250', or hundredweight '12.50' (cwt x 100); junk -> <NA>."""
    number = pd.to_numeric(raw.str.replace(",", "").str.replace(r"\s*lbs?$", "", case=False, regex=True),
                           errors="coerce")
    number = (number * 100 if unit == "cwt" else number).round()
    return number.where(number > 0).astype("Int64")


class Recorder:
    """Collects fix counts and exceptions while the files are parsed."""

    def __init__(self):
        self.fixes = Counter()
        self.rows = []

    def fix(self, carriers, changed, name, unit):
        """Count `changed` rows per carrier as one kind of fix."""
        for carrier, n in carriers[changed].value_counts().items():
            self.fixes[(carrier, name, unit)] += int(n)

    def add(self, source_file, csv_row, carrier_id, control_id, kind, raw_value="", detail=""):
        self.rows.append(dict(source_file=source_file, csv_row=csv_row, carrier_id=carrier_id,
                              control_id=control_id, exception_type=kind, raw_value=raw_value, detail=detail))

    def problem(self, frame, kind, value_col=None, detail=""):
        """One exception per row of `frame` (which carries _source, _row, carrier_id, control_id)."""
        details = detail if isinstance(detail, pd.Series) else pd.Series(detail, index=frame.index)
        for i, r in frame.iterrows():
            self.add(r["_source"], r["_row"], r["carrier_id"], r["control_id"], kind,
                     r[value_col] if value_col else "", details[i])

    def report(self):
        rows = [dict(carrier_id=c, fix_type=f, unit=u, count=n) for (c, f, u), n in sorted(self.fixes.items())]
        return pd.DataFrame(rows, columns=["carrier_id", "fix_type", "unit", "count"])

    def exceptions(self):
        df = pd.DataFrame(self.rows, columns=EXCEPTION_COLUMNS)
        return df.sort_values(["source_file", "csv_row", "exception_type"], kind="stable").reset_index(drop=True)


# ------------------------------------------------------------ one raw file


def raw_lines(df, spec):
    """Charge lines as (charge_raw, amount_raw) rows: one per row in long layouts, and the
    LH/FSC columns plus the accessorial slots unpivoted in wide layouts."""
    keys = ["carrier_id", "control_id", "_source", "_row"]
    if spec["shape"] == "long":
        return df[keys].assign(charge_raw=df["charge"], amount_raw=df["amount"])
    parts = []
    for code, col in spec["charges"].items():
        part = df[keys].assign(charge_raw=code, amount_raw=df[col])
        parts.append(part[part["amount_raw"] != ""])
    for code_col, amount_col in spec["slots"]:
        part = df[keys].assign(charge_raw=df[code_col], amount_raw=df[amount_col])
        parts.append(part[(part["charge_raw"] != "") | (part["amount_raw"] != "")])
    return pd.concat(parts)


def parse_file(raw, layout, folder_carrier, source, cfg, city_lookup, rec):
    """Parse one raw file into (invoice header rows, charge line rows)."""
    spec = LAYOUTS[layout]
    df = raw.rename(columns={col: field for field, col in spec["columns"].items()})
    df["_source"], df["_row"] = source, raw.index + 2          # +2: header is line 1
    names = {c["id"]: c["name"] for c in cfg["carriers"]}
    variants = {v: c["id"] for c in cfg["carriers"] for v in [c["name"], *c["name_variants"]]}
    df["carrier_id"] = df["carrier"].map(variants) if "carrier" in df else folder_carrier
    if "carrier" in df:
        df["carrier_id"] = df["carrier_id"].fillna("")

    hdr = df if spec["shape"] == "wide" else df.drop_duplicates("control_id")
    if spec["shape"] == "long":     # every line repeats the invoice total; they must agree
        disagree = df.groupby("control_id")["total"].nunique()
        rec.problem(hdr[hdr["control_id"].isin(disagree[disagree > 1].index)], "inconsistent_total", "total",
                    "stated invoice total differs between charge lines; first line used")
    hdr = hdr.copy()
    c = hdr["carrier_id"]

    # header fields
    invoice_date = parse_dates(hdr["invoice_date"], spec["date_format"])
    ship_date = parse_dates(hdr["ship_date"], spec["date_format"])
    for col, parsed in (("invoice_date", invoice_date), ("ship_date", ship_date)):
        rec.fix(c, parsed.notna() & (hdr[col] != parsed.dt.strftime(ISO_DATE)), "date_parsing", "date field")
    weight = parse_weight(hdr["weight"], spec["weight_unit"])
    rec.fix(c, weight.notna() & (hdr["weight"] != weight.astype(str)),
            "cwt_to_lbs" if spec["weight_unit"] == "cwt" else "weight_text_cleanup", "invoice")
    if "carrier" in hdr:
        rec.fix(c, (c != "") & (hdr["carrier"] != c.map(names)), "carrier_name_variant", "invoice")
    bol = hdr["bol"].map(canonical_bol)
    rec.fix(c, bol.notna() & (hdr["bol"] != bol), "bol_canonicalization", "invoice")
    origin, destination = hdr["origin"].map(lambda t: clean_city(t, city_lookup)), \
        hdr["destination"].map(lambda t: clean_city(t, city_lookup))
    rec.fix(c, origin.notna() & (hdr["origin"] != origin), "city_cleanup", "city field")
    rec.fix(c, destination.notna() & (hdr["destination"] != destination), "city_cleanup", "city field")

    if "pro_suffix_types" in spec:      # A and C: the pro number's suffix is the only type marker
        parts = hdr["pro_number"].str.extract(r"^(\d+)(\D.*)?$")
        pro, suffix = parts[0], parts[1].fillna("")
        invoice_type = suffix.map(spec["pro_suffix_types"])
        rec.fix(c, invoice_type.notna() & (suffix != ""), "invoice_type_from_pro_suffix", "invoice")
        supersedes = pd.Series("", index=hdr.index)
    else:                                # B and D: an explicit type column, and the rebilled invoice number
        pro = hdr["pro_number"]
        invoice_type = hdr["type"].map(spec["type_map"])
        rec.fix(c, invoice_type.notna() & (hdr["type"] != invoice_type), "invoice_type_text", "invoice")
        supersedes = hdr["supersedes"]
    total = parse_money(hdr["total"])
    rec.fix(c, hdr["total"].str.contains(r"[$,]"), "currency_to_float", "amount field")

    # what stops an invoice from being audited at all is dropped; smaller problems are logged and kept
    fatal = (invoice_date.isna() | ship_date.isna() | weight.isna() | invoice_type.isna() | pro.isna()
             | (c == ""))
    for bad, kind, col in ((invoice_date.isna(), "unparseable_date", "invoice_date"),
                           (ship_date.isna(), "unparseable_date", "ship_date"),
                           (weight.isna(), "unparseable_weight", "weight"),
                           (invoice_type.isna() | pro.isna(), "unparseable_invoice_type", "pro_number"),
                           (c == "", "unknown_carrier_name", "carrier" if "carrier" in hdr else "carrier_id")):
        rec.problem(hdr[bad], kind, col, "row dropped")
    rec.problem(hdr[bol.isna() & ~fatal], "unparseable_bol", "bol", "no 8-digit BOL; invoice kept, exact match impossible")
    for col, cleaned in (("origin", origin), ("destination", destination)):
        rec.problem(hdr[cleaned.isna() & ~fatal], "unknown_city", col, "not a city in the shipper's lanes; invoice kept")
    rec.problem(hdr[total.isna() & ~fatal], "unparseable_total", "total", "stated total unreadable; invoice kept")

    out = pd.DataFrame({
        "invoice_id": c + ":" + hdr["control_id"], "carrier_id": c, "control_id": hdr["control_id"],
        "invoice_number": hdr["invoice_number"], "pro_number": pro, "bol_raw": hdr["bol"],
        "bol_canonical": bol, "invoice_type": invoice_type, "supersedes_invoice_number": supersedes,
        "invoice_date": invoice_date, "ship_date": ship_date, "origin": origin, "destination": destination,
        "billed_weight_lbs": weight, "total": total.round(2), "source_file": source, "_row": hdr["_row"]})[~fatal]

    # charge lines
    lines = raw_lines(df, spec)
    lines = lines[lines["control_id"].isin(out["control_id"])]
    charge_map = {**cfg["normalization"]["charge_code_map"],
                  **{code: code for code in cfg["normalization"]["charge_code_map"].values()}}
    code = lines["charge_raw"].map(charge_map)
    amount = parse_money(lines["amount_raw"])
    rec.problem(lines[code.isna()], "unmapped_charge", "charge_raw",
                "charge description not in normalization.charge_code_map; line not loaded, amount "
                + lines["amount_raw"])
    rec.problem(lines[code.notna() & amount.isna()], "unparseable_amount", "amount_raw", "line not loaded")
    rec.fix(lines["carrier_id"], code.notna() & (lines["charge_raw"] != code), "charge_description_to_code", "line")
    rec.fix(lines["carrier_id"], lines["amount_raw"].str.contains(r"[$,]"), "currency_to_float", "amount field")
    good = code.notna() & amount.notna()
    out_lines = pd.DataFrame({"invoice_id": lines["carrier_id"] + ":" + lines["control_id"],
                              "charge_code": code, "amount": amount.round(2)})[good]
    return out, out_lines


# ------------------------------------------------------------ across files


def mark_superseded(inv, rec):
    """Set `is_superseded` on the invoice each rebill replaces, and fill `supersedes_invoice_number`.

    A rebill replaces the latest earlier, not-yet-superseded invoice it refers to: the one
    whose invoice number it cites (formats B and D) or, where only the pro suffix marks it
    (A and C), the one sharing its base pro number. A balance-due invoice is a supplement,
    never the invoice being replaced. Rebills are processed in received order so a rebill of
    a rebill supersedes the earlier rebill.
    """
    inv["is_superseded"] = False
    for i, r in inv[inv["invoice_type"] == "rebill"].sort_values(["received_date", "control_id"]).iterrows():
        cand = ((inv["carrier_id"] == r["carrier_id"]) & (inv["invoice_type"] != "balance_due")
                & (inv["received_date"] < r["received_date"]) & ~inv["is_superseded"])
        cand &= (inv["invoice_number"] == r["supersedes_invoice_number"] if r["supersedes_invoice_number"]
                 else inv["pro_number"] == r["pro_number"])
        if not cand.any():
            rec.add(r["source_file"], r["_row"], r["carrier_id"], r["control_id"], "rebill_target_not_found",
                    r["supersedes_invoice_number"] or r["pro_number"], "no earlier invoice to supersede")
            continue
        target = inv[cand].sort_values(["received_date", "control_id"]).index[-1]
        inv.loc[target, "is_superseded"] = True
        inv.loc[i, "supersedes_invoice_number"] = inv.loc[target, "invoice_number"]


def match_shipments(inv, shipments, lanes, cfg):
    """Set `shipment_id` and `match_method` (exact | fallback | unmatched) on every invoice.

    Exact: carrier + canonical BOL. Otherwise fallback: carrier + origin + destination + ship
    date within +/-N days + billed weight within X% of the shipment's, and only if exactly one
    shipment qualifies (two candidates are ambiguous, so the invoice stays unmatched).
    """
    fb = cfg["normalization"]["fallback_match"]
    ship = shipments.merge(lanes[["lane_id", "origin_city", "destination_city"]], on="lane_id")
    by_bol = dict(zip(zip(ship["carrier_id"], ship["bol"]), ship["shipment_id"]))
    inv["shipment_id"] = [by_bol.get(k) for k in zip(inv["carrier_id"], inv["bol_canonical"])]
    inv["match_method"] = ["unmatched" if pd.isna(s) else "exact" for s in inv["shipment_id"]]
    lane_groups = {k: g for k, g in ship.groupby(["carrier_id", "origin_city", "destination_city"])}
    for i, r in inv[inv["match_method"] == "unmatched"].iterrows():
        g = lane_groups.get((r["carrier_id"], r["origin"], r["destination"]))
        if g is None:
            continue
        near = g[((g["ship_date"] - r["ship_date"]).abs() <= pd.Timedelta(days=fb["ship_date_days"]))
                 & ((g["weight_lbs"] - r["billed_weight_lbs"]).abs() <= fb["weight_pct"] * g["weight_lbs"])]
        if len(near) == 1:
            inv.loc[i, ["shipment_id", "match_method"]] = [near.iloc[0]["shipment_id"], "fallback"]


def normalize(cfg, data_dir=REPO_ROOT / "data"):
    """Read every raw carrier file and return {invoices, invoice_lines, normalization_report,
    normalization_exceptions}. Reads data/raw and data/reference only."""
    data_dir = Path(data_dir)
    ref = data_dir / "reference"
    lanes, shipments, receipts = (read_csv(ref / f"{name}.csv") for name in ("lanes", "shipments", "ap_receipt_log"))
    city_lookup = {city_key(c): c for c in pd.concat([lanes["origin_city"], lanes["destination_city"]]).unique()}
    rec = Recorder()

    invoices, lines = [], []
    for path in sorted((data_dir / "raw" / "invoices").glob("*/*.csv")):
        source = path.relative_to(data_dir).as_posix()
        raw = read_raw_csv(path)
        layout = detect_layout(raw.columns)
        if layout is None:
            rec.add(source, 1, path.parent.name, "", "unrecognized_header", ",".join(raw.columns),
                    "header matches no known carrier layout; file not loaded")
            continue
        inv, ln = parse_file(raw, layout, path.parent.name, source, cfg, city_lookup, rec)
        invoices.append(inv)
        lines.append(ln)
    inv = pd.concat(invoices, ignore_index=True)
    lines = pd.concat(lines, ignore_index=True)

    twice = inv["invoice_id"].duplicated()          # a control id is unique per carrier; keep the first
    for _, r in inv[twice].iterrows():
        rec.add(r["source_file"], r["_row"], r["carrier_id"], r["control_id"], "duplicate_control_id",
                r["control_id"], "same carrier and control id seen before; later copy dropped")
    inv = inv[~twice].reset_index(drop=True)
    lines = lines[lines["invoice_id"].isin(inv["invoice_id"])]

    # received date: the AP receipt stamp, not anything the carrier wrote
    inv = inv.merge(receipts, on=["carrier_id", "control_id"], how="left")
    for _, r in inv[inv["received_date"].isna()].iterrows():
        rec.add(r["source_file"], r["_row"], r["carrier_id"], r["control_id"], "missing_receipt", "",
                "no row in ap_receipt_log for this carrier and control id")

    # totals check: the lines must add up to the stated total
    inv["lines_total"] = inv["invoice_id"].map(lines.groupby("invoice_id")["amount"].sum()).fillna(0.0).round(2)
    tolerance = cfg["normalization"]["totals_tolerance"]
    inv["totals_ok"] = inv["total"].notna() & ((inv["lines_total"] - inv["total"]).abs() <= tolerance + MONEY_SLACK)
    for _, r in inv[inv["total"].notna() & ~inv["totals_ok"]].iterrows():
        rec.add(r["source_file"], r["_row"], r["carrier_id"], r["control_id"], "totals_mismatch", f"{r['total']:.2f}",
                f"charge lines sum to {r['lines_total']:.2f} vs stated total {r['total']:.2f}")

    mark_superseded(inv, rec)
    inv["supersedes_invoice_number"] = inv["supersedes_invoice_number"].mask(inv["supersedes_invoice_number"] == "")
    match_shipments(inv, shipments, lanes, cfg)

    inv = inv.sort_values(["received_date", "invoice_id"], kind="stable").reset_index(drop=True)
    lines = lines.sort_values("invoice_id", kind="stable").reset_index(drop=True)
    return {"invoices": inv[INVOICE_COLUMNS], "invoice_lines": lines[["invoice_id", "charge_code", "amount"]],
            "normalization_report": rec.report(), "normalization_exceptions": rec.exceptions()}


def write_normalized(result, data_dir=REPO_ROOT / "data"):
    """Write the four normalized tables to data/normalized/."""
    out = Path(data_dir) / "normalized"
    out.mkdir(parents=True, exist_ok=True)
    for name, df in result.items():
        write_csv(df, out / f"{name}.csv")
