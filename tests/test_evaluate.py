"""Evaluation metrics on a tiny hand-labeled example, then a consistency check on the full data."""

import pandas as pd
import pytest

from freight_audit_lab.evaluate import (NO_TRAP, baseline_fp_causes, evaluate, fp_cause, load_labels, metrics, outcomes,
                                        write_evaluation)

CENT = 0.01
LABEL_COLUMNS = ["invoice_id", "label_kind", "label", "mode", "true_dollar_impact"]
FLAG_COLUMNS = ["invoice_id", "error_type", "dollar_impact_estimate", "counted_in_recoverable"]

LABELS = pd.DataFrame([
    ("I1", "error", "rate_overcharge", "markup", 100.0),
    ("I2", "error", "rate_overcharge", "sub_tolerance", 5.0),
    ("I3", "error", "duplicate_invoice", "resend", 200.0),
    ("I4", "clean", "clean", "", 0.0),
    ("I5", "trap", "rate_amendment", "", 0.0),
    ("I6", "superseded", "superseded", "", 0.0),
    ("I7", "error", "weight_overbilling", "inflation", 40.0),
    ("I7", "trap", "documented_reweigh", "", 0.0)], columns=LABEL_COLUMNS)

ENGINE = pd.DataFrame([
    ("I1", "rate_overcharge", 100.0, True),        # TP
    ("I3", "duplicate_invoice", 200.0, True),      # TP
    ("I3", "rate_overcharge", 30.0, False),        # FP, kept for information so worth $0 in flagged $
    ("I5", "rate_overcharge", 25.0, True),         # FP on the rate-amendment trap
    ("I6", "rate_overcharge", 50.0, True),         # on a superseded invoice: not scored at all
    ("I7", "weight_overbilling", 40.0, True)],     # TP
    columns=FLAG_COLUMNS)

BASELINE = pd.DataFrame([("I4", "rate_overcharge", 10.0, True)], columns=FLAG_COLUMNS)    # FP on a clean invoice


def by_type(result, system):
    return result["by_type"].query("system == @system").set_index("error_type")


def test_counts_precision_recall_and_dollars_are_right_on_a_hand_labeled_example():
    result = evaluate(ENGINE, BASELINE, LABELS)
    rate = by_type(result, "engine").loc["rate_overcharge"]
    assert (rate["flags"], rate.tp, rate.fp, rate.fn) == (3, 1, 2, 1)
    assert rate.precision == pytest.approx(1 / 3) and rate.recall == pytest.approx(1 / 2)
    assert rate.f1 == pytest.approx(0.4)                                   # 2PR / (P + R) = 0.4
    assert rate.flagged_dollars_estimate == pytest.approx(125.00, abs=CENT)    # 100 + 25; the $30 flag is not counted
    assert rate.true_dollars == pytest.approx(105.00, abs=CENT) and rate.tp_dollars == pytest.approx(100.00, abs=CENT)
    assert rate.dollar_recall == pytest.approx(100 / 105)
    dup = by_type(result, "engine").loc["duplicate_invoice"]
    assert (dup.tp, dup.fp, dup.fn, dup.precision, dup.recall) == (1, 0, 0, 1.0, 1.0)


def test_all_row_sums_the_types_and_superseded_invoices_are_excluded():
    engine = by_type(evaluate(ENGINE, BASELINE, LABELS), "engine")
    total = engine.loc["ALL"]
    assert (total["flags"], total.tp, total.fp, total.fn) == (5, 3, 2, 1)     # I6's flag is not in the 5
    assert set(outcomes(ENGINE, LABELS)["invoice_id"]).isdisjoint({"I6"})


def test_a_type_with_no_flags_and_no_errors_has_no_precision_instead_of_a_fake_number():
    row = by_type(evaluate(ENGINE, BASELINE, LABELS), "engine").loc["phantom_invoice"]
    assert row["flags"] == 0 and pd.isna(row.precision) and pd.isna(row.recall) and pd.isna(row.f1)


def test_recall_by_mode_shows_the_sub_tolerance_miss():
    modes = evaluate(ENGINE, BASELINE, LABELS)["by_mode"].set_index(["error_type", "mode"])
    assert modes.loc[("rate_overcharge", "markup"), "engine_recall"] == 1.0
    sub = modes.loc[("rate_overcharge", "sub_tolerance")]
    assert (sub.n_labeled, sub.engine_detected, sub.engine_recall) == (1, 0, 0.0)
    assert modes.loc[("rate_overcharge", "markup"), "baseline_recall"] == 0.0     # the baseline flagged only I4


def test_trap_table_counts_false_positives_on_trap_and_clean_invoices():
    traps = evaluate(ENGINE, BASELINE, LABELS)["traps"].set_index("trap")
    assert (traps.loc["rate_amendment", "n_invoices"], traps.loc["rate_amendment", "engine_fp"]) == (1, 1)
    assert traps.loc["rate_amendment", "engine_fp_by_type"] == "rate_overcharge:1"
    assert traps.loc["documented_reweigh", "engine_fp"] == 0          # the weight flag on I7 is a true positive
    assert (traps.loc[NO_TRAP, "n_invoices"], traps.loc[NO_TRAP, "engine_fp"], traps.loc[NO_TRAP, "baseline_fp"]) == (1, 0, 1)


def test_outcomes_can_be_limited_to_a_scope_of_invoices():
    o = outcomes(ENGINE, LABELS, scope={"I1", "I2"})
    assert metrics(o[o["error_type"] == "rate_overcharge"])["tp"] == 1 and set(o["invoice_id"]) == {"I1", "I2"}


def test_outputs_are_written(tmp_path):
    write_evaluation(evaluate(ENGINE, BASELINE, LABELS), tmp_path)
    assert {p.name for p in tmp_path.iterdir()} == {"eval_by_type.csv", "eval_by_mode.csv", "eval_traps.csv",
                                                   "eval_engine_vs_baseline.csv", "baseline_fp_causes.csv"}


def test_each_false_flag_gets_one_cause_and_the_precedence_is_the_documented_one():
    none = set()
    assert fp_cause("rate_overcharge", {"rate_amendment", "documented_reweigh"}, {"weight_overbilling"}, False) == "rate_amendment"
    assert fp_cause("rate_overcharge", {"documented_reweigh"}, {"weight_overbilling"}, False) == "reweigh"
    assert fp_cause("rate_overcharge", {"bol_format"}, {"weight_overbilling"}, False) == "weight_misread_as_rate"
    assert fp_cause("weight_overbilling", {"documented_reweigh", "rate_amendment"}, none, False) == "reweigh"
    assert fp_cause("unauthorized_accessorial", {"late_authorization", "bol_format"}, none, False) == "late_authorization"
    assert fp_cause("duplicate_invoice", {"balance_due"}, none, False) == "rebill_balance_due"
    assert fp_cause("phantom_invoice", {"bol_typo"}, none, False) == "bol_typo_or_zero"
    assert fp_cause("phantom_invoice", {"bol_format"}, none, True) == "bol_typo_or_zero"
    # the rule that raised the flag matters: a late-authorization invoice's rate flag is not a late authorization
    assert fp_cause("rate_overcharge", {"late_authorization"}, none, False) == "other"
    assert fp_cause("phantom_invoice", {"bol_format"}, none, False) == "other"       # spaces, dashes, ... are cleaned by the baseline


def test_cause_table_adds_up_to_the_false_flags_where_the_trap_table_double_counts():
    labels = pd.concat([LABELS, pd.DataFrame([("I5", "trap", "bol_format", "spaces", 0.0),
                                              ("I5", "trap", "documented_reweigh", "", 0.0)], columns=LABEL_COLUMNS)],
                       ignore_index=True)
    flags = pd.DataFrame([("I5", "rate_overcharge", 25.0, True), ("I4", "rate_overcharge", 10.0, True)], columns=FLAG_COLUMNS)
    result = evaluate(ENGINE, flags, labels)
    table = result["baseline_fp_causes"].set_index("cause")
    assert table["false_flags"].sum() == 2 and table.loc["rate_amendment", "false_flags"] == 1 and table.loc["other", "false_flags"] == 1
    assert table["share_of_false_flags"].sum() == pytest.approx(1.0)
    traps = result["traps"].set_index("trap")                                    # I5 is counted under all three of its traps
    assert sum(traps.loc[t, "baseline_fp"] for t in ("rate_amendment", "bol_format", "documented_reweigh")) == 3


def test_cause_table_is_empty_but_well_formed_when_the_baseline_raises_no_false_flags():
    table = baseline_fp_causes(LABELS, outcomes(ENGINE.iloc[0:1], LABELS))
    assert table["false_flags"].sum() == 0 and list(table["cause"].iloc[-1:]) == ["other"] and table["share_of_false_flags"].eq(0).all()


# ---------------------------------------------------------------- on the full generated data


@pytest.fixture(scope="module")
def scored(audited, full):
    labels = load_labels(full[1])
    baseline = audited["baseline_flags"]
    return evaluate(audited["audit_flags"], baseline, labels), labels


def test_engine_only_misses_errors_injected_below_tolerance_or_worth_pennies(scored):
    """Every missed rate or weight error is a sub_tolerance one; fuel misses are sub_tolerance or under the dollar floor."""
    result, labels = scored
    modes = result["by_mode"].set_index(["error_type", "mode"])
    for key in [("rate_overcharge", "markup"), ("weight_overbilling", "inflation"), ("fsc_mismatch", "wrong_table")]:
        assert modes.loc[key, "engine_recall"] == 1.0
    assert modes.loc[("rate_overcharge", "sub_tolerance"), "engine_detected"] == 0
    assert modes.loc[("weight_overbilling", "sub_tolerance"), "engine_detected"] == 0
    missed_dollars = modes["true_dollars"] * (1 - modes["engine_dollar_recall"].fillna(1))
    assert missed_dollars.sum() < 0.01 * modes["true_dollars"].sum()


def test_engine_beats_baseline_on_precision_and_leaves_traps_alone(scored):
    result, _ = scored
    eng, base = by_type(result, "engine").loc["ALL"], by_type(result, "baseline").loc["ALL"]
    assert eng.precision > 0.99 and base.precision < 0.6
    traps = result["traps"].set_index("trap")
    assert traps["engine_fp"].sum() <= 3 and traps["baseline_fp"].sum() > 100
    assert traps.loc[NO_TRAP, "engine_fp"] == 0


def test_baseline_false_flag_causes_add_up_to_the_baseline_false_flags(scored):
    """The trap table sums to more than the false flags (overlapping traps); the cause table must not."""
    result, _ = scored
    causes = result["baseline_fp_causes"]
    total_fp = int(by_type(result, "baseline").loc["ALL", "fp"])
    assert causes["false_flags"].sum() == total_fp
    assert result["traps"]["baseline_fp"].sum() > total_fp                # the overlap the cause table removes
    assert causes["share_of_false_flags"].sum() == pytest.approx(1.0, abs=1e-6)
    assert causes["cause"].iloc[-1] == "other" and causes.loc[causes["cause"] == "bol_typo_or_zero", "false_flags"].iloc[0] > 0


def test_flag_and_label_counts_reconcile_with_the_answer_key(scored):
    result, labels = scored
    live = labels[~labels["invoice_id"].isin(labels.loc[labels["label_kind"] == "superseded", "invoice_id"])]
    truth = live[live["label_kind"] == "error"].drop_duplicates(["invoice_id", "label"])
    eng = by_type(result, "engine").loc["ALL"]
    assert eng.tp + eng.fn == len(truth)
