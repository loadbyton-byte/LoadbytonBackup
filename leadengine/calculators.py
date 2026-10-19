"""Deal-cost, mortgage and yield arithmetic. Pure functions; every rate comes from `Rates` (configuration, not law)."""
from __future__ import annotations

from .config import Rates


def _need(cond: bool, msg: str) -> None:
    if not cond:
        raise ValueError(msg)


def deal_costs(price: float, rates: Rates, *, loan: float = 0.0) -> dict:
    _need(price > 0, "price must be > 0")
    _need(0 <= loan <= price, "loan must be between 0 and the price")
    dld = price * rates.dld_pct / 100
    agency = price * rates.agency_pct / 100
    agency_vat = agency * rates.vat_pct / 100
    reg = loan * rates.mortgage_reg_pct / 100 + (rates.mortgage_reg_fee if loan > 0 else 0.0)
    total = dld + rates.dld_admin_fee + agency + agency_vat + reg
    return {"dld": round(dld, 2), "dld_admin": rates.dld_admin_fee, "agency": round(agency, 2), "agency_vat": round(agency_vat, 2),
            "mortgage_registration": round(reg, 2), "total_costs": round(total, 2)}


def mortgage_payment(principal: float, annual_rate_pct: float, years: int) -> float:
    _need(principal > 0 and years > 0 and annual_rate_pct >= 0, "principal and years must be > 0 and rate >= 0")
    n, r = years * 12, annual_rate_pct / 100 / 12
    return round(principal / n if r == 0 else principal * r / (1 - (1 + r) ** -n), 2)


def rental_yield(price: float, annual_rent: float, *, service_charge: float = 0.0, vacancy_pct: float = 5.0) -> dict:
    _need(price > 0 and annual_rent >= 0 and service_charge >= 0 and 0 <= vacancy_pct <= 100,
          "price must be > 0; rent and service charge >= 0; vacancy 0-100")
    net = annual_rent * (1 - vacancy_pct / 100) - service_charge
    return {"gross_pct": round(annual_rent / price * 100, 2), "net_pct": round(net / price * 100, 2)}


def max_loan(price: float, *, ltv_pct: float) -> float:
    _need(price > 0 and 0 <= ltv_pct <= 100, "price must be > 0 and ltv_pct 0-100")
    return round(price * ltv_pct / 100, 2)
