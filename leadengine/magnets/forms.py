"""Input parsing, validation and presentation of the public calculators. Everything a visitor (or a tampered hidden field) sends is
re-validated here; nothing non-finite or absurd ever reaches the calculators or a template."""
from __future__ import annotations

import math
import re
from typing import Mapping

from ..calculators import affordability, deal_costs, mortgage_payment, rental_yield
from ..config import Rates
from ..identity import _NATIVE_DIGITS

TOOLS = ("affordability", "costs", "yield", "mortgage")
FIELDS: dict[str, tuple[str, ...]] = {
    "affordability": ("income", "debts", "down", "years", "rate"),
    "costs": ("price", "loan"),
    "yield": ("price", "rent", "service", "vacancy"),
    "mortgage": ("principal", "rate", "years"),
}
DEFAULTS = {"debts": 0.0, "down": 0.0, "loan": 0.0, "service": 0.0, "vacancy": 5.0}      # optional fields and their value when left empty
ROWS: dict[str, tuple[str, ...]] = {
    "affordability": ("max_monthly_payment", "max_loan", "max_price", "ltv_capped_price", "dbr_pct"),
    "costs": ("dld", "dld_admin", "agency", "agency_vat", "mortgage_registration", "total_costs"),
    "yield": ("gross_pct", "net_pct"),
    "mortgage": ("monthly_payment",),
}
PCT_ROWS = {"dbr_pct", "gross_pct", "net_pct"}
MAX_AMOUNT = 1e10
_NUM = re.compile(r"^(\d{1,3}([, \u00a0\u202f]\d{3})+|\d{1,15})(\.\d{1,6})?$", re.ASCII)       # plain, or grouped by exactly three digits
_SEPARATORS = str.maketrans("", "", ", \u00a0\u202f")


def parse_number(raw) -> float | None:
    """A plain non-negative number (ASCII or Arabic-Indic digits, thousands separators allowed), else None."""
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 24:
        return None
    s = raw.strip().translate(_NATIVE_DIGITS).replace("٫", ".").replace("٬", ",").replace("،", ",")
    if not _NUM.fullmatch(s):
        return None
    v = float(s.translate(_SEPARATORS))
    return v if math.isfinite(v) and v <= MAX_AMOUNT else None


def parse(tool: str, data: Mapping[str, str]) -> tuple[dict, str | None]:
    """(inputs, error key). Required fields must be numbers; optional ones fall back to DEFAULTS when empty."""
    out: dict[str, float] = {}
    for f in FIELDS[tool]:
        raw = data.get(f, "")
        if f in DEFAULTS and not str(raw).strip():
            out[f] = DEFAULTS[f]
            continue
        v = parse_number(raw)
        if v is None:
            return {}, "pub.err.number"
        out[f] = v
    return out, None


def in_range(tool: str, i: dict) -> bool:
    """Sane bounds for every tool (the calculators themselves only check positivity)."""
    years_ok = lambda y: y == int(y) and 1 <= y <= 35                                    # noqa: E731
    if tool == "affordability":
        return years_ok(i["years"]) and 0 <= i["rate"] <= 30 and i["income"] > 0
    if tool == "mortgage":
        return years_ok(i["years"]) and 0 <= i["rate"] <= 30 and i["principal"] > 0
    if tool == "costs":
        return i["price"] > 0 and 0 <= i["loan"] <= i["price"]
    if tool == "yield":
        return i["price"] > 0 and 0 <= i["rent"] <= i["price"] and 0 <= i["service"] <= i["price"] and 0 <= i["vacancy"] <= 100
    return False


def compute(tool: str, inputs: dict, rates: Rates) -> tuple[dict, str | None]:
    """(result, error key). Range problems surface as `pub.err.range`, never as exceptions."""
    if not in_range(tool, inputs):
        return {}, "pub.err.range"
    try:
        if tool == "affordability":
            years = inputs["years"]
            if years != int(years):
                return {}, "pub.err.range"
            return affordability(inputs["income"], inputs["debts"], inputs["down"], int(years), inputs["rate"], rates=rates), None
        if tool == "costs":
            return deal_costs(inputs["price"], rates, loan=inputs["loan"]), None
        if tool == "yield":
            return rental_yield(inputs["price"], inputs["rent"], service_charge=inputs["service"], vacancy_pct=inputs["vacancy"]), None
        years = inputs["years"]
        if years != int(years):
            return {}, "pub.err.range"
        return {"monthly_payment": mortgage_payment(inputs["principal"], inputs["rate"], int(years))}, None
    except ValueError:
        return {}, "pub.err.range"


def fmt(key: str, value) -> str:
    return f"{value:,.2f}%" if key in PCT_ROWS else f"{value:,.2f}"


def summary(tool: str, inputs: dict, result: dict) -> tuple[str, dict]:
    """One English line for the lead's text plus the `extra` answers the pipeline reads (budget...)."""
    label = {"affordability": "Affordability calculator", "costs": "Buying-costs calculator", "yield": "Rental-yield calculator",
             "mortgage": "Mortgage calculator"}[tool]
    parts = ", ".join(f"{k} {inputs[k]:,.0f}" if k not in ("rate", "years", "vacancy") else f"{k} {inputs[k]:g}" for k in FIELDS[tool])
    extra: dict[str, str] = {}
    if tool == "affordability":
        extra["budget"] = f"AED {result['ltv_capped_price']:.0f}"
    elif tool in ("costs", "yield"):
        extra["budget"] = f"AED {inputs['price']:.0f}"
    return f"Used the {label} (AED): {parts}.", extra


def raw(v: float) -> str:
    """A number as plain decimal text that `parse_number` accepts again (never scientific notation)."""
    s = format(v, ".6f").rstrip("0").rstrip(".")
    return s or "0"
