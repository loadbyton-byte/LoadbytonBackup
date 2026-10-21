"""Where a lead is from, using only what the person supplied: explicit form fields, their own phone number's country code, or an
explicit self-location phrase in their own text ("I'm in London"). No IP geolocation, lookups or enrichment.

A plain mention of a place ("interested in London property", "a flat in Dubai Marina") is NOT an origin."""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..identity import normalize_phone
from .cities import COUNTRIES, City, _fold, find_cities, resolve

BASIS_RANK = {"form": 3, "phone": 2, "text": 1}


@dataclass(frozen=True)
class Origin:
    country: str                 # ISO 3166 alpha-2
    city: str | None             # catalogue City.id
    basis: str                   # form | phone | text


# ITU calling codes (no '+') for the catalogue's countries. +1 is shared (US, CA, JM, TT...) and +7 is split below, so those are absent.
CALLING_CODES: dict[str, str] = {
    "AE": "971", "AM": "374", "AO": "244", "AR": "54", "AT": "43", "AU": "61", "AZ": "994", "BD": "880", "BE": "32", "BG": "359", "BH": "973",
    "BR": "55", "BY": "375", "CD": "243", "CH": "41", "CI": "225", "CL": "56", "CM": "237", "CN": "86", "CO": "57", "CY": "357", "CZ": "420",
    "DE": "49", "DK": "45", "DZ": "213", "EC": "593", "EE": "372", "EG": "20", "ES": "34", "ET": "251", "FI": "358", "FR": "33", "GB": "44",
    "GE": "995", "GH": "233", "GR": "30", "HK": "852", "HR": "385", "HU": "36", "ID": "62", "IE": "353", "IL": "972", "IN": "91", "IQ": "964",
    "IR": "98", "IT": "39", "JO": "962", "JP": "81", "KE": "254", "KG": "996", "KH": "855", "KR": "82", "KW": "965", "LB": "961", "LK": "94",
    "LT": "370", "LU": "352", "LV": "371", "MA": "212", "MC": "377", "MM": "95", "MN": "976", "MO": "853", "MT": "356", "MU": "230", "MX": "52",
    "MY": "60", "MZ": "258", "NG": "234", "NL": "31", "NO": "47", "NP": "977", "NZ": "64", "OM": "968", "PA": "507", "PE": "51", "PH": "63",
    "PK": "92", "PL": "48", "PT": "351", "QA": "974", "RO": "40", "RS": "381", "RW": "250", "SA": "966", "SD": "249", "SE": "46", "SG": "65",
    "SN": "221", "TH": "66", "TJ": "992", "TN": "216", "TR": "90", "TW": "886", "TZ": "255", "UA": "380", "UG": "256", "UY": "598", "UZ": "998",
    "VN": "84", "ZA": "27", "ZM": "260", "ZW": "263",
}
SHARED_PLUS_ONE = {"US", "CA", "JM", "TT"}
_BY_CODE = sorted(((v, k) for k, v in CALLING_CODES.items()), key=lambda x: -len(x[0]))


def country_for_phone(phone: str | None) -> str | None:
    try:
        p = normalize_phone(phone) if isinstance(phone, str) else None
    except Exception:
        return None
    if not p or not p.startswith("+"):
        return None
    digits = p[1:]
    if digits.startswith("7"):
        return "KZ" if digits[1:2] in ("6", "7") else "RU"
    if digits.startswith("1"):
        return None
    for code, cc in _BY_CODE:
        if digits.startswith(code):
            return cc
    return None


_PHRASES_EN = ("i am in", "i m in", "im in", "we are in", "we re in", "based in", "living in", "live in", "residing in", "currently in",
               "moving from", "relocating from", "flying from", "coming from", "calling from", "writing from", "originally from",
               "i am from", "i m from", "we are from", "we re from")
_PHRASES_AR = ("انا في", "نحن في", "مقيم في", "مقيمه في", "ساكن في", "ساكنه في", "اسكن في", "اعيش في", "قادم من", "قادمه من", "جاي من",
               "مسافر من", "مسافره من")
_PUNCT = re.compile(r"[^\w\s]", re.U)


def _prep(text: str) -> str:
    t = _fold(text)
    t = t.replace("'", " ").replace("’", " ")
    return re.sub(r"\s+", " ", _PUNCT.sub(" ", t)).strip()


def _from_text(text: str) -> Origin | None:
    t = f" {_prep(text)} "
    best: tuple[int, City] | None = None
    for phrase in (*_PHRASES_EN, *(_prep(p) for p in _PHRASES_AR)):
        for m in re.finditer(rf"(?<!\w){re.escape(phrase)} ", t):
            tail = " ".join(t[m.end():].split(" ")[:4])
            for city in find_cities(tail):
                if any(tail.startswith(_prep(k)) for k in (city.name, *city.aliases) if _prep(k)):      # the city must come right after the phrase
                    if best is None or m.start() < best[0]:
                        best = (m.start(), city)
                    break
    return Origin(best[1].country, best[1].id, "text") if best else None


def _from_form(country, city) -> Origin | None:
    c = resolve(city) if isinstance(city, str) and city.strip() and len(city) <= 80 else None
    if c:
        return Origin(c.country, c.id, "form")
    if isinstance(country, str) and 1 < len(country.strip()) <= 60:
        key = country.strip()
        if key.upper() in COUNTRIES:
            return Origin(key.upper(), None, "form")
        for iso, (name, _cur) in COUNTRIES.items():
            if name.lower() == key.lower():
                return Origin(iso, None, "form")
    return None


def resolve_origin(text: str | None, phone: str | None, country: str | None = None, city: str | None = None) -> Origin | None:
    """Strongest available origin (form > phone > text), or None. Pure; never raises."""
    try:
        o = _from_form(country, city)
        if o:
            return o
        cc = country_for_phone(phone)
        if cc:
            return Origin(cc, None, "phone")
        return _from_text(text[:20000]) if isinstance(text, str) and text else None
    except Exception:
        return None
