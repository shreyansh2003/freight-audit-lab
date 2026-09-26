"""Stage 2: canonical invoices, traps, errors, and the answer key."""

import hashlib

import pandas as pd
import pytest

from freight_audit_lab.config import load_config
from freight_audit_lab.contract import (contract_linehaul, diesel_for_ship_date, fsc_ltl_amount,
                                        fsc_tl_amount, lookup_rate)
from freight_audit_lab.generate import generate_all
from freight_audit_lab.generate.traps import noisy_bol, transpose_bol, BOL_PATTERNS

CENT = 0.01
ERROR_TYPES = ["duplicate_invoice", "rate_overcharge", "fsc_mismatch", "unauthorized_accessorial",
               "weight_overbilling", "phantom_invoice"]


def tree_hashes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*.csv"))}


def test_same_seed_identical_raw_files_and_labels(tmp_path):
    cfg = load_config()
    cfg["shipments"]["n"] = 1500                       # smaller run keeps this test quick
    generate_all(cfg, tmp_path / "a")
    generate_all(cfg, tmp_path / "b")
    a, b = tree_hashes(tmp_path / "a"), tree_hashes(tmp_path / "b")
    assert any(k.startswith("raw/") for k in a) and "ground_truth/labels.csv" in a
    assert a == b


def test_every_labeled_error_has_real_impact(full):
    tables, _ = full
    errors = tables["labels"].query("label_kind == 'error'")
    assert len(errors) > 0
    assert (errors["true_dollar_impact"] > CENT).all()
    traps = tables["labels"].query("label_kind != 'error'")
    assert (traps["true_dollar_impact"] == 0).all()


def test_invoice_counts_reconcile(full, cfg):
    tables, _ = full
    inv, labels = tables["invoices"], tables["labels"]
    n_ship = len(tables["shipments"])
    count = labels.query("label_kind != 'clean'").groupby("label")["invoice_id"].nunique()
    n_dup, n_phantom = count["duplicate_invoice"], count["phantom_invoice"]
    by_type = inv["invoice_type"].value_counts()
    assert by_type["original"] == n_ship + n_dup + n_phantom      # originals = shipments, + resends, + phantoms
    assert by_type["rebill"] == count["rebill"] == count["superseded"]
    assert by_type["balance_due"] == count["balance_due"]
    assert len(inv) == n_ship + n_dup + n_phantom + by_type["rebill"] + by_type["balance_due"]
    assert inv["invoice_id"].is_unique
    assert inv["control_id"].is_unique
    assert set(labels["invoice_id"]) == set(inv["invoice_id"])


def test_receipt_log_covers_every_invoice_within_runoff_window(full, cfg):
    tables, _ = full
    log, inv = tables["ap_receipt_log"], tables["invoices"]
    start = pd.Timestamp(cfg["period"]["start"])
    runoff_end = (start + pd.DateOffset(months=cfg["period"]["months"])
                  + pd.Timedelta(days=cfg["period"]["runoff_days"] - 1))
    assert runoff_end == pd.Timestamp(cfg["period"]["audit_as_of"])
    assert len(log) == len(inv) and log["control_id"].is_unique
    assert (log["received_date"] >= start).all() and (log["received_date"] <= runoff_end).all()
    assert (inv["invoice_date"] <= inv["received_date"]).all()


def _expected(cfg, invoices, frame):
    """Expected count of an error type: config rate x carrier multiplier, summed over eligible invoices."""
    mult = {c["id"]: c["error_multiplier"] for c in cfg["carriers"]}
    return frame["carrier_id"].map(mult).sum()


@pytest.mark.parametrize("error_type, low, high", [
    ("rate_overcharge", 0.6, 1.4), ("unauthorized_accessorial", 0.6, 1.4),
    ("weight_overbilling", 0.6, 1.4), ("duplicate_invoice", 0.5, 1.5), ("phantom_invoice", 0.5, 1.5)])
def test_observed_injection_rate_is_near_config(full, cfg, error_type, low, high):
    tables, _ = full
    inv = tables["invoices"].merge(tables["shipments"][["shipment_id", "mode"]], on="shipment_id", how="left")
    labels = tables["labels"]
    errored = set(labels.query("label_kind == 'error' and label != 'duplicate_invoice'")["invoice_id"])
    originals = inv[(inv["invoice_type"] == "original") & inv["shipment_id"].ne("")]
    originals = originals.drop_duplicates("shipment_id")           # the first copy is the original
    live = originals[~originals["superseded"]]
    eligible = {"weight_overbilling": live[live["mode"] == "LTL"],
                "duplicate_invoice": live[~live["invoice_id"].isin(errored)],
                "phantom_invoice": originals}.get(error_type, live)
    expected = cfg["errors"][error_type] * _expected(cfg, inv, eligible)
    observed = (labels["label"] == error_type).sum()
    assert low * expected <= observed <= high * expected, (observed, expected)


def test_systemic_wrong_table_hits_every_carf_original_in_its_window(full, cfg):
    """CARF bills fuel at 5.5 mpg instead of 6.0 on every original shipped Aug 1 - Oct 31, 2025."""
    tables, _ = full
    s = cfg["errors"]["systemic_issues"][0]
    assert (s["carrier"], s["mode"], s["rate"]) == ("CARF", "wrong_table", 1.0)
    assert cfg["errors"]["magnitudes"]["fsc_tl_wrong_table_mpg"] == 5.5 and cfg["fsc"]["tl"]["mpg"] == 6.0
    inv, lines = tables["invoices"], tables["invoice_lines"]
    window = inv["ship_date"].between(pd.Timestamp(s["start"]), pd.Timestamp(s["end"]))
    mine = inv[(inv["carrier_id"] == s["carrier"]) & window & (inv["invoice_type"] == "original")
               & inv["shipment_id"].ne("") & ~inv["superseded"]]      # a rebill carries contract charges
    hit = set(tables["labels"].query("label == 'fsc_mismatch' and mode == 'wrong_table'")["invoice_id"])
    assert len(mine) > 100 and set(mine["invoice_id"]) <= hit
    outside = inv[(inv["carrier_id"] == s["carrier"]) & ~window]
    assert not hit & set(outside["invoice_id"])                        # nothing leaks outside the window
    # the billed FSC is the 5.5-mpg amount (skipping rounding noise, which moves FSC by design)
    noisy = set(tables["labels"].query("label == 'rounding_noise'")["invoice_id"])
    fsc = lines[lines["charge_code"] == "FSC"].set_index("invoice_id")["amount"]
    shp = tables["shipments"].set_index("shipment_id")
    for row in mine[~mine["invoice_id"].isin(noisy)].itertuples():
        diesel = diesel_for_ship_date(tables["diesel_weekly"], row.ship_date)
        want = fsc_tl_amount(shp.loc[row.shipment_id, "miles"], diesel, cfg,
                             mpg=cfg["errors"]["magnitudes"]["fsc_tl_wrong_table_mpg"])
        assert fsc[row.invoice_id] == pytest.approx(want, abs=CENT), row.invoice_id


def test_random_wrong_week_errors_still_occur(full):
    labels = full[0]["labels"].query("label == 'fsc_mismatch'")
    assert (labels["mode"] == "wrong_week").sum() > 10


class TrueTotals:
    """Contract price of a shipment at its reference weight, plus its authorized accessorials."""

    def __init__(self, tables, cfg):
        self.tables, self.cfg = tables, cfg
        self.shp = tables["shipments"].set_index("shipment_id")
        self.cert = tables["reweigh_certificates"].set_index("shipment_id")["certified_weight_lbs"]
        self.acc = tables["authorizations"].groupby("shipment_id")["authorized_amount"].sum()

    def linehaul_and_fsc(self, shipment_id):
        s = self.shp.loc[shipment_id]
        weight = int(self.cert.get(shipment_id, s["weight_lbs"]))
        rate = lookup_rate(self.tables["rate_card"], s["carrier_id"], s["lane_id"], s["ship_date"])
        lh = contract_linehaul(rate, weight, s["miles"])
        diesel = diesel_for_ship_date(self.tables["diesel_weekly"], s["ship_date"])
        fsc = (fsc_ltl_amount(lh, diesel, self.cfg) if s["mode"] == "LTL"
               else fsc_tl_amount(s["miles"], diesel, self.cfg))
        return lh + fsc

    def total(self, shipment_id):
        return self.linehaul_and_fsc(shipment_id) + self.acc.get(shipment_id, 0.0)


def test_billed_minus_true_equals_labeled_impact(full, cfg):
    """Re-price every invoice from the contract alone: the gap is exactly the labeled impact.

    Skips invoices with rounding noise (deliberate, unlabeled dollars) and shipments whose
    authorized accessorial is billed on a separate balance-due invoice.
    """
    tables, _ = full
    inv, labels, lines = tables["invoices"], tables["labels"], tables["invoice_lines"]
    billed = lines.groupby("invoice_id")["amount"].sum()
    errors = labels.query("label_kind == 'error' and label not in ['duplicate_invoice', 'phantom_invoice']")
    impact = errors.groupby("invoice_id")["true_dollar_impact"].sum()
    noisy = set(labels.query("label == 'rounding_noise'")["invoice_id"])
    split = set(inv.query("invoice_type == 'balance_due'")["shipment_id"])
    firsts = inv[inv["invoice_type"].isin(["original", "rebill"]) & inv["shipment_id"].ne("")
                 & ~inv["superseded"]]
    firsts = firsts.drop_duplicates(["shipment_id", "invoice_type"])       # skip resent duplicates
    truth = TrueTotals(tables, cfg)
    checked = with_impact = 0
    for row in firsts.itertuples():
        if row.invoice_id in noisy or row.shipment_id in split:
            continue
        gap = billed[row.invoice_id] - truth.total(row.shipment_id)
        assert gap == pytest.approx(impact.get(row.invoice_id, 0.0), abs=2 * CENT), row.invoice_id
        checked += 1
        with_impact += row.invoice_id in impact.index
    assert checked > 5000 and with_impact > 500


def test_duplicates_repeat_an_original(full):
    tables, _ = full
    inv, labels = tables["invoices"], tables["labels"]
    dups = labels.query("label == 'duplicate_invoice'")
    modes = dups["mode"].value_counts()
    assert set(modes.index) == {"resend", "rekeyed"} and modes.min() > 10
    lines = tables["invoice_lines"]
    total = lines.groupby("invoice_id")["amount"].sum()
    for d in dups.itertuples():
        row = inv.set_index("invoice_id").loc[d.invoice_id]
        twins = inv[(inv["shipment_id"] == row["shipment_id"]) & (inv["invoice_type"] == "original")]
        assert len(twins) == 2
        first = twins.sort_values("received_date").iloc[0]
        lag = (row["received_date"] - first["received_date"]).days
        assert 5 <= lag <= 45
        assert d.true_dollar_impact == pytest.approx(total[d.invoice_id], abs=CENT)
        assert (row["invoice_number"] == first["invoice_number"]) == (d.mode == "resend")


def test_rebill_supersedes_and_balance_due_bills_an_authorized_accessorial(full):
    tables, _ = full
    inv, lines, auths = tables["invoices"], tables["invoice_lines"], tables["authorizations"]
    for r in inv[inv["invoice_type"] == "rebill"].itertuples():
        orig = inv[(inv["shipment_id"] == r.shipment_id) & (inv["invoice_type"] == "original")]
        assert len(orig) == 1 and orig.iloc[0]["superseded"]
        assert 10 <= (r.received_date - orig.iloc[0]["received_date"]).days <= 30
    for r in inv[inv["invoice_type"] == "balance_due"].itertuples():
        own = lines[lines["invoice_id"] == r.invoice_id]
        assert len(own) == 1 and own.iloc[0]["charge_code"] not in ("LH", "FSC")
        auth = auths[(auths["shipment_id"] == r.shipment_id) & (auths["code"] == own.iloc[0]["charge_code"])]
        assert len(auth) == 1 and auth.iloc[0]["authorized_amount"] == pytest.approx(own.iloc[0]["amount"], abs=CENT)
        orig = inv[(inv["shipment_id"] == r.shipment_id) & (inv["invoice_type"] == "original")]
        assert own.iloc[0]["charge_code"] not in set(lines[lines["invoice_id"] == orig.iloc[0]["invoice_id"]]["charge_code"])


def test_late_authorization_is_after_invoice_date_but_before_audit_as_of(full, cfg):
    tables, _ = full
    inv, auths = tables["invoices"].set_index("invoice_id"), tables["authorizations"]
    ship = tables["shipments"].set_index("shipment_id")
    late = tables["labels"].query("label == 'late_authorization'")
    assert len(late) > 100
    audit_as_of = pd.Timestamp(cfg["period"]["audit_as_of"])
    late_keys = set()
    for r in late.itertuples():
        shipment = inv.loc[r.invoice_id, "shipment_id"]
        late_keys.add((shipment, r.mode.upper()))
        auth = auths[(auths["shipment_id"] == shipment) & (auths["code"] == r.mode.upper())].iloc[0]
        assert inv.loc[r.invoice_id, "invoice_date"] < auth["authorized_at"] <= audit_as_of
    normal = auths[[(s, c) not in late_keys for s, c in zip(auths["shipment_id"], auths["code"])]]
    assert (normal["authorized_at"].to_numpy() >= ship.loc[normal["shipment_id"], "ship_date"].to_numpy()).all()
    assert (normal["authorized_at"].to_numpy() <= ship.loc[normal["shipment_id"], "delivery_date"].to_numpy()).all()


def test_documented_reweigh_bills_certified_weight(full):
    tables, _ = full
    cert = tables["reweigh_certificates"].set_index("shipment_id")["certified_weight_lbs"]
    inv = tables["invoices"]
    hit = tables["labels"].query("label == 'documented_reweigh'").merge(inv, on="invoice_id")
    assert len(hit) == len(cert)
    assert (hit["weight_lbs"].to_numpy() == cert.loc[hit["shipment_id"]].to_numpy()).all()


def test_bol_noise_traps(full):
    tables, _ = full
    inv = tables["invoices"].set_index("invoice_id")
    bols = tables["shipments"].set_index("shipment_id")["bol"]
    real = set(bols)
    fmt = tables["labels"].query("label == 'bol_format'")
    assert fmt["mode"].isin(BOL_PATTERNS).all() and fmt["mode"].nunique() == len(BOL_PATTERNS)
    for r in fmt.itertuples():
        row = inv.loc[r.invoice_id]
        digits = "".join(ch for ch in row["bol_raw"] if ch.isdigit()).zfill(8)
        assert row["bol_raw"] != row["bol_digits"] and digits == row["bol_digits"]
    for r in tables["labels"].query("label == 'bol_typo'").itertuples():
        row = inv.loc[r.invoice_id]
        true = bols[row["shipment_id"]]
        diffs = [i for i in range(8) if true[i] != row["bol_digits"][i]]
        assert len(diffs) == 2 and diffs[1] == diffs[0] + 1
        assert true[diffs[0]] == row["bol_digits"][diffs[1]] and row["bol_digits"] not in real


def test_phantoms_match_no_shipment_even_by_fallback(full, cfg):
    tables, _ = full
    fb = cfg["normalization"]["fallback_match"]
    inv = tables["invoices"].set_index("invoice_id")
    shp = tables["shipments"].merge(tables["lanes"][["lane_id", "origin_city", "destination_city"]], on="lane_id")
    phantoms = tables["labels"].query("label == 'phantom_invoice'")["invoice_id"]
    assert len(phantoms) > 20
    for pid in phantoms:
        row = inv.loc[pid]
        assert row["shipment_id"] == "" and row["bol_digits"] not in set(shp["bol"])
        near = shp[(shp["carrier_id"] == row["carrier_id"]) & (shp["origin_city"] == row["origin_city"])
                   & (shp["destination_city"] == row["dest_city"])
                   & ((shp["ship_date"] - row["ship_date"]).abs() <= pd.Timedelta(days=fb["ship_date_days"]))
                   & ((shp["weight_lbs"] - row["weight_lbs"]).abs() <= fb["weight_pct"] * row["weight_lbs"])]
        assert near.empty, pid


def test_rounding_noise_stays_within_pct_of_the_contract_amount(full, cfg):
    tables, _ = full
    inv, labels = tables["invoices"].set_index("invoice_id"), tables["labels"]
    tol = cfg["traps"]["rounding_noise_pct"]
    split = set(inv.query("invoice_type == 'balance_due'")["shipment_id"])
    has_error = set(labels.query("label_kind == 'error'")["invoice_id"])
    noisy = [r for r in labels.query("label == 'rounding_noise'").itertuples()
             if r.invoice_id not in has_error and inv.loc[r.invoice_id, "shipment_id"] not in split
             and inv.loc[r.invoice_id, "invoice_type"] == "original"]
    assert len(noisy) > 500
    truth, nonzero = TrueTotals(tables, cfg), 0
    for r in noisy[:400]:
        shipment = inv.loc[r.invoice_id, "shipment_id"]
        gap = inv.loc[r.invoice_id, "total"] - truth.total(shipment)
        assert abs(gap) <= tol * truth.linehaul_and_fsc(shipment) + 2 * CENT, r.invoice_id
        nonzero += abs(gap) > 0
    assert nonzero > 0.9 * len(noisy[:400])


def test_transposition_helper_never_hits_a_real_bol():
    assert transpose_bol("12345678", {"21345678"}, 0.0) != "21345678"
    assert transpose_bol("11111111", set(), 0.5) is None
    assert noisy_bol("00482913", "zeros_dropped") == "482913"
