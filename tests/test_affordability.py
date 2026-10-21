import pytest

from leadengine.calculators import affordability, mortgage_payment
from leadengine.config import Rates


def test_dbr_limits_the_payment():
    r = affordability(30000, 5000, 200000, 25, 4.5, rates=Rates())
    assert r["max_monthly_payment"] == 10000.0 and r["dbr_pct"] == 50.0


@pytest.mark.parametrize("income,rate,years", [(30000, 4.5, 25), (12000, 6.0, 20), (80000, 3.0, 30), (20000, 0.0, 15)])
def test_inverse_of_mortgage_payment(income, rate, years):
    r = affordability(income, 0, 100000, years, rate, rates=Rates())
    if rate == 0:
        assert r["max_loan"] == round(r["max_monthly_payment"] * years * 12, 2)
    else:
        assert abs(mortgage_payment(r["max_loan"], rate, years) - r["max_monthly_payment"]) < 0.05


def test_debts_above_the_limit_floor_at_zero():
    r = affordability(30000, 20000, 150000, 25, 4.5, rates=Rates())
    assert r["max_monthly_payment"] == 0.0 and r["max_loan"] == 0.0 and r["max_price"] == 150000.0


def test_ltv_cap():
    low = affordability(100000, 0, 10000, 25, 4.5, rates=Rates())
    assert low["limited_by"] == "ltv" and low["ltv_capped_price"] == round(10000 / 0.2, 2) < low["max_price"]
    high = affordability(10000, 0, 2_000_000, 25, 4.5, rates=Rates())
    assert high["limited_by"] == "dbr" and high["ltv_capped_price"] == high["max_price"]


def test_custom_dbr_from_rates(monkeypatch):
    assert affordability(30000, 5000, 0, 25, 4.5, rates=Rates(dbr_pct=40))["max_monthly_payment"] == 7000.0
    monkeypatch.setenv("DBR_PCT", "35")
    assert Rates.from_env().dbr_pct == 35.0


@pytest.mark.parametrize("args", [
    (0, 0, 0, 25, 4.5), (-1, 0, 0, 25, 4.5), (1000, -1, 0, 25, 4.5), (1000, 0, -1, 25, 4.5), (1000, 0, 0, 0, 4.5), (1000, 0, 0, 36, 4.5),
    (1000, 0, 0, 25, -1), (1000, 0, 0, 25, 31), (float("nan"), 0, 0, 25, 4.5), (1000, float("inf"), 0, 25, 4.5), (1e11, 0, 0, 25, 4.5),
    (1000, 0, 1e11, 25, 4.5), (1000, 0, 0, 25, float("nan")), (1000, 0, 0, 2.5, 4.5), ("1000", 0, 0, 25, 4.5)])
def test_validation(args):
    with pytest.raises(ValueError):
        affordability(*args, rates=Rates())
