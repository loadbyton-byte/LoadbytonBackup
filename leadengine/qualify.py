"""WhatsApp qualification as a pure state machine: (state, inbound text) -> (state, reply, done).

Steps: intent -> area -> budget -> timeline -> consent. Anything the person already said is not asked again; an answer we can't
parse is re-asked once, then skipped. Replies follow the person's language (English / Arabic)."""
from __future__ import annotations

import re
import unicodedata

from .nlp.areas import extract_areas
from .nlp.intent import classify, parse_budget, parse_timeline
from .nlp.text import detect_language, normalize

STEPS = ("intent", "area", "budget", "timeline", "consent")
_STOP_WORDS = {"stop", "unsubscribe", "cancel", "optout", "ايقاف", "وقف", "الغاء", "stop all", "stop messages", "stop messaging",
               "stop please", "please stop", "opt out", "الغاء الاشتراك", "توقف", "اوقف", "remove", "remove all"}
_FILLERS = {"please", "pls", "plz", "me", "now", "thanks", "thank", "you", "من", "فضلك", "لو", "سمحت", "الان"}
_YES = {"yes", "y", "ok", "okay", "sure", "yep", "yeah", "نعم", "ايوه", "ايوا", "تمام", "اوكي", "موافق"}
_NO = {"no", "n", "nope", "لا", "كلا"}
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_EXPLORING = re.compile(normalize(r"\b(just (looking|exploring|browsing)|not sure|no rush|exploring)\b|بس استكشاف|لاحقا"))

Q = {
    "en": {
        "intent": "Thanks for your message! Are you looking to buy, rent or sell?",
        "area": "Which area or community are you interested in? (e.g. Dubai Marina, JVC, Downtown)",
        "budget": "What is your approximate budget in AED? (e.g. 1.5M, or 90k per year for rent)",
        "timeline": "When are you hoping to move or buy? (e.g. this month, in 3 months, just exploring)",
        "consent": "Thank you! May one of our agents contact you here on WhatsApp with matching options? Reply YES to agree. "
                   "You can send STOP at any time.",
        "retry": "Sorry, I didn't catch that. ",
        "yes": "Great - an agent will message you shortly. Send STOP at any time to opt out.",
        "no": "No problem - we won't send anything unless you message us. Have a great day!",
    },
    "ar": {
        "intent": "شكراً لرسالتك! هل تبحث عن شراء أو إيجار أو بيع؟",
        "area": "ما هي المنطقة التي تهمك؟ (مثال: دبي مارينا، جي في سي، داون تاون)",
        "budget": "ما هي ميزانيتك التقريبية بالدرهم؟ (مثال: 1.5 مليون، أو 90 ألف سنوياً للإيجار)",
        "timeline": "متى تخطط للانتقال أو الشراء؟ (مثال: هذا الشهر، خلال 3 أشهر، مجرد استكشاف)",
        "consent": "شكراً لك! هل تسمح لأحد وكلائنا بالتواصل معك هنا على واتساب بخيارات مناسبة؟ أرسل نعم للموافقة. "
                   "ويمكنك إرسال إيقاف في أي وقت.",
        "retry": "عذراً، لم أفهم. ",
        "yes": "ممتاز - سيراسلك أحد الوكلاء قريباً. أرسل إيقاف في أي وقت لإلغاء الاشتراك.",
        "no": "لا مشكلة - لن نرسل لك شيئاً إلا إذا راسلتنا. نتمنى لك يوماً سعيداً!",
    },
}


def _words(text: str) -> str:
    return " ".join(_PUNCT.sub(" ", normalize(unicodedata.normalize("NFKC", text))).split())


def is_stop(text: str) -> bool:
    """Exact opt-out keywords (after dropping polite fillers like 'please'/'من فضلك'): 'Can I stop by tomorrow?' must NOT unsubscribe anyone."""
    core = " ".join(w for w in _words(text).split() if w not in _FILLERS)
    return core in {_words(w) for w in _STOP_WORDS}


def _intent(text: str) -> str | None:
    t = normalize(text)
    if re.search(normalize(r"\b(sell|selling|valuation)\b|ابيع|بيع"), t):
        return "sell"
    if re.search(normalize(r"\b(rent|renting|lease)\b|ايجار|استاجر"), t):
        return "rent"
    if re.search(normalize(r"\b(buy|buying|purchase|invest\w*)\b|اشتري|شراء|استثمار"), t):
        return "buy"
    label = classify(text).label
    return {"buyer": "buy", "investor": "buy", "renter": "rent", "seller": "sell", "landlord": "sell"}.get(label)


def _parse(step: str, text: str):
    """Return the parsed answer for `step`, or None when the text doesn't answer it."""
    if step == "intent":
        return _intent(text)
    if step == "area":
        areas, emirates = extract_areas(text)
        return areas[0] if areas else (emirates[0] if emirates else None)
    if step == "budget":
        return parse_budget(normalize(text))[1]
    if step == "timeline":
        days = parse_timeline(normalize(text))
        return days if days is not None else (365 if _EXPLORING.search(normalize(text)) else None)
    if step == "consent":
        first = (_words(text).split() or [""])[0]
        return True if first in _YES else False if first in _NO else None
    return None


def advance(state: dict, text: str) -> tuple[dict, str, bool]:
    st = {"step": "start", "answers": {}, "retries": {}, "lang": "en", **(state or {})}
    st["answers"], st["retries"] = dict(st["answers"]), dict(st["retries"])
    lang = detect_language(text)
    if lang in ("ar", "en"):
        st["lang"] = lang
    q = Q[st["lang"]]

    if st["step"] == "start":
        for step in ("intent", "area", "budget", "timeline"):          # take whatever the first message already says
            val = _parse(step, text)
            if val is not None:
                st["answers"][step] = val
    elif st["step"] in STEPS:
        step = st["step"]
        val = _parse(step, text)
        if val is not None:
            st["answers"][step] = val
        elif st["retries"].get(step, 0) < 1:
            st["retries"][step] = st["retries"].get(step, 0) + 1
            return st, q["retry"] + q[step], False
        else:
            st["answers"][step] = False if step == "consent" else None   # skip: unanswered twice (no answer to consent = no)

    if st["answers"].get("consent") is not None:
        st["step"] = "done"
        return st, q["yes"] if st["answers"]["consent"] else q["no"], True
    for step in STEPS:
        if step not in st["answers"]:
            st["step"] = step
            return st, q[step], False
    st["step"] = "done"
    return st, q["no"], True
