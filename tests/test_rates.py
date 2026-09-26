"""Hand-computed pricing and FSC cases. Every expected value is worked out in the comment."""

import pandas as pd
import pytest

from freight_audit_lab.contract import (contract_linehaul, fsc_ltl_amount, fsc_ltl_pct,
                                              fsc_tl_amount, lookup_rate, ship_week)

CENT = 0.01

LTL_ROW = pd.Series({
    "mode": "LTL", "min_charge": 110.0, "rate_per_mile": float("nan"),
    "deficit_weight_rating": True,
    "cwt_0_499": 30.00, "cwt_500_999": 25.00, "cwt_1000_1999": 20.00,
    "cwt_2000_4999": 17.00, "cwt_5000_7500": 14.40,
})
TL_ROW = pd.Series({"mode": "TL", "min_charge": 450.0, "rate_per_mile": 2.40,
                    "deficit_weight_rating": False})

FSC_CFG = {"fsc": {"ltl": {"base_price": 1.20, "step": 0.10, "pct_per_step": 0.009},
                   "tl": {"peg_price": 1.25, "mpg": 6.0}}}


@pytest.mark.parametrize("weight, expected", [
    (200, 110.00),    # 30.00 x 2 = 60.00; deficit at 500 lb = 125.00; min charge 110 wins
    (700, 175.00),    # 25.00 x 7 = 175.00; deficit at 1,000 lb = 200.00 is dearer
    (1000, 200.00),   # exactly on the break: 20.00 x 10
    (1500, 300.00),   # 20.00 x 15 = 300.00; deficit at 2,000 lb = 340.00 is dearer
    (3000, 510.00),   # 17.00 x 30
    (8000, 1152.00),  # above the last break's max: last break still applies, 14.40 x 80
])
def test_ltl_breaks_and_min_charge(weight, expected):
    assert contract_linehaul(LTL_ROW, weight, miles=500) == pytest.approx(expected, abs=CENT)


def test_ltl_deficit_weight_rating():
    # 950 lb at 25.00 = 237.50, but billing it as 1,000 lb at 20.00 = 200.00 is cheaper.
    assert contract_linehaul(LTL_ROW, 950, 500) == pytest.approx(200.00, abs=CENT)
    literal = LTL_ROW.copy()
    literal["deficit_weight_rating"] = False
    assert contract_linehaul(literal, 950, 500) == pytest.approx(237.50, abs=CENT)


def test_ltl_rounds_to_cents():
    # 20.00 x 12.345 = 246.90
    assert contract_linehaul(LTL_ROW, 1234.5, 500) == pytest.approx(246.90, abs=CENT)


@pytest.mark.parametrize("miles, expected", [
    (150, 450.00),   # 2.40 x 150 = 360.00 < TL minimum 450
    (800, 1920.00),  # 2.40 x 800
])
def test_tl_linehaul_and_minimum(miles, expected):
    assert contract_linehaul(TL_ROW, 40000, miles) == pytest.approx(expected, abs=CENT)


def _amended_card():
    base = {"carrier_id": "CARA", "lane_id": "L001", "mode": "TL", "min_charge": 450.0,
            "deficit_weight_rating": False}
    return pd.DataFrame([
        {**base, "version": 1, "rate_per_mile": 2.40,
         "effective_from": pd.Timestamp("2025-01-01"), "effective_to": pd.Timestamp("2025-06-30")},
        {**base, "version": 2, "rate_per_mile": 2.28,
         "effective_from": pd.Timestamp("2025-07-01"), "effective_to": pd.Timestamp("2099-12-31")},
    ])


def test_lookup_either_side_of_amendment():
    card = _amended_card()
    assert lookup_rate(card, "CARA", "L001", "2025-06-30")["version"] == 1
    assert lookup_rate(card, "CARA", "L001", "2025-07-01")["version"] == 2
    assert lookup_rate(card, "CARA", "L001", "2025-12-15")["rate_per_mile"] == 2.28


def test_lookup_fails_outside_contract():
    with pytest.raises(LookupError):
        lookup_rate(_amended_card(), "CARA", "L001", "2024-12-31")


@pytest.mark.parametrize("diesel, pct, lh_1000_fsc", [
    (1.10, 0.000, 0.00),     # below base price: no surcharge
    (3.30, 0.189, 189.00),   # (3.30-1.20)/0.10 = 21 steps (float gives 20.999..; guarded)
    (3.70, 0.225, 225.00),   # 25 steps x 0.9%
    (4.849, 0.324, 324.00),  # 36.49 -> 36 steps x 0.9%
])
def test_fsc_ltl(diesel, pct, lh_1000_fsc):
    assert fsc_ltl_pct(diesel, FSC_CFG) == pytest.approx(pct, abs=1e-9)
    assert fsc_ltl_amount(1000.0, diesel, FSC_CFG) == pytest.approx(lh_1000_fsc, abs=CENT)


@pytest.mark.parametrize("diesel, expected", [
    (1.00, 0.00),     # below peg: no surcharge
    (3.70, 408.33),   # (3.70-1.25)/6 = 0.408333 $/mi x 1,000 mi
    (4.85, 600.00),   # (4.85-1.25)/6 = 0.60 $/mi x 1,000 mi
])
def test_fsc_tl(diesel, expected):
    assert fsc_tl_amount(1000, diesel, FSC_CFG) == pytest.approx(expected, abs=CENT)


def test_ship_week_is_monday_on_or_before():
    assert ship_week("2025-03-05") == pd.Timestamp("2025-03-03")  # Wednesday -> Monday
    assert ship_week("2025-03-03") == pd.Timestamp("2025-03-03")  # Monday stays
    assert ship_week("2025-03-09") == pd.Timestamp("2025-03-03")  # Sunday -> prior Monday
