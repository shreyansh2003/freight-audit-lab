"""Stage 3: normalization and matching."""

import shutil

import pandas as pd
import pytest

from freight_audit_lab.config import load_config
from freight_audit_lab.generate.render import HEADERS
from freight_audit_lab.generate.traps import BOL_PATTERNS, noisy_bol
from freight_audit_lab.normalize import canonical_bol, detect_layout, normalize, write_normalized

CENT = 0.01

HEADER = {"A": ",".join(HEADERS["A"]), "B": ",".join(HEADERS["B"]), "C": ",".join(HEADERS["C"]),
          "D": ",".join(HEADERS["D"])}


def build(tmp_path, raw, shipments, receipts, lanes=None):
    """Write a tiny data dir: raw {'CARA/2025-03.csv': text}, shipments rows, receipt rows."""
    lanes = lanes or ["L1,Chicago,St. Louis", "L2,Chicago,Dallas"]
    ref = tmp_path / "reference"
    ref.mkdir(parents=True)
    (ref / "lanes.csv").write_text("lane_id,origin_city,destination_city\n" + "\n".join(lanes) + "\n")
    (ref / "shipments.csv").write_text("shipment_id,bol,ship_date,lane_id,carrier_id,weight_lbs\n"
                                       + "\n".join(shipments) + "\n")
    (ref / "ap_receipt_log.csv").write_text("carrier_id,control_id,received_date\n" + "\n".join(receipts) + "\n")
    for name, text in raw.items():
        path = tmp_path / "raw" / "invoices" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return tmp_path


def run(tmp_path, cfg, **kw):
    return normalize(cfg, build(tmp_path, **kw))


def exceptions_of(result, kind):
    ex = result["normalization_exceptions"]
    return ex[ex["exception_type"] == kind]


# One invoice (LH 300.00 + FSC 70.50 + LIFTGATE 95.00 = 465.50), written in each carrier's layout.
FOUR_FORMATS = {
    "CARA/2025-03.csv": HEADER["A"] + "\n"
        '0100001,123456789,BOL-0048 2913,11111111,03/20/2025,03/04/2025,"Chicago, IL","St. Louis, MO",1250,'
        "300.00,70.50,LIFTGATE,95.00,,,465.50\n",
    "CARB/2025-03.csv": HEADER["B"] + "\n"
        "0200001,123456789,482913,22222222,2025-03-20,2025-03-04,Chicago,St. Louis,12.50,LH,300.00,original,,465.50\n"
        "0200001,123456789,482913,22222222,2025-03-20,2025-03-04,Chicago,St. Louis,12.50,FSC,70.50,original,,465.50\n"
        "0200001,123456789,482913,22222222,2025-03-20,2025-03-04,Chicago,St. Louis,12.50,LIFTGATE,95.00,original,,465.50\n",
    "CARC/2025-03.csv": HEADER["C"] + "\n"
        'C Express,0300001,123456789,bol#00482913,33333333,2025-03-20 00:00:00,2025-03-04 00:00:00,'
        'CHICAGO IL,ST. LOUIS MO,"1,250 LB",$300.00,$70.50,LIFTGATE,$95.00,,,$465.50\n',
    "CARD/2025-03.csv": HEADER["D"] + "\n"
        '0400001,123456789,BOL#00482913,44444444,20-Mar-25,04-Mar-25,"chicago,il",ST LOUIS  MO,1250,LINEHAUL,300.00,ORIGINAL,,465.50\n'
        '0400001,123456789,BOL#00482913,44444444,20-Mar-25,04-Mar-25,"chicago,il",ST LOUIS  MO,1250,FUEL SURCHARGE,70.50,ORIGINAL,,465.50\n'
        '0400001,123456789,BOL#00482913,44444444,20-Mar-25,04-Mar-25,"chicago,il",ST LOUIS  MO,1250,LIFTGATE SERVICE,95.00,ORIGINAL,,465.50\n',
}
FOUR_SHIPMENTS = [f"S-{c},00482913,2025-03-04,L1,CAR{c},1250" for c in "ABCD"]
FOUR_RECEIPTS = [f"CAR{c},{i}{i}{i}{i}{i}{i}{i}{i},2025-03-24" for c, i in zip("ABCD", "1234")]


def four_formats(tmp_path, cfg):
    return run(tmp_path, cfg, raw=FOUR_FORMATS, shipments=FOUR_SHIPMENTS, receipts=FOUR_RECEIPTS)


# ---------------------------------------------------------------- layouts


def test_detect_layout_reads_the_header_not_the_folder():
    for name, header in HEADERS.items():
        assert detect_layout(header) == name
    assert detect_layout(["not", "a", "carrier", "file"]) is None


def test_each_format_parses_the_same_invoice_into_the_same_rows(tmp_path, cfg):
    r = four_formats(tmp_path, cfg)
    inv, lines = r["invoices"], r["invoice_lines"]
    assert len(inv) == 4 and r["normalization_exceptions"].empty
    carrier_specific = ["invoice_id", "carrier_id", "invoice_number", "bol_raw", "source_file", "shipment_id"]
    same = inv.drop(columns=carrier_specific).drop_duplicates()
    assert len(same) == 1
    row = same.iloc[0]
    assert (row["pro_number"], row["bol_canonical"], row["invoice_type"]) == ("123456789", "00482913", "original")
    assert row["invoice_date"] == pd.Timestamp("2025-03-20") and row["ship_date"] == pd.Timestamp("2025-03-04")
    assert row["received_date"] == pd.Timestamp("2025-03-24")
    assert (row["origin"], row["destination"], row["billed_weight_lbs"]) == ("Chicago", "St. Louis", 1250)
    assert row["total"] == pytest.approx(465.50, abs=CENT) and row["totals_ok"] and not row["is_superseded"]
    assert set(inv["invoice_id"]) == {f"CAR{c}:{i}{i}{i}{i}{i}{i}{i}{i}" for c, i in zip("ABCD", "1234")}
    assert set(inv["match_method"]) == {"exact"}
    assert dict(zip(inv["carrier_id"], inv["shipment_id"])) == {f"CAR{c}": f"S-{c}" for c in "ABCD"}
    for _, g in lines.groupby("invoice_id"):
        assert list(zip(g["charge_code"], g["amount"])) == [("LH", 300.00), ("FSC", 70.50), ("LIFTGATE", 95.00)]


def test_every_fix_is_counted_per_carrier(tmp_path, cfg):
    report = four_formats(tmp_path, cfg)["normalization_report"].set_index(["carrier_id", "fix_type"])["count"]
    assert report[("CARA", "date_parsing")] == 2 and ("CARB", "date_parsing") not in report.index   # ISO needs no fix
    assert report[("CARB", "cwt_to_lbs")] == 1 and report[("CARC", "weight_text_cleanup")] == 1
    assert ("CARA", "weight_text_cleanup") not in report.index
    assert report[("CARC", "carrier_name_variant")] == 1 and report[("CARC", "currency_to_float")] == 4
    assert report[("CARD", "charge_description_to_code")] == 3 and report[("CARD", "invoice_type_text")] == 1
    assert all(report[(c, "bol_canonicalization")] == 1 for c in ("CARA", "CARB", "CARC", "CARD"))
    assert report[("CARA", "city_cleanup")] == 2 and ("CARB", "city_cleanup") not in report.index


def test_carrier_name_variant_maps_through_config_and_unknown_names_are_dropped(tmp_path, cfg):
    bad = FOUR_FORMATS["CARC/2025-03.csv"].replace("C Express", "Mystery Freight Co")
    r = run(tmp_path, cfg, raw={"CARC/2025-03.csv": bad}, shipments=FOUR_SHIPMENTS, receipts=FOUR_RECEIPTS)
    assert r["invoices"].empty
    ex = exceptions_of(r, "unknown_carrier_name")
    assert len(ex) == 1 and ex.iloc[0]["raw_value"] == "Mystery Freight Co" and ex.iloc[0]["csv_row"] == 2


def test_unrecognized_header_and_unparseable_date_go_to_exceptions(tmp_path, cfg):
    bad_date = FOUR_FORMATS["CARA/2025-03.csv"].replace("03/20/2025", "13/45/2025")
    r = run(tmp_path, cfg, raw={"CARA/2025-03.csv": bad_date, "CARE/2025-03.csv": "a,b,c\n1,2,3\n"},
            shipments=FOUR_SHIPMENTS, receipts=FOUR_RECEIPTS)
    assert r["invoices"].empty and r["invoice_lines"].empty
    assert exceptions_of(r, "unrecognized_header").iloc[0]["source_file"] == "raw/invoices/CARE/2025-03.csv"
    date_ex = exceptions_of(r, "unparseable_date")
    assert len(date_ex) == 1 and date_ex.iloc[0]["raw_value"] == "13/45/2025"


# ---------------------------------------------------------------- BOLs and cities


@pytest.mark.parametrize("digits", ["00482913", "12345678", "00000007"])
def test_canonical_bol_undoes_every_noise_pattern(digits):
    for pattern in BOL_PATTERNS:
        if pattern == "zeros_dropped" and digits[0] != "0":
            continue
        assert canonical_bol(noisy_bol(digits, pattern)) == digits, pattern
    assert canonical_bol("BOL#" + digits) == digits


def test_canonical_bol_refuses_what_is_not_a_bol():
    assert canonical_bol("") is None and canonical_bol("BOL-") is None and canonical_bol("123456789") is None


def test_unknown_city_is_logged_and_the_invoice_kept(tmp_path, cfg):
    raw = {"CARB/2025-03.csv": FOUR_FORMATS["CARB/2025-03.csv"].replace("St. Louis", "Springfield")}
    r = run(tmp_path, cfg, raw=raw, shipments=FOUR_SHIPMENTS, receipts=FOUR_RECEIPTS)
    inv = r["invoices"]
    assert len(inv) == 1 and pd.isna(inv.iloc[0]["destination"]) and inv.iloc[0]["match_method"] == "exact"
    assert set(exceptions_of(r, "unknown_city")["raw_value"]) == {"Springfield"}


# ---------------------------------------------------------------- matching

CARA = "CARA"


def one_a_invoice(n, bol, ship, weight, origin="Chicago, IL", dest="St. Louis, MO", pro=None, control=None):
    """A wide-layout CARA row with LH 100.00 + FSC 20.00. `n` makes numbers and ids unique."""
    control = control or f"9{n:07d}"
    return (f'01{n:05d},{pro or 700000000 + n},{bol},{control},03/20/2025,{ship},"{origin}","{dest}",{weight},'
            "100.00,20.00,,,,,120.00\n"), f"CARA,{control},2025-03-25"


def fallback_case(tmp_path, cfg, cases, shipments):
    rows, receipts = zip(*(one_a_invoice(n, **kw) for n, kw in enumerate(cases, start=1)))
    return run(tmp_path, cfg, raw={"CARA/2025-03.csv": HEADER["A"] + "\n" + "".join(rows)},
               shipments=shipments, receipts=list(receipts))


def test_exact_match_wins_and_fallback_resolves_a_transposed_bol(tmp_path, cfg):
    shipments = ["S1,12345678,2025-03-04,L1,CARA,1000"]
    r = fallback_case(tmp_path, cfg, [
        dict(bol="BOL#12345678", ship="03/04/2025", weight=1000),        # exact, noise stripped
        dict(bol="21345678", ship="03/04/2025", weight=1010),             # digits 1,2 swapped: fallback
    ], shipments)
    got = r["invoices"].sort_values("invoice_number")[["match_method", "shipment_id"]].values.tolist()
    assert got == [["exact", "S1"], ["fallback", "S1"]]


def test_fallback_tolerances_on_date_and_weight(tmp_path, cfg):
    shipments = ["S1,12345678,2025-03-04,L1,CARA,1000"]
    cases = [dict(bol="99999991", ship="03/05/2025", weight=1020),        # 1 day, 2.0% -> fallback
             dict(bol="99999992", ship="03/06/2025", weight=1000),        # 2 days -> unmatched
             dict(bol="99999993", ship="03/04/2025", weight=1021),        # 2.1% -> unmatched
             dict(bol="99999994", ship="03/04/2025", weight=1000, dest="Dallas, TX")]   # wrong lane -> unmatched
    r = fallback_case(tmp_path, cfg, cases, shipments)
    assert r["invoices"].sort_values("invoice_number")["match_method"].tolist() == [
        "fallback", "unmatched", "unmatched", "unmatched"]


def test_fallback_refuses_when_two_shipments_tie(tmp_path, cfg):
    shipments = ["S1,12345678,2025-03-04,L1,CARA,1000", "S2,12345679,2025-03-04,L1,CARA,1005"]
    r = fallback_case(tmp_path, cfg, [dict(bol="99999999", ship="03/04/2025", weight=1002)], shipments)
    inv = r["invoices"].iloc[0]
    assert inv["match_method"] == "unmatched" and pd.isna(inv["shipment_id"])


def test_a_bol_belonging_to_another_carrier_is_not_a_match(tmp_path, cfg):
    shipments = ["S1,12345678,2025-03-04,L1,CARB,1000"]
    r = fallback_case(tmp_path, cfg, [dict(bol="12345678", ship="03/04/2025", weight=1000)], shipments)
    assert r["invoices"].iloc[0]["match_method"] == "unmatched"


# ---------------------------------------------------------------- charges and totals


def test_unknown_charge_goes_to_exceptions_not_into_the_lines(tmp_path, cfg):
    d = FOUR_FORMATS["CARD/2025-03.csv"].splitlines()
    d.append(d[2].replace("FUEL SURCHARGE", "MISC ADJ").replace("70.50", "0.00"))
    r = run(tmp_path, cfg, raw={"CARD/2025-03.csv": "\n".join(d) + "\n"}, shipments=FOUR_SHIPMENTS, receipts=FOUR_RECEIPTS)
    ex = exceptions_of(r, "unmapped_charge")
    assert len(ex) == 1 and ex.iloc[0]["raw_value"] == "MISC ADJ" and ex.iloc[0]["csv_row"] == 5
    assert ex.iloc[0]["source_file"] == "raw/invoices/CARD/2025-03.csv" and "amount 0.00" in ex.iloc[0]["detail"]
    assert "MISC ADJ" not in set(r["invoice_lines"]["charge_code"]) and len(r["invoice_lines"]) == 3
    assert r["invoices"].iloc[0]["totals_ok"]                # a $0.00 line does not break the total


def test_totals_check_uses_a_one_cent_tolerance_in_every_layout(tmp_path, cfg):
    def a_row(n, total):
        return (f'01{n:05d},70000000{n},1234567{n},9000000{n},03/20/2025,03/04/2025,"Chicago, IL","Dallas, TX",1000,'
                f"300.00,70.50,LIFTGATE,95.00,,,{total}\n")
    raw = {"CARA/2025-03.csv": HEADER["A"] + "\n" + a_row(1, "465.50") + a_row(2, "465.51") + a_row(3, "465.52"),
           "CARB/2025-03.csv": FOUR_FORMATS["CARB/2025-03.csv"].replace("465.50", "500.00"),
           "CARD/2025-03.csv": FOUR_FORMATS["CARD/2025-03.csv"].replace("465.50", "400.00")}
    receipts = [f"CARA,9000000{n},2025-03-25" for n in (1, 2, 3)] + ["CARB,22222222,2025-03-25", "CARD,44444444,2025-03-25"]
    r = run(tmp_path, cfg, raw=raw, shipments=FOUR_SHIPMENTS, receipts=receipts)
    ok = r["invoices"].set_index("invoice_id")["totals_ok"]
    assert ok["CARA:90000001"] and ok["CARA:90000002"] and not ok["CARA:90000003"]
    assert not ok["CARB:22222222"] and not ok["CARD:44444444"]
    bad = exceptions_of(r, "totals_mismatch").set_index("control_id")
    assert set(bad.index) == {"90000003", "22222222", "44444444"}
    assert "465.50" in bad.loc["22222222", "detail"] and "500.00" in bad.loc["22222222", "detail"]


def test_a_missing_receipt_is_logged(tmp_path, cfg):
    r = run(tmp_path, cfg, raw={"CARA/2025-03.csv": FOUR_FORMATS["CARA/2025-03.csv"]}, shipments=FOUR_SHIPMENTS,
            receipts=["CARA,00000000,2025-03-24"])
    assert pd.isna(r["invoices"].iloc[0]["received_date"]) and len(exceptions_of(r, "missing_receipt")) == 1


# ---------------------------------------------------------------- rebills


def test_pro_suffix_rebill_supersedes_the_original_but_not_a_balance_due(tmp_path, cfg):
    rows = [  # same shipment: original, then a balance-due, then a rebill of the original
        ("0100001", "111111111", "9000001", "03/01/2025"), ("0100002", "111111111-BD", "9000002", "03/05/2025"),
        ("0100003", "111111111-C", "9000003", "03/15/2025"), ("0100004", "222222222", "9000004", "03/02/2025")]
    text = HEADER["A"] + "\n" + "".join(
        f'{num},{pro},12345678,{ctl},{d},03/01/2025,"Chicago, IL","Dallas, TX",1000,100.00,20.00,,,,,120.00\n'
        for num, pro, ctl, d in rows)
    receipts = [f"CARA,{ctl},{d[6:]}-{d[:2]}-{d[3:5]}" for _, _, ctl, d in rows]
    r = run(tmp_path, cfg, raw={"CARA/2025-03.csv": text}, shipments=FOUR_SHIPMENTS, receipts=receipts)
    inv = r["invoices"].set_index("invoice_number")
    assert inv["invoice_type"].to_dict() == {"0100001": "original", "0100002": "balance_due", "0100003": "rebill",
                                             "0100004": "original"}
    assert inv["is_superseded"].to_dict() == {"0100001": True, "0100002": False, "0100003": False, "0100004": False}
    assert inv.loc["0100003", "supersedes_invoice_number"] == "0100001"
    assert inv.loc["0100001", "pro_number"] == inv.loc["0100003", "pro_number"] == "111111111"


def test_rebills_cite_the_invoice_number_they_replace_and_chain(tmp_path, cfg):
    def row(num, ctl, kind, cites, date):
        return (f"{num},1{num},12345678,{ctl},{date},2025-03-01,Chicago,Dallas,10.00,LH,100.00,{kind},{cites},100.00\n")
    rows = [("0200010", "9000010", "original", "", "2025-03-01"), ("0200011", "9000011", "rebill", "0200010", "2025-03-15"),
            ("0200012", "9000012", "rebill", "0200011", "2025-04-01"), ("0200013", "9000013", "original", "", "2025-03-02"),
            ("0200014", "9000014", "rebill", "0209999", "2025-04-02")]          # cites an invoice that does not exist
    text = HEADER["B"] + "\n" + "".join(row(*x) for x in rows)
    receipts = [f"CARB,{ctl},{d}" for _, ctl, _, _, d in rows]
    r = run(tmp_path, cfg, raw={"CARB/2025-03.csv": text}, shipments=FOUR_SHIPMENTS, receipts=receipts)
    inv = r["invoices"].set_index("invoice_number")
    assert inv["is_superseded"].to_dict() == {"0200010": True, "0200011": True, "0200012": False,
                                              "0200013": False, "0200014": False}
    missing = exceptions_of(r, "rebill_target_not_found")
    assert len(missing) == 1 and missing.iloc[0]["raw_value"] == "0209999"


# ---------------------------------------------------------------- the full synthetic dataset


@pytest.fixture(scope="module")
def result(full, cfg):
    _, data_dir = full
    return normalize(cfg, data_dir)


def test_normalized_invoices_reproduce_what_the_generator_wrote(full, result):
    """Every parsed field equals the generator's own value for that invoice (the tests may look; the pipeline may not)."""
    tables, _ = full
    truth = tables["invoices"].set_index("invoice_id")
    inv = result["invoices"].set_index("invoice_id")
    assert set(inv.index) == set(truth.index) and inv.index.is_unique
    t = truth.loc[inv.index]
    for got, want in (("invoice_number", "invoice_number"), ("pro_number", "pro_base"), ("bol_canonical", "bol_digits"),
                      ("invoice_type", "invoice_type"), ("invoice_date", "invoice_date"), ("received_date", "received_date"),
                      ("ship_date", "ship_date"), ("billed_weight_lbs", "weight_lbs"), ("is_superseded", "superseded")):
        assert (inv[got].to_numpy() == t[want].to_numpy()).all(), got
    assert inv["total"].to_numpy() == pytest.approx(t["total"].to_numpy(), abs=CENT)
    cities = tables["lanes"]
    known = set(cities["origin_city"]) | set(cities["destination_city"])
    assert (inv["origin"] == t["origin_city"]).all() and (inv["destination"] == t["dest_city"]).all()
    assert set(inv["origin"]) | set(inv["destination"]) <= known


def test_every_invoice_passes_the_totals_check_and_lines_reproduce_the_generator(full, result):
    tables, _ = full
    inv, lines = result["invoices"], result["invoice_lines"]
    assert inv["totals_ok"].all() and (inv["lines_total"] - inv["total"]).abs().max() <= CENT
    want = tables["invoice_lines"].sort_values(["invoice_id", "charge_code"]).reset_index(drop=True)
    got = lines.sort_values(["invoice_id", "charge_code"]).reset_index(drop=True)
    assert got["invoice_id"].tolist() == want["invoice_id"].tolist()
    assert got["charge_code"].tolist() == want["charge_code"].tolist()
    assert got["amount"].to_numpy() == pytest.approx(want["amount"].to_numpy(), abs=CENT)


def test_matching_finds_the_true_shipment_and_leaves_phantoms_unmatched(full, result):
    tables, _ = full
    truth = tables["invoices"].set_index("invoice_id")["shipment_id"]
    inv = result["invoices"].set_index("invoice_id")
    counts = inv["match_method"].value_counts()
    assert counts["exact"] > 10000 and counts["fallback"] > 0 and counts["unmatched"] > 0
    matched = inv[inv["match_method"] != "unmatched"]
    assert (matched["shipment_id"] == truth.loc[matched.index]).all()           # never the wrong shipment
    phantoms = truth[truth == ""].index
    assert (inv.loc[phantoms, "match_method"] == "unmatched").all()             # a phantom is never matched
    unmatched_real = inv[(inv["match_method"] == "unmatched") & ~inv.index.isin(phantoms)]
    typos = tables["labels"].query("label == 'bol_typo'")["invoice_id"]
    assert set(unmatched_real.index) <= set(typos)     # real shipments left unmatched only had a typo'd BOL


def test_only_the_planted_unknown_charges_are_exceptions(result, cfg):
    ex = result["normalization_exceptions"]
    assert set(ex["exception_type"]) == {"unmapped_charge"} and len(ex) == cfg["traps"]["unknown_charge_lines"]
    assert set(ex["raw_value"]) == {cfg["traps"]["unknown_charge_description"]}


def test_report_counts_are_carrier_by_fix(result):
    report = result["normalization_report"]
    assert list(report.columns) == ["carrier_id", "fix_type", "unit", "count"] and (report["count"] > 0).all()
    assert not report.duplicated(["carrier_id", "fix_type", "unit"]).any()


def test_normalize_is_deterministic_and_never_needs_ground_truth(full, cfg, tmp_path):
    _, data_dir = full
    clean = tmp_path / "data"
    shutil.copytree(data_dir / "raw", clean / "raw")
    shutil.copytree(data_dir / "reference", clean / "reference")            # no ground_truth/ here
    a, b = normalize(cfg, data_dir), normalize(cfg, clean)
    for name in a:
        pd.testing.assert_frame_equal(a[name], b[name])


def test_write_normalized_round_trips_ids_as_text(result, tmp_path):
    write_normalized(result, tmp_path)
    from freight_audit_lab.csv_io import read_csv
    inv = read_csv(tmp_path / "normalized" / "invoices.csv")
    assert inv["bol_canonical"].str.fullmatch(r"\d{8}").all() and inv["bol_canonical"].str.startswith("0").any()
    assert inv["invoice_number"].str.fullmatch(r"\d{7}").all() and inv["pro_number"].str.fullmatch(r"\d{9}").all()
