"""UAE community gazetteer: canonical name -> (emirate, aliases). Aliases may be Arabic."""
from __future__ import annotations

import re

from .text import normalize

AREAS: dict[str, tuple[str, list[str]]] = {
    "Downtown Dubai": ("Dubai", ["downtown dubai", "downtown", "burj khalifa", "داون تاون", "وسط مدينة دبي", "downtown dxb"]),
    "Dubai Marina": ("Dubai", ["dubai marina", "marina", "دبي مارينا", "مارينا"]),
    "Palm Jumeirah": ("Dubai", ["palm jumeirah", "the palm", "palm", "نخلة جميرا", "النخلة", "نخله جميرا"]),
    "Jumeirah Village Circle": ("Dubai", ["jvc", "jumeirah village circle", "جي في سي", "قرية جميرا"]),
    "Jumeirah Lake Towers": ("Dubai", ["jlt", "jumeirah lake towers", "بحيرات جميرا"]),
    "Business Bay": ("Dubai", ["business bay", "الخليج التجاري", "بزنس باي"]),
    "Dubai Hills Estate": ("Dubai", ["dubai hills", "dubai hills estate", "دبي هيلز"]),
    "Arabian Ranches": ("Dubai", ["arabian ranches", "المرابع العربية", "ارابيان رانشز"]),
    "Damac Hills": ("Dubai", ["damac hills", "داماك هيلز"]),
    "Dubai South": ("Dubai", ["dubai south", "دبي الجنوب", "expo city", "اكسبو سيتي"]),
    "MBR City": ("Dubai", ["mbr city", "mohammed bin rashid city", "sobha hartland", "district one", "مدينة محمد بن راشد"]),
    "Al Furjan": ("Dubai", ["al furjan", "furjan", "الفرجان"]),
    "Dubai Creek Harbour": ("Dubai", ["creek harbour", "creek harbor", "dubai creek harbour", "خور دبي"]),
    "Dubai Sports City": ("Dubai", ["sports city", "dubai sports city", "المدينة الرياضية"]),
    "Motor City": ("Dubai", ["motor city", "مدينة السيارات"]),
    "Meydan": ("Dubai", ["meydan", "ميدان"]),
    "Jumeirah": ("Dubai", ["jumeirah", "جميرا", "la mer", "جزر العالم"]),
    "Emaar Beachfront": ("Dubai", ["emaar beachfront", "beachfront", "امار بيتشفرونت"]),
    "Dubai Silicon Oasis": ("Dubai", ["silicon oasis", "dso", "واحة السيليكون"]),
    "Al Barsha": ("Dubai", ["al barsha", "barsha", "البرشاء"]),
    "Deira": ("Dubai", ["deira", "ديرة"]),
    "Bur Dubai": ("Dubai", ["bur dubai", "بر دبي", "karama", "الكرامه"]),
    "Dubai Islands": ("Dubai", ["dubai islands", "جزر دبي"]),
    "Al Reem Island": ("Abu Dhabi", ["al reem", "reem island", "جزيرة الريم", "الريم"]),
    "Saadiyat Island": ("Abu Dhabi", ["saadiyat", "جزيرة السعديات", "السعديات"]),
    "Yas Island": ("Abu Dhabi", ["yas island", "yas", "جزيرة ياس", "ياس"]),
    "Al Raha": ("Abu Dhabi", ["al raha", "الراحه", "raha beach"]),
    "Khalifa City": ("Abu Dhabi", ["khalifa city", "مدينة خليفة"]),
    "Corniche Abu Dhabi": ("Abu Dhabi", ["abu dhabi corniche", "كورنيش ابوظبي"]),
    "Al Marjan Island": ("Ras Al Khaimah", ["al marjan", "marjan island", "جزيرة المرجان", "wynn ras al khaimah"]),
    "Ras Al Khaimah": ("Ras Al Khaimah", ["ras al khaimah", "rak", "راس الخيمه"]),
    "Sharjah": ("Sharjah", ["sharjah", "الشارقه", "aljada", "الجادة", "muwaileh", "مويلح"]),
    "Ajman": ("Ajman", ["ajman", "عجمان"]),
    "Umm Al Quwain": ("Umm Al Quwain", ["umm al quwain", "uaq", "ام القيوين"]),
    "Fujairah": ("Fujairah", ["fujairah", "الفجيره"]),
}

EMIRATE_ALIASES = {
    "Dubai": ["dubai", "دبي", "dxb"],
    "Abu Dhabi": ["abu dhabi", "abudhabi", "ابوظبي", "ابو ظبي"],
    "Sharjah": ["sharjah", "الشارقه"],
    "Ajman": ["ajman", "عجمان"],
    "Ras Al Khaimah": ["ras al khaimah", "rak", "راس الخيمه"],
    "Umm Al Quwain": ["umm al quwain", "uaq", "ام القيوين"],
    "Fujairah": ["fujairah", "الفجيره"],
}


def _compile(alias: str) -> re.Pattern[str]:
    a = re.escape(normalize(alias))
    # Latin aliases need word boundaries; Arabic ones are matched as substrings.
    if re.search("[a-z]", a):
        return re.compile(rf"(?<![a-z0-9]){a}(?![a-z0-9])")
    return re.compile(a)


_AREA_PATTERNS = [(name, emirate, _compile(a)) for name, (emirate, aliases) in AREAS.items() for a in aliases]
_EMIRATE_PATTERNS = [(name, _compile(a)) for name, aliases in EMIRATE_ALIASES.items() for a in aliases]


def extract_areas(text: str) -> tuple[list[str], list[str]]:
    """Return (communities, emirates) mentioned in text, in order of first appearance.

    Longest match wins: "Palm Jumeirah" must not also count as the generic "Jumeirah"."""
    t = normalize(text)
    spans: list[tuple[int, int, str, str]] = []
    for name, emirate, pat in _AREA_PATTERNS:
        for m in pat.finditer(t):
            spans.append((m.start(), m.end(), name, emirate))
    kept = [a for a in spans
            if not any(b[2] != a[2] and b[0] <= a[0] and a[1] <= b[1] and (b[1] - b[0]) > (a[1] - a[0]) for b in spans)]
    kept.sort()
    areas: list[str] = []
    emirates: list[str] = []
    for _, _, name, emirate in kept:
        if name not in areas:
            areas.append(name)
        if emirate not in emirates:
            emirates.append(emirate)
    for name, pat in _EMIRATE_PATTERNS:
        if pat.search(t) and name not in emirates:
            emirates.append(name)
    return areas, emirates
