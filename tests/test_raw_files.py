"""Stage 2: the four messy carrier layouts and the AP receipt log."""

import re

import pandas as pd
import pytest

from freight_audit_lab.csv_io import read_csv, read_raw_csv
from freight_audit_lab.generate.ground_truth import LABEL_COLUMNS
from freight_audit_lab.generate.render import HEADERS

CONTROL_COL = {"A": "EDI Ctrl #", "B": "doc_id", "C": "Transmission ID", "D": "Control No"}
NUMBER_COL = {"A": "Invoice No", "B": "invoice_number", "C": "Invoice #", "D": "Inv Number"}
PRO_COL = {"A": "Pro #", "B": "pro_number", "C": "PRO Number", "D": "Pro Number"}
BOL_COL = {"A": "BOL", "B": "bill_of_lading", "C": "BOL Number", "D": "BOL"}


@pytest.fixture(scope="module")
def raw(full, cfg):
    """{carrier_id: DataFrame of all its raw rows, as text}, plus the carrier -> format map."""
    _, data_dir = full
    fmt = {c["id"]: c["format"] for c in cfg["carriers"]}
    frames = {}
    for cid in fmt:
        files = sorted((data_dir / "raw" / "invoices" / cid).glob("*.csv"))
        frames[cid] = pd.concat([read_raw_csv(p).assign(_file=p.stem) for p in files], ignore_index=True)
    return frames, fmt


def test_files_live_under_carrier_and_received_month(full, raw):
    tables, data_dir = full
    frames, fmt = raw
    log = tables["ap_receipt_log"].set_index(["carrier_id", "control_id"])["received_date"]
    for cid, df in frames.items():
        control = df[CONTROL_COL[fmt[cid]]]
        months = log.loc[[(cid, c) for c in control]].dt.strftime("%Y-%m").to_numpy()
        assert (df["_file"].to_numpy() == months).all()


def test_no_ground_truth_columns_or_labels_in_raw_files(full, raw):
    frames, fmt = raw
    forbidden = {c.lower() for c in LABEL_COLUMNS} | {"shipment_id", "received_date", "received"}
    for cid, df in frames.items():
        headers = {h.lower() for h in df.columns if h != "_file"}
        assert not headers & forbidden, (cid, headers & forbidden)
    # B and D carry an explicit invoice type, so "rebill" and "balance_due" are legitimately visible
    labels = set(full[0]["labels"]["label"]) - {"clean", "rebill", "balance_due"}
    for cid, df in frames.items():
        text = " ".join(df.drop(columns="_file").astype(str).to_numpy().ravel()[:200000]).lower()
        assert not any(label in text for label in labels)


def test_each_format_has_its_own_header_signature():
    signatures = [frozenset(h) for h in HEADERS.values()]
    assert len(set(signatures)) == 4
    assert "Carrier" in HEADERS["C"] and not any("Carrier" in HEADERS[f] for f in "ABD")


def test_every_invoice_appears_exactly_once_per_control_id(full, raw):
    tables, _ = full
    frames, fmt = raw
    ids = pd.concat([df[CONTROL_COL[fmt[cid]]].drop_duplicates().to_frame("control_id").assign(carrier_id=cid)
                     for cid, df in frames.items()])
    log = tables["ap_receipt_log"]
    assert len(ids) == len(log)
    assert set(zip(ids["carrier_id"], ids["control_id"])) == set(zip(log["carrier_id"], log["control_id"]))


def test_raw_amounts_are_lossless(full, raw):
    """Parsing each layout by hand recovers every invoice's total: nothing is lost in rendering."""
    tables, _ = full
    frames, fmt = raw
    totals = tables["invoices"].set_index(["carrier_id", "control_id"])["total"]
    for cid, df in frames.items():
        f = fmt[cid]
        if f == "A":
            got = df.set_index(CONTROL_COL[f])["Total"].astype(float)
        elif f == "C":
            got = df.set_index(CONTROL_COL[f])["Total Due"].str.replace(r"[$,]", "", regex=True).astype(float)
        else:
            col = "amount" if f == "B" else "Amount"
            got = df.assign(v=df[col].astype(float)).groupby(CONTROL_COL[f])["v"].sum()
        want = totals.loc[cid].loc[got.index]
        assert (got.to_numpy() == pytest.approx(want.to_numpy(), abs=0.005))


def test_long_layouts_repeat_the_invoice_total_on_every_line(full, raw):
    """Formats B and D carry a stated total, so the Stage 3 totals check covers all four layouts."""
    tables, _ = full
    frames, fmt = raw
    totals = tables["invoices"].set_index(["carrier_id", "control_id"])["total"]
    for cid, df in frames.items():
        if fmt[cid] not in ("B", "D"):
            continue
        f = fmt[cid]
        stated = df.groupby(CONTROL_COL[f])["Invoice Total" if f == "D" else "invoice_total"]
        assert (stated.nunique() == 1).all()                            # same value on every line
        got = stated.first().astype(float)
        assert (got.to_numpy() == pytest.approx(totals.loc[cid].loc[got.index].to_numpy(), abs=0.005))


def test_ids_survive_as_text_with_leading_zeros(raw):
    frames, fmt = raw
    for cid, df in frames.items():
        numbers = df[NUMBER_COL[fmt[cid]]]
        assert numbers.str.fullmatch(r"\d{7}").all() and numbers.str.startswith("0").all()
        assert df[PRO_COL[fmt[cid]]].str.match(r"\d{9}").all()


def test_layout_specific_messiness(raw):
    frames, fmt = raw
    a, b, c, d = (pd.concat([df for cid, df in frames.items() if fmt[cid] == f]) for f in "ABCD")
    assert a["Inv Date"].str.fullmatch(r"\d\d/\d\d/\d{4}").all() and a["Ship Date"].str.fullmatch(r"\d\d/\d\d/\d{4}").all()
    assert b["invoice_date"].str.fullmatch(r"\d{4}-\d\d-\d\d").all()
    assert (b["weight_cwt"].astype(float) * 100).round(6).mod(1).eq(0).all()          # cwt of whole pounds
    assert set(b["invoice_type"]) == {"original", "rebill", "balance_due"}
    assert c["Invoice Date"].str.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d").all()
    assert c["Linehaul"].str.startswith("$").where(c["Linehaul"] != "", True).all()
    assert c["Weight"].str.fullmatch(r"[\d,]+ LB").all() and c["Weight"].str.contains(",").any()
    assert c["Carrier"].nunique() > 2 and set(c["Carrier"]).isdisjoint(
        {"Carrier C Express", "Carrier G Logistics"})                                # variants only
    assert d["Inv Date"].str.fullmatch(r"\d\d-[A-Z][a-z]{2}-\d\d").all()
    assert d["BOL"].str.contains("BOL", case=False).all()
    assert set(d["Type"]) == {"ORIGINAL", "REBILL", "BAL DUE"}
    assert d["Orig"].nunique() > d["Orig"].str.lower().str.replace(r"[^a-z ]", "", regex=True).str.split().str[0].nunique()


def test_pro_suffixes_mark_rebills_and_balance_dues_in_wide_layouts(full, raw):
    tables, _ = full
    frames, fmt = raw
    inv = tables["invoices"]
    for cid, suffixes in (("CARA", ("-C", "-BD")), ("CARE", ("-C", "-BD")), ("CARC", ("R", "B")), ("CARG", ("R", "B"))):
        pro = frames[cid][PRO_COL[fmt[cid]]]
        mine = inv[inv["carrier_id"] == cid]
        assert pro.str.endswith(suffixes[0]).sum() == (mine["invoice_type"] == "rebill").sum()
        assert pro.str.endswith(suffixes[1]).sum() == (mine["invoice_type"] == "balance_due").sum()
    b = frames["CARB"]
    assert (b.loc[b["invoice_type"] == "rebill", "supersedes"] != "").all()
    assert (b.loc[b["invoice_type"] != "rebill", "supersedes"] == "").all()


def test_unknown_charge_lines_appear_only_in_format_d(full, raw, cfg):
    frames, fmt = raw
    desc = cfg["traps"]["unknown_charge_description"]
    d = pd.concat([df for cid, df in frames.items() if fmt[cid] == "D"])
    misc = d[d["Charge Description"] == desc]
    assert len(misc) == cfg["traps"]["unknown_charge_lines"]
    assert (misc["Amount"].astype(float) == 0).all()
    assert set(d["Charge Description"]) - {desc} <= set(cfg["normalization"]["charge_code_map"])
    for cid, df in frames.items():
        if fmt[cid] != "D":
            assert not df.astype(str).apply(lambda col: col.str.contains(desc)).any().any()


def test_receipt_log_round_trips_with_string_ids(full):
    _, data_dir = full
    log = read_csv(data_dir / "reference" / "ap_receipt_log.csv")
    assert log["control_id"].str.fullmatch(r"\d{8}").all() and log["control_id"].str.startswith("0").any()
    assert list(log.columns) == ["carrier_id", "control_id", "received_date"]
