"""Where a lead is from, using only what the person supplied: explicit form fields, their own phone number's country code, or an
explicit first-person self-location phrase in their own text ("I'm in London"). No IP geolocation, lookups or enrichment.

A plain mention of a place ("interested in London property", "a flat in Dubai Marina", "best area to live in Dubai") is NOT an origin."""
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
_NANP_ISLANDS = {"1876": "JM", "1868": "TT"}                    # +1 area codes that identify a catalogue country
_BY_CODE = sorted(((v, k) for k, v in CALLING_CODES.items()), key=lambda x: -len(x[0]))

# Common ways people write a country on a form that are not the catalogue's official name.
COUNTRY_ALIASES = {
    "uk": "GB", "u.k.": "GB", "great britain": "GB", "britain": "GB", "england": "GB", "scotland": "GB", "wales": "GB", "northern ireland": "GB",
    "uae": "AE", "u.a.e.": "AE", "emirates": "AE", "usa": "US", "u.s.a.": "US", "u.s.": "US", "america": "US", "ksa": "SA", "saudi": "SA",
    "turkey": "TR", "ivory coast": "CI", "cote d'ivoire": "CI", "russian federation": "RU", "korea": "KR", "republic of korea": "KR",
    "holland": "NL", "czech republic": "CZ", "burma": "MM", "drc": "CD", "congo": "CD", "hong kong sar": "HK",
}


def resolve_country(value) -> str | None:
    """ISO2 for an ISO2 code, an official name or a common alias (accent/case-insensitive); None if unknown."""
    if not isinstance(value, str):
        return None
    key = _fold(value.strip())
    if not 2 <= len(key) <= 60:
        return None
    if key.upper() in COUNTRIES:
        return key.upper()
    if key in COUNTRY_ALIASES:
        return COUNTRY_ALIASES[key]
    for iso, (name, _cur) in COUNTRIES.items():
        if _fold(name) == key:
            return iso
    return None


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
        return _NANP_ISLANDS.get(digits[:4])
    for code, cc in _BY_CODE:
        if digits.startswith(code):
            return cc
    return None


# First-person phrases only: "live in", "living in" and "based in" alone are too common in property talk ("best area to live in Dubai").
_PHRASES_EN = ("i am in", "i m in", "im in", "we are in", "we re in", "i am currently in", "i m currently in", "we are currently in", "we re currently in",
               "i live in", "we live in", "i am living in", "i m living in", "we are living in", "we re living in",
               "i am based in", "i m based in", "we are based in", "we re based in", "i am residing in", "i m residing in",
               "i am coming from", "i m coming from", "we are coming from", "we re coming from",
               "i am originally from", "i m originally from", "we are originally from", "we re originally from",
               "i am from", "i m from", "we are from", "we re from")
# No first-person subject, so these count only when they open a clause ("Hi, moving from London"), never inside a sentence about others
_BARE_EN = ("moving from", "relocating from", "flying from", "calling from", "writing from")
_PHRASES_AR = ("انا في", "انا حاليا في", "نحن في", "نحن حاليا في", "مقيم في", "مقيمه في", "ساكن في", "ساكنه في", "اسكن في", "اعيش في",
               "انا من", "نحن من", "قادم من", "قادمه من", "جاي من", "مسافر من", "مسافره من")
_FILLER = {"hi", "hello", "hey", "salam", "thanks", "thank", "you", "please", "pls", "ok", "okay", "yes", "no"}
_BEFORE_BLOCK = {"to", "want", "wants", "wanted", "would", "love", "if", "when", "where", "developer", "agent", "company", "broker",
                 "he", "she", "they", "ل", "يبغى", "ابغى", "ابي", "المطور", "الوكيل"}
_AFTER_PLAN = {"next", "tomorrow", "soon", "tonight", "later", "weekend", "قريبا", "بكرا", "غدا"}
_PUNCT = re.compile(r"[^\w\s]", re.U)
_SENTENCE = re.compile(r"[.!?؟\n,;:،؛\-–—]+")                  # clause boundaries: a phrase is judged only against its own clause


def _prep(text: str) -> str:
    t = _fold(text).replace("'", " ").replace("’", " ")
    return re.sub(r"\s+", " ", _PUNCT.sub(" ", t)).strip()


_ALL_PHRASES = tuple(dict.fromkeys((*_PHRASES_EN, *_BARE_EN, *(_prep(p) for p in _PHRASES_AR))))
_BARE = set(_BARE_EN)


def _starts_with_alias(tail: str, city: City) -> int:
    """Length of the alias the tail starts with (at a word boundary), else 0."""
    for k in sorted((_prep(x) for x in (city.name, *city.aliases)), key=len, reverse=True):
        if k and (tail == k or tail.startswith(k + " ")):
            return len(k)
    return 0


def _from_text(text: str) -> Origin | None:
    for sentence in _SENTENCE.split(text):
        t = f" {_prep(sentence)} "
        best: tuple[int, City] | None = None
        for phrase in _ALL_PHRASES:
            for m in re.finditer(rf"(?<!\w){re.escape(phrase)} ", t):
                before = t[:m.start()].split()
                if phrase in _BARE and any(w not in _FILLER for w in before):
                    continue
                if any(w in _BEFORE_BLOCK for w in before[-2:]):
                    continue
                rest = t[m.end():m.end() + 160].split(" ")
                tail = " ".join(rest[:4])
                for city in find_cities(tail):
                    n = _starts_with_alias(tail, city)
                    if n:
                        after = tail[n:].split()[:2]
                        if any(w in _AFTER_PLAN for w in after):
                            break                                         # "I'm in Dubai next week": a plan, not a residence
                        if best is None or m.start() < best[0]:
                            best = (m.start(), city)
                        break
        if best:
            return Origin(best[1].country, best[1].id, "text")
    return None


def _from_form(country, city) -> Origin | None:
    c = None
    if isinstance(city, str) and city.strip() and len(city) <= 80:
        c = resolve(city) or next((x for x in _catalogue_ids() if x.id == city.strip().lower()), None)
    cc = resolve_country(country)
    if c:
        if cc and cc != c.country:
            return Origin(cc, None, "form")          # the person's own country outranks an ambiguous city name ("Birmingham")
        return Origin(c.country, c.id, "form")
    return Origin(cc, None, "form") if cc else None


def _catalogue_ids():
    from .cities import catalogue
    return catalogue()


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
