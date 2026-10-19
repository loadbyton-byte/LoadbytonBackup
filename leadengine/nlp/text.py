"""Text normalisation shared by every classifier (English, Arabic, mixed)."""
from __future__ import annotations

import re

_AR_DIACRITICS = re.compile("[ً-ْـ]")
_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_AR_LETTER = re.compile("[؀-ۿ]")
_LATIN_LETTER = re.compile("[a-zA-Z]")
_ALEF = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ة": "ه", "ؤ": "و", "ئ": "ي"})


def normalize(text: str) -> str:
    """Lower-case, unify Arabic letter variants, strip diacritics, ASCII digits."""
    text = text.translate(_AR_DIGITS).translate(_ALEF)
    text = _AR_DIACRITICS.sub("", text)
    return text.lower()


def detect_language(text: str) -> str:
    ar = len(_AR_LETTER.findall(text))
    en = len(_LATIN_LETTER.findall(text))
    total = ar + en
    if total == 0:
        return "unknown"
    share = ar / total
    if share > 0.7:
        return "ar"
    if share > 0.15:
        return "mixed"
    return "en"
