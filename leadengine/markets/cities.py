"""City catalogue: where Dubai-property demand can come from, with the language/currency/timezone needed to market there.

`cities.psv` is editable data. `tier` is an editorial starting priority, NOT a statistic: replace it with demand measured from your own
analytics. Lookups are accent-, case- and Arabic-diacritic-insensitive; text matching ignores 1-2 character aliases to avoid false hits."""
from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..nlp.text import normalize

DATA = Path(__file__).with_name("cities.psv")

# ISO 3166 alpha-2 -> (country name, ISO 4217 currency). Currencies reflect usage at the time of writing: verify before pricing in them.
COUNTRIES: dict[str, tuple[str, str]] = {
    "AE": ("United Arab Emirates", "AED"), "IN": ("India", "INR"), "PK": ("Pakistan", "PKR"), "BD": ("Bangladesh", "BDT"),
    "LK": ("Sri Lanka", "LKR"), "NP": ("Nepal", "NPR"), "SA": ("Saudi Arabia", "SAR"), "KW": ("Kuwait", "KWD"), "QA": ("Qatar", "QAR"),
    "BH": ("Bahrain", "BHD"), "OM": ("Oman", "OMR"), "JO": ("Jordan", "JOD"), "LB": ("Lebanon", "LBP"), "IQ": ("Iraq", "IQD"),
    "IR": ("Iran", "IRR"), "IL": ("Israel", "ILS"), "TR": ("Türkiye", "TRY"), "EG": ("Egypt", "EGP"), "AZ": ("Azerbaijan", "AZN"),
    "GE": ("Georgia", "GEL"), "AM": ("Armenia", "AMD"), "NG": ("Nigeria", "NGN"), "KE": ("Kenya", "KES"), "ZA": ("South Africa", "ZAR"),
    "GH": ("Ghana", "GHS"), "ET": ("Ethiopia", "ETB"), "TZ": ("Tanzania", "TZS"), "UG": ("Uganda", "UGX"), "RW": ("Rwanda", "RWF"),
    "MA": ("Morocco", "MAD"), "TN": ("Tunisia", "TND"), "DZ": ("Algeria", "DZD"), "SD": ("Sudan", "SDG"), "AO": ("Angola", "AOA"),
    "MZ": ("Mozambique", "MZN"), "ZW": ("Zimbabwe", "ZWG"), "ZM": ("Zambia", "ZMW"), "SN": ("Senegal", "XOF"), "CI": ("Côte d'Ivoire", "XOF"),
    "CD": ("DR Congo", "CDF"), "MU": ("Mauritius", "MUR"), "CM": ("Cameroon", "XAF"), "GB": ("United Kingdom", "GBP"), "IE": ("Ireland", "EUR"),
    "FR": ("France", "EUR"), "BE": ("Belgium", "EUR"), "NL": ("Netherlands", "EUR"), "DE": ("Germany", "EUR"), "CH": ("Switzerland", "CHF"),
    "AT": ("Austria", "EUR"), "IT": ("Italy", "EUR"), "ES": ("Spain", "EUR"), "PT": ("Portugal", "EUR"), "GR": ("Greece", "EUR"),
    "CZ": ("Czechia", "CZK"), "PL": ("Poland", "PLN"), "HU": ("Hungary", "HUF"), "RO": ("Romania", "RON"), "BG": ("Bulgaria", "EUR"),
    "RS": ("Serbia", "RSD"), "HR": ("Croatia", "EUR"), "SE": ("Sweden", "SEK"), "NO": ("Norway", "NOK"), "DK": ("Denmark", "DKK"),
    "FI": ("Finland", "EUR"), "LU": ("Luxembourg", "EUR"), "MC": ("Monaco", "EUR"), "CY": ("Cyprus", "EUR"), "MT": ("Malta", "EUR"),
    "UA": ("Ukraine", "UAH"), "BY": ("Belarus", "BYN"), "LV": ("Latvia", "EUR"), "EE": ("Estonia", "EUR"), "LT": ("Lithuania", "EUR"),
    "RU": ("Russia", "RUB"), "KZ": ("Kazakhstan", "KZT"), "UZ": ("Uzbekistan", "UZS"), "KG": ("Kyrgyzstan", "KGS"), "TJ": ("Tajikistan", "TJS"),
    "CN": ("China", "CNY"), "HK": ("Hong Kong", "HKD"), "MO": ("Macau", "MOP"), "TW": ("Taiwan", "TWD"), "JP": ("Japan", "JPY"),
    "KR": ("South Korea", "KRW"), "MN": ("Mongolia", "MNT"), "SG": ("Singapore", "SGD"), "MY": ("Malaysia", "MYR"), "ID": ("Indonesia", "IDR"),
    "TH": ("Thailand", "THB"), "PH": ("Philippines", "PHP"), "VN": ("Vietnam", "VND"), "KH": ("Cambodia", "KHR"), "MM": ("Myanmar", "MMK"),
    "AU": ("Australia", "AUD"), "NZ": ("New Zealand", "NZD"), "US": ("United States", "USD"), "CA": ("Canada", "CAD"), "MX": ("Mexico", "MXN"),
    "BR": ("Brazil", "BRL"), "AR": ("Argentina", "ARS"), "CO": ("Colombia", "COP"), "PE": ("Peru", "PEN"), "CL": ("Chile", "CLP"),
    "PA": ("Panama", "USD"), "EC": ("Ecuador", "USD"), "UY": ("Uruguay", "UYU"), "TT": ("Trinidad and Tobago", "TTD"), "JM": ("Jamaica", "JMD"),
}


@dataclass(frozen=True)
class City:
    id: str
    name: str
    country: str
    country_name: str
    currency: str
    region: str
    timezone: str
    languages: tuple[str, ...]
    tier: int
    aliases: tuple[str, ...]

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["languages"], d["aliases"] = list(self.languages), list(self.aliases)
        return d


def _fold(text: str) -> str:
    """Lower-case, strip Latin accents and Arabic diacritics/variants so 'São Paulo' == 'sao paulo' and 'إسطنبول' == 'اسطنبول'."""
    s = unicodedata.normalize("NFKD", normalize(text))
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def _slug(name: str, cc: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-") + "-" + cc.lower()


def load_cities(path: Path | str = DATA) -> list[City]:
    cities: list[City] = []
    owner: dict[str, str] = {}
    ids: set[str] = set()
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip() or line.startswith("#"):
            continue
        f = line.split("|")
        if len(f) != 7:
            raise ValueError(f"line {n}: expected 7 fields, got {len(f)}")
        name, cc, region, tz, langs, tier, aliases = (x.strip() for x in f)
        if cc not in COUNTRIES:
            raise ValueError(f"line {n}: unknown country code {cc!r}")
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"line {n}: unknown timezone {tz!r}") from None
        languages = tuple(x for x in langs.split(";") if x)
        if not languages or not all(re.fullmatch(r"[a-z]{2}", x) for x in languages):
            raise ValueError(f"line {n}: languages must be ISO 639-1 codes")
        if tier not in ("1", "2", "3"):
            raise ValueError(f"line {n}: tier must be 1, 2 or 3")
        cid = _slug(name, cc)
        if cid in ids:
            raise ValueError(f"line {n}: duplicate city id {cid}")
        ids.add(cid)
        alias_list = tuple(a.strip() for a in aliases.split(";") if a.strip())
        for key in (name, *alias_list):
            k = _fold(key)
            if k in owner and owner[k] != cid:
                raise ValueError(f"line {n}: name/alias {key!r} already used by {owner[k]}")
            owner[k] = cid
        cname, currency = COUNTRIES[cc]
        cities.append(City(cid, name, cc, cname, currency, region, tz, languages, int(tier), alias_list))
    return cities


@cache
def catalogue() -> tuple[City, ...]:
    return tuple(load_cities())


@cache
def _index() -> dict[str, City]:
    return {_fold(k): c for c in catalogue() for k in (c.name, *c.aliases)}


def resolve(name: str, fuzzy: bool = False) -> City | None:
    """Exact (accent/case-insensitive) lookup by name or alias; with fuzzy=True also tolerates typos ('chennari' -> Chennai)."""
    idx, key = _index(), _fold(name or "")
    if key in idx:
        return idx[key]
    if fuzzy and key:
        close = difflib.get_close_matches(key, list(idx), n=1, cutoff=0.82)
        return idx[close[0]] if close else None
    return None


_CYRILLIC = re.compile("[Ѐ-ӿ]")


@cache
def _patterns() -> list[tuple[re.Pattern[str], City]]:
    out = []
    for key, city in _index().items():
        if len(key) < 3:                                     # 'la', 'hk': too ambiguous inside free text
            continue
        if _CYRILLIC.search(key) and len(key) >= 5:
            key = key[:-1]                                   # Russian inflection: Москва -> Москве
        k = re.escape(key)
        out.append((re.compile(rf"(?<![a-z0-9]){k}(?![a-z0-9])" if re.search("[a-z]", key) else k), city))
    return out


def find_cities(text: str) -> list[City]:
    """Cities mentioned in free text, in order of first appearance; the longest match wins ('New York City' is one city).
    Dubai itself is in the catalogue, so callers measuring *source* markets should drop region 'UAE (domestic)'."""
    t = _fold(text or "")
    spans = [(m.start(), m.end(), c) for pat, c in _patterns() for m in pat.finditer(t)]
    kept = [a for a in spans if not any(b[2] is not a[2] and b[0] <= a[0] and a[1] <= b[1] and (b[1] - b[0]) > (a[1] - a[0]) for b in spans)]
    out: list[City] = []
    for _, _, c in sorted(kept, key=lambda x: x[0]):
        if c not in out:
            out.append(c)
    return out


def search(q: str | None = None, country: str | None = None, region: str | None = None, tier: int | None = None,
           lang: str | None = None, limit: int = 100) -> list[City]:
    rows = [c for c in catalogue()
            if (not country or c.country == country.upper()) and (not region or c.region == region)
            and (tier is None or c.tier == tier) and (not lang or lang.lower() in c.languages)]
    if q:
        needle = _fold(q)
        hit = [c for c in rows if any(needle in _fold(k) for k in (c.name, *c.aliases))]
        if not hit:
            near = resolve(q, fuzzy=True)
            hit = [near] if near and near in rows else []
        rows = hit
    return rows[:limit]


def stats() -> dict:
    cs = catalogue()
    by_region: dict[str, int] = {}
    for c in cs:
        by_region[c.region] = by_region.get(c.region, 0) + 1
    return {"cities": len(cs), "countries": len({c.country for c in cs}), "by_region": by_region,
            "languages": sorted({lang for c in cs for lang in c.languages}), "by_tier": {t: sum(c.tier == t for c in cs) for t in (1, 2, 3)}}
