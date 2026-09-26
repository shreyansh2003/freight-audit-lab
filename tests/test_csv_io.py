"""IDs must survive a CSV round trip as text, leading zeros and all."""

from pathlib import Path

import pandas as pd

from freight_audit_lab.csv_io import load_reference, read_csv, read_raw_csv

REPO = Path(__file__).resolve().parent.parent


def test_id_columns_keep_leading_zeros(tmp_path):
    path = tmp_path / "x.csv"
    path.write_text("bol,pro_number,invoice_number,control_id,shipment_id,ship_date\n"
                    "00482913,0012345,000789,00042,SHP00001,2025-01-02\n")
    row = read_csv(path).iloc[0]
    assert (row["bol"], row["pro_number"], row["invoice_number"], row["control_id"]) == \
        ("00482913", "0012345", "000789", "00042")
    assert row["ship_date"] == pd.Timestamp("2025-01-02")


def test_plain_pandas_would_have_dropped_the_zeros(tmp_path):
    """Documents the bug this module exists to prevent."""
    path = tmp_path / "x.csv"
    path.write_text("bol\n00482913\n")
    assert pd.read_csv(path)["bol"].iloc[0] == 482913


def test_raw_files_are_all_text(tmp_path):
    path = tmp_path / "raw.csv"
    path.write_text("BOL,Amount,Note\n00482913,1.50,\n")
    row = read_raw_csv(path).iloc[0]
    assert (row["BOL"], row["Amount"], row["Note"]) == ("00482913", "1.50", "")


def test_reference_bols_match_the_generator_output():
    ref = load_reference(REPO / "data")
    bol = ref["shipments"]["bol"]
    assert bol.str.fullmatch(r"\d{8}").all()
    assert bol.str.startswith("0").any()          # the ~10% with a leading zero survived


def test_nobody_else_calls_pd_read_csv():
    offenders = []
    for path in list((REPO / "freight_audit_lab").rglob("*.py")) + list((REPO / "tests").rglob("*.py")):
        if path.name in ("csv_io.py", "test_csv_io.py"):
            continue
        if "read_csv(" in path.read_text():
            offenders.append(str(path.relative_to(REPO)))
    assert not offenders, f"use freight_audit_lab.csv_io instead of read_csv in: {offenders}"
