"""Bilingual (EN / Gulf-Arabic) real-estate intent classifier + entity extractor.

Deterministic and explainable on purpose: every decision carries the evidence phrases that
triggered it, so a human can audit why a post became a lead. An LLM can be layered on top
(see `pipeline.Pipeline(llm_refiner=...)`) but is never required.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .areas import extract_areas
from .text import detect_language, normalize


def _c(*patterns: str) -> list[re.Pattern[str]]:
    return [re.compile(normalize(p)) for p in patterns]


# --- intent label patterns (label -> (weight, regex)) ------------------------------------
LABEL_PATTERNS: dict[str, list[tuple[float, re.Pattern[str]]]] = {
    "buyer": [
        (3, p) for p in _c(
            r"\b(looking|want|wanna|planning|plan|trying|intend|need|ready|thinking|hoping) (to|2) buy\b",
            r"\bbuy(ing)? (a|an|my|our|first|property|home|house|apartment|flat|villa|unit)\b",
            r"\bpurchas(e|ing) (a|an|my|our|property|apartment|villa|unit)\b",
            r"\b(first|1st)[- ]time (home )?buyer\b",
            r"\bdown ?payment\b", r"\bpre-?approv", r"\bmortgage\b", r"\bpayment plan\b",
            r"\bhandover\b", r"\boff[- ]?plan\b", r"\bwhere (should|can|to) i buy\b",
            r"ابي اشتري", r"ابغي اشتري", r"ابغى اشتري", r"ابغا اشتري", r"ناوي اشتري", r"اريد شراء", r"اريد ان اشتري",
            r"ابحث عن (شقه|فيله|عقار|وحده|تاون هاوس|ارض).{0,25}(للبيع|للشراء|تمليك)", r"تمليك", r"الدفعه الاولي", r"خطه دفع",
        )
    ],
    "investor": [
        (3, p) for p in _c(
            r"\brental yield\b", r"\broi\b", r"\bcapital (appreciation|gains)\b", r"\bbuy[- ]to[- ]let\b",
            r"\binvest(ing|ment)? in (dubai|abu dhabi|uae|property|real estate|off[- ]?plan)\b",
            r"\bgolden visa\b", r"\bproperty (portfolio|investment)\b", r"\bflip(ping)?\b", r"\bpassive income\b",
            r"استثمار عقاري", r"الفيزا الذهبيه", r"الاقامه الذهبيه", r"عائد ايجاري", r"اريد الاستثمار", r"استثمر في",
        )
    ],
    "renter": [
        (3, p) for p in _c(
            r"\b(looking|need|want|searching|trying|hoping) (to|for)( a| an)? ?(rent|\d ?(br|bed|bhk)|studio|apartment|flat|room|villa|place)\b",
            r"\bmoving to (dubai|abu dhabi|sharjah|ajman|uae|rak)\b", r"\brelocat(e|ing|ion) to\b",
            r"\bannual rent\b", r"\b(1|2|4|6|12) cheques?\b", r"\bwhere (to|can i) rent\b", r"\bbest (area|place|community) to (live|rent)\b",
            r"\bshort[- ]term (rental|stay)\b", r"\bpet[- ]friendly (apartment|flat|building)\b",
            r"ابحث عن (شقه|غرفه|فيله|استوديو|سكن).{0,25}(ايجار|للايجار)", r"ابي ايجار", r"مطلوب (شقه|غرفه|فيله|استوديو) للايجار",
            r"ابغي استاجر", r"ابغى استاجر", r"انتقل الي (دبي|ابوظبي|الشارقه)", r"سكن في (دبي|ابوظبي|الشارقه)",
        )
    ],
    # buy-vs-rent not stated: resolved later from budget size, otherwise kept as "seeker"
    "seeker": [
        (2, p) for p in _c(
            r"\b(searching|looking|hunting|shopping) for (a|an|my)\b.{0,30}\b(\d ?(br|bed\w*|bhk)|studio|villa|apartment|flat|townhouse|penthouse|unit)\b",
            r"\bbudget( is| of|:)? ?(aed|dhs?)?\s*\d", r"\b(comparing|compare)\b.{0,60}\b(villa|apartment|townhouse|communit\w+|areas?)\b",
            r"\b(how much|price|cost|starting price)\b.{0,40}\b(studio|\d ?(br|bed\w*)|villa|apartment|unit|flat|penthouse)\b",
            r"\bprice (please|pls|list)\b", r"\bany (units?|availability) (available|left)\b",
            r"كم السعر", r"ابي اعرف السعر", r"ميزانيتي",
        )
    ],
    "seller": [
        (3, p) for p in _c(
            r"\b(want|looking|plan(ning)?|need) to sell (my|our|a)\b", r"\bsell(ing)? my (apartment|villa|property|flat|unit|house|townhouse)\b",
            r"\bhow much (is|would) my .{0,25}(worth|sell for)\b", r"\bvaluation\b", r"\bsell (before|after) handover\b",
            r"ابي ابيع", r"ابغي ابيع", r"ابغى ابيع", r"اريد بيع", r"كم قيمه (شقتي|فيلتي|عقاري)", r"تقييم عقار",
        )
    ],
    "landlord": [
        (3, p) for p in _c(
            r"\b(want|looking|need) to (rent out|lease out|let)\b", r"\brent(ing)? out my\b", r"\btenant for my\b",
            r"\bproperty management\b", r"\bmy (apartment|villa|unit|flat) (is )?(vacant|empty)\b",
            r"ابي اأجر", r"ابي اجر", r"ابغي اجر", r"ابغى اجر", r"اريد تاجير", r"مستاجر ل(شقتي|فيلتي|وحدتي)",
        )
    ],
}

# Self-promotion / broker spam — we never want these as leads.
SPAM_PATTERNS = _c(
    r"\bdm (me )?for\b", r"\bcall (us|me) (now|today|on)\b", r"whats ?app[: ]*\+?\(?971", r"\+?971[ -]?\d{1,2}[ -]?\d{3}[ -]?\d{4}",
    r"\bexclusive (offer|launch|deal)\b", r"\bbrn[: #]*\d{3,}", r"\brera (reg|licen|brn)", r"\blimited units\b", r"\bbook (now|your unit|today)\b",
    r"\bguaranteed (roi|returns?)\b", r"\bour (team|agents?|company|brokerage)\b", r"\b(listing|listed) (by|with) \w+ (properties|real estate)\b",
    r"\bfor (sale|rent)[:!-]\s", r"\bnew launch\b", r"\bpermit (no|number)\b", r"\btrakheesi\b", r"\bregistration open\b",
    r"للتواصل", r"تواصل معنا", r"عرض حصري", r"وحدات محدوده", r"احجز الان", r"رقم الرخصه", r"رخصه تريخيص",
)

BROKER_REQUEST = _c(
    r"\b(recommend(ation)?s?|suggest(ions)?) (for |of )?(a |an )?(good |trusted |reliable |honest )?(real estate |property )?(agent|broker|agency)\b",
    r"\bany(one)? (know|got|have) (a )?(good |trusted |reliable )?(agent|broker)\b", r"\bneed (a|an) (agent|broker)\b", r"\blooking for (a|an) (agent|broker)\b",
    r"وسيط عقاري موثوق", r"تنصحوني (ب)?وسيط", r"مين يعرف وسيط", r"ابي وسيط", r"ابغى وسيط", r"مستشار عقاري",
)

STAGE_READY = _c(
    r"\bready to (buy|move|sign|book|reserve|purchase)\b", r"\bcash buyer\b", r"\bpre-?approved\b", r"\bviewings?\b", r"\b(visit|see|view) (it |them )?(this|next) (week|weekend)\b",
    r"\bin dubai (this|next) week\b", r"\bbook(ing)? a viewing\b", r"\breserv(e|ation)\b", r"\bsign(ing)? (this|next)\b", r"\bdeposit\b", r"\burgent(ly)?\b", r"\basap\b",
    r"جاهز للشراء", r"كاش", r"معاينه", r"زياره العقار", r"مستعجل", r"فورا", r"اسرع وقت",
)
STAGE_SHORTLIST = _c(
    r"\b(vs|versus|compare|comparing|comparison)\b", r"\bwhich (is )?(better|best)\b", r"\bshortlist(ed)?\b", r"\bbetween .{3,40} and .{3,40}\b", r"\bpros and cons\b",
    r"افضل (منطقه|مشروع|مطور)", r"مقارنه", r"ايهما افضل", r"بين .{3,30} و ",
)
STAGE_RESEARCH = _c(
    r"\bis it (worth|a good idea|safe)\b", r"\bthinking (about|of)\b", r"\bshould i\b", r"\bwhat('s| is) the (best|average)\b", r"\bhow (much|does|do|to)\b",
    r"\bany advice\b", r"\bnew to\b", r"\bexplain\b", r"هل يستاهل", r"افكر في", r"نصيحه", r"كيف",
)

URGENCY_RULES: list[tuple[re.Pattern[str], int]] = [
    (re.compile(normalize(r"\b(asap|urgent(ly)?|immediately|right now|today|tomorrow)\b|مستعجل|فورا|اسرع وقت|اليوم|بكره")), 3),
    (re.compile(normalize(r"\bthis (week|weekend)\b|هذا الاسبوع")), 7),
    (re.compile(normalize(r"\bnext (week)\b|الاسبوع الجاي|الاسبوع القادم")), 10),
    (re.compile(normalize(r"\bnext month\b|\bend of (this )?month\b|الشهر الجاي|الشهر القادم")), 30),
    (re.compile(normalize(r"(?<!earlier )(?<!last )(?<!since )(?<!later )\bthis month\b|\b(within|in) a month\b|هذا الشهر|خلال شهر")), 30),
    (re.compile(normalize(r"\bthis (quarter)\b")), 60),
    (re.compile(normalize(r"\b(end of|by) (the )?(year|q4)\b|\bthis year\b|نهايه السنه|هذه السنه")), 120),
]
_WITHIN = re.compile(normalize(r"\b(?:within|in|next|after)\s+(\d{1,2})\s*(day|week|month)s?\b"))
_AR_WITHIN = re.compile(normalize(r"(?:خلال|بعد)\s+(\d{1,2})\s*(يوم|ايام|اسبوع|اسابيع|شهر|اشهر|شهور)"))

# --- entity patterns ---------------------------------------------------------------------
_UNIT = {"m": 1e6, "mn": 1e6, "mil": 1e6, "mill": 1e6, "million": 1e6, "k": 1e3, "thousand": 1e3, "مليون": 1e6, "الف": 1e3}
_UNIT_RE = r"(million|mill|mil|mn|m|k|thousand|مليون|الف)"
_RANGE = re.compile(rf"(\d+(?:\.\d+)?)\s*(?:-|–|to|الي)\s*(\d+(?:\.\d+)?)\s*{_UNIT_RE}\b")
_AMOUNT = re.compile(rf"(?:aed|dhs?|dirhams?|درهم)?\s*(\d+(?:[.,]\d+)*)\s*{_UNIT_RE}(?![a-z])")
_PLAIN = re.compile(r"(?:aed|dhs?|dirhams?|درهم)\s*(\d{1,3}(?:,\d{3})+|\d{5,9})|(\d{1,3}(?:,\d{3})+|\d{5,9})\s*(?:aed|dhs?|dirhams?|درهم)")
_BEDS = re.compile(r"(\d)\s*[- ]?\s*(?:br|bhk|bed(?:room)?s?|bd|bdr)\b|bed(?:room)?s?\s*[:=]\s*(\d)")
_AR_BEDS = {"غرفتين": 2, "غرفه وصاله": 1, "ثلاث غرف": 3, "اربع غرف": 4, "استوديو": 0}
_PROP_TYPES = {
    "villa": r"\bvilla\b|فيله", "townhouse": r"\btownhouse\b|تاون هاوس", "apartment": r"\b(apartment|flat|apt)\b|شقه",
    "studio": r"\bstudio\b|استوديو", "penthouse": r"\bpenthouse\b|بنتهاوس", "plot": r"\b(plot|land)\b|ارض",
    "commercial": r"\b(office|shop|retail|warehouse|commercial)\b|مكتب|محل",
}
_PROP_RE = {k: re.compile(normalize(v)) for k, v in _PROP_TYPES.items()}
_FIN = {
    "cash": re.compile(normalize(r"\bcash( buyer| purchase| payment)?\b|كاش")),
    "mortgage": re.compile(normalize(r"\bmortgage\b|\bpre-?approv\w*\b|\bbank (loan|finance)\b|قرض عقاري|تمويل عقاري")),
    "payment_plan": re.compile(normalize(r"\bpayment plan\b|\binstal+ments?\b|خطه دفع|تقسيط")),
}
_OFFPLAN = re.compile(normalize(r"\boff[- ]?plan\b|\bunder construction\b|\bnew launch\b|على الخارطه|قيد الانشاء"))
_GOLDEN = re.compile(normalize(r"\bgolden visa\b|الفيزا الذهبيه|الاقامه الذهبيه"))


@dataclass
class Intent:
    label: str = "noise"            # buyer|investor|renter|seller|landlord|agent_spam|noise
    stage: str = "none"             # none|research|shortlisting|ready
    confidence: float = 0.0
    language: str = "unknown"
    areas: list[str] = field(default_factory=list)
    emirates: list[str] = field(default_factory=list)
    budget_min: float | None = None
    budget_max: float | None = None
    bedrooms: int | None = None
    property_types: list[str] = field(default_factory=list)
    purchase_type: str | None = None    # off_plan | ready | rent
    timeline_days: int | None = None
    financing: str | None = None        # cash | mortgage | payment_plan
    golden_visa: bool = False
    wants_agent: bool = False
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _num(s: str) -> float:
    s = s.replace(",", "")
    return float(s)


def parse_budget(t: str) -> tuple[float | None, float | None]:
    m = _RANGE.search(t)
    if m:
        mult = _UNIT[m.group(3)]
        lo, hi = _num(m.group(1)) * mult, _num(m.group(2)) * mult
        return min(lo, hi), max(lo, hi)
    vals = []
    for m in _AMOUNT.finditer(t):
        raw = m.group(1)
        # "1,200k" style commas are thousands separators; "1.5m" is decimal.
        v = _num(raw.replace(",", "") if raw.count(",") and "." not in raw else raw.replace(",", "."))
        vals.append(v * _UNIT[m.group(2)])
    for m in _PLAIN.finditer(t):
        vals.append(_num(m.group(1) or m.group(2)))
    vals = [v for v in vals if v >= 20_000]
    if not vals:
        return None, None
    return None, max(vals)


def parse_timeline(t: str) -> int | None:
    m = _WITHIN.search(t)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return n * {"day": 1, "week": 7, "month": 30}[unit]
    m = _AR_WITHIN.search(t)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return n * (1 if unit in ("يوم", "ايام") else 7 if unit in ("اسبوع", "اسابيع") else 30)
    for pat, days in URGENCY_RULES:
        if pat.search(t):
            return days
    return None


def _hits(patterns, t: str) -> list[str]:
    out = []
    for p in patterns:
        m = p.search(t)
        if m:
            out.append(m.group(0).strip())
    return out


def fill_entities(it: Intent, raw: str) -> Intent:
    """Extract areas, budget, bedrooms, type, timeline, financing from free text into `it` (in place)."""
    t = normalize(raw)
    label = it.label
    it.areas, it.emirates = extract_areas(raw)
    lo, hi = parse_budget(t)
    it.budget_min, it.budget_max = lo, hi
    b = _BEDS.search(t)
    if b:
        it.bedrooms = int(b.group(1) or b.group(2))
    else:
        for k, v in _AR_BEDS.items():
            if normalize(k) in t:
                it.bedrooms = v
                break
    if it.bedrooms is None and "studio" in t:
        it.bedrooms = 0
    it.property_types = [k for k, pat in _PROP_RE.items() if pat.search(t)]
    it.timeline_days = parse_timeline(t)
    for k, pat in _FIN.items():
        if pat.search(t):
            it.financing = k
            break
    it.golden_visa = bool(_GOLDEN.search(t))
    if label == "renter" or label == "landlord":
        it.purchase_type = "rent"
    elif _OFFPLAN.search(t):
        it.purchase_type = "off_plan"
    elif label in ("buyer", "investor", "seller"):
        it.purchase_type = "ready"
    elif label == "seeker":
        it.purchase_type = None
    return it


def classify(text: str) -> Intent:
    raw = text or ""
    t = normalize(raw)
    it = Intent(language=detect_language(raw))
    if len(t.strip()) < 12:
        return it

    spam_hits = _hits(SPAM_PATTERNS, t)
    scores: dict[str, float] = {}
    evidence: dict[str, list[str]] = {}
    for label, rules in LABEL_PATTERNS.items():
        for weight, pat in rules:
            m = pat.search(t)
            if m:
                scores[label] = scores.get(label, 0) + weight
                evidence.setdefault(label, []).append(m.group(0).strip())

    # Heavy spam signals beat intent: brokers advertise using the same vocabulary buyers use.
    spam_strength = len(spam_hits)
    if spam_strength >= 2 or (spam_strength == 1 and not scores):
        it.label, it.confidence, it.evidence = "agent_spam", min(0.55 + 0.15 * spam_strength, 0.97), spam_hits[:4]
        return it
    if not scores:
        return it

    if "seeker" in scores and len(scores) > 1:
        del scores["seeker"]                         # weak signal only counts when nothing explicit fired
    label = max(scores, key=scores.get)
    # buyers who mention yield/ROI are investors; keep investor only if it clearly outranks buyer
    if label == "investor" and scores.get("buyer", 0) >= scores["investor"] + 3:
        label = "buyer"
    top = scores[label]
    it.label = label
    it.evidence = evidence[label][:4]
    it.wants_agent = bool(_hits(BROKER_REQUEST, t))
    ready = _hits(STAGE_READY, t)
    short = _hits(STAGE_SHORTLIST, t)
    research = _hits(STAGE_RESEARCH, t)
    if ready or it.wants_agent:
        it.stage = "ready"
        it.evidence += ready[:2]
    elif short:
        it.stage = "shortlisting"
        it.evidence += short[:1]
    elif research or label:
        it.stage = "research"

    fill_entities(it, raw)
    if it.label == "seeker" and it.budget_max:
        # UAE rule of thumb: annual rents rarely exceed ~250k for apartments; purchases start well above 600k.
        if it.budget_max >= 600_000:
            it.label, it.purchase_type = "buyer", it.purchase_type or "ready"
        elif it.budget_max <= 250_000:
            it.label, it.purchase_type = "renter", "rent"
    # Confidence: more independent matches + concrete entities => higher.
    specifics = sum(x is not None and x != [] for x in (it.budget_max, it.bedrooms, it.timeline_days)) + bool(it.areas)
    it.confidence = round(min(0.45 + 0.12 * min(top / 3, 3) + 0.05 * specifics + (0.1 if it.wants_agent else 0), 0.98), 2)
    if spam_strength == 1:
        it.confidence = round(it.confidence * 0.8, 2)
    return it
