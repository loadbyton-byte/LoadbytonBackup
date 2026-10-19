import pytest
from fastapi.testclient import TestClient

from leadengine.api import create_app
from leadengine.calculators import deal_costs, max_loan, mortgage_payment, rental_yield
from leadengine.config import Rates, Settings
from leadengine.db import DB


def test_deal_costs_1m_cash():
    c = deal_costs(1_000_000, Rates())
    assert (c["dld"], c["dld_admin"], c["agency"], c["agency_vat"], c["mortgage_registration"]) == (40_000, 580, 20_000, 1_000, 0)
    assert c["total_costs"] == 61_580


def test_deal_costs_with_loan_adds_registration():
    c = deal_costs(1_000_000, Rates(), loan=800_000)
    assert c["mortgage_registration"] == 2_000 + 290 and c["total_costs"] == 61_580 + 2_290


def test_rates_are_configurable():
    c = deal_costs(1_000_000, Rates(dld_pct=2.0, agency_pct=0.0, dld_admin_fee=0.0))
    assert c["dld"] == 20_000 and c["agency"] == 0 and c["total_costs"] == 20_000


def test_mortgage_payment_known_value_and_zero_rate():
    assert mortgage_payment(800_000, 4.5, 25) == pytest.approx(4446.6, abs=0.5)
    assert mortgage_payment(120_000, 0, 10) == 1000


def test_yield():
    y = rental_yield(1_000_000, 70_000, service_charge=10_000, vacancy_pct=5)
    assert y["gross_pct"] == 7.0 and y["net_pct"] == pytest.approx(5.65, abs=0.005)


def test_max_loan():
    assert max_loan(1_000_000, ltv_pct=80) == 800_000


@pytest.mark.parametrize("call", [
    lambda: deal_costs(0, Rates()), lambda: deal_costs(-5, Rates()), lambda: deal_costs(1e6, Rates(), loan=-1),
    lambda: deal_costs(1e6, Rates(), loan=2e6),
    lambda: mortgage_payment(0, 4, 25), lambda: mortgage_payment(1e5, -1, 25), lambda: mortgage_payment(1e5, 4, 0),
    lambda: rental_yield(0, 1000), lambda: rental_yield(1e6, -1), lambda: rental_yield(1e6, 1000, vacancy_pct=120),
    lambda: max_loan(1e6, ltv_pct=101), lambda: max_loan(0, ltv_pct=80),
])
def test_invalid_inputs_raise_valueerror(call):
    with pytest.raises(ValueError):
        call()


@pytest.fixture()
def client():
    return TestClient(create_app(Settings(db_path=":memory:", api_key=""), DB(":memory:")))


def test_tool_endpoints(client):
    assert client.post("/tools/deal-costs", json={"price": 1_000_000}).json()["total_costs"] == 61_580
    assert client.post("/tools/mortgage", json={"principal": 120_000, "annual_rate_pct": 0, "years": 10}).json()["monthly_payment"] == 1000
    assert client.post("/tools/yield", json={"price": 1_000_000, "annual_rent": 70_000}).json()["gross_pct"] == 7.0
    assert client.post("/tools/deal-costs", json={"price": 0}).status_code == 422


def test_deal_sheet_for_lead_budget(client):
    client.post("/demo/seed")
    lid = next(l["id"] for l in client.get("/leads").json() if l["budget_max"] == 1_800_000)
    d = client.get(f"/leads/{lid}/deal-sheet").json()
    assert d["price"] == 1_800_000 and d["down_payment"] == 360_000 and d["loan"] == 1_440_000
    assert d["costs"]["total_costs"] == 114_270 and d["cash_needed_total"] == 474_270
    assert d["monthly_payment"] > 0 and "verify" in d["disclaimer"].lower()


def test_deal_sheet_422_without_budget_or_for_renters(client):
    client.post("/demo/seed")
    no_budget = next(l["id"] for l in client.get("/leads").json() if not l["budget_max"])
    assert client.get(f"/leads/{no_budget}/deal-sheet").status_code == 422
    renter = next(l["id"] for l in client.get("/leads").json() if l["label"] == "renter" and l["budget_max"])
    assert client.get(f"/leads/{renter}/deal-sheet").status_code == 422
    assert client.get("/leads/9999/deal-sheet").status_code == 404


def test_rates_can_be_overridden_from_environment(monkeypatch):
    monkeypatch.setenv("DLD_PCT", "3.5")
    monkeypatch.setenv("AGENCY_PCT", "1.0")
    monkeypatch.setenv("DEFAULT_YEARS", "20")
    r = Settings(db_path=":memory:").rates
    assert (r.dld_pct, r.agency_pct, r.default_years) == (3.5, 1.0, 20) and r.vat_pct == 5.0
    monkeypatch.setenv("DLD_PCT", "not-a-number")
    assert Settings(db_path=":memory:").rates.dld_pct == 4.0              # bad value falls back to the default, no crash
