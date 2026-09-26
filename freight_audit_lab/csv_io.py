"""The one way this project reads CSVs.

Identifiers are text, not numbers. About 10% of BOL numbers start with a zero
("00482913"), and pandas would read that column as an integer and silently drop the zeros,
so the BOL would no longer match the shipment. Every ID column is therefore read as a
string. Nothing outside this module calls `pd.read_csv` (a test enforces that).
"""

from pathlib import Path

import pandas as pd

# Columns that identify things. Always read as str, whatever they look like.
ID_COLUMNS = ["bol", "bol_raw", "bol_canonical", "pro_number", "invoice_number",
              "supersedes_invoice_number", "control_id", "shipment_id", "invoice_id"]

# Columns holding dates in our own (ISO) files, parsed to Timestamps when present.
DATE_COLUMNS = ["ship_date", "delivery_date", "invoice_date", "received_date", "authorized_at",
                "certified_at", "effective_from", "effective_to", "week_start"]


def read_csv(path, **kwargs):
    """Read one of our own ISO-dated CSVs: ID columns as str, known date columns as dates."""
    dtype = {col: "str" for col in ID_COLUMNS}
    header = pd.read_csv(path, nrows=0).columns
    dates = [c for c in DATE_COLUMNS if c in header]
    return pd.read_csv(path, dtype=dtype, parse_dates=dates, **kwargs)


def read_raw_csv(path):
    """Read a messy carrier file with every cell as text.

    Carrier layouts disagree about column names, dates, and number formats, so nothing is
    interpreted here. Normalization parses each column on purpose, and empty cells stay ""
    rather than becoming NaN so the raw text is preserved exactly.
    """
    return pd.read_csv(path, dtype="str", keep_default_na=False)


def load_reference(data_dir):
    """Every data/reference/*.csv as {file stem: DataFrame}."""
    return {p.stem: read_csv(p) for p in sorted((Path(data_dir) / "reference").glob("*.csv"))}
