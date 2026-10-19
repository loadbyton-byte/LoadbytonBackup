"""Outreach *drafts* — never auto-sent. Each draft carries the compliance verdict for its channel,
the mandatory agent/permit disclosure, and an opt-out line. A human approves and sends."""
from __future__ import annotations

from .compliance import Compliance, Decision
from .config import Settings

_LABEL_EN = {
    "buyer": "buying", "investor": "investing", "renter": "renting", "seller": "selling", "landlord": "renting out",
}


def _where(lead: dict) -> str:
    return lead.get("primary_area") or lead.get("emirate") or "the UAE"


def _budget(lead: dict, lang: str) -> str:
    b = lead.get("budget_max")
    if not b:
        return ""
    amt = f"{b/1e6:.1f}M" if b >= 1e6 else f"{b/1e3:.0f}K"
    return f" (around AED {amt})" if lang == "en" else f" (بحدود {amt} درهم)"


def draft(lead: dict, channel: str, comp: Compliance, settings: Settings) -> dict:
    lang = "ar" if lead.get("language") == "ar" else "en"
    verdict: Decision = comp.can_contact(lead, channel)
    label = lead.get("label") or "buyer"
    where, budget = _where(lead), _budget(lead, lang)
    sig = f"{settings.agent_name} | {settings.agency_name} | BRN {settings.brn}"
    notes: list[str] = []

    if channel == "public_reply":
        # Helpful, public, no personal data requested, no phone number in the open.
        if lang == "ar":
            body = (f"مرحباً! بخصوص {where}: يعتمد الاختيار على الميزانية{budget} والغرض (سكن أو استثمار). "
                    "يسعدني أشاركك مقارنة بين المناطق والأسعار الفعلية. أرسل لي رسالة خاصة إذا تحب نكمل بالتفصيل.")
        else:
            body = (f"Hi! For {where}, the right pick really depends on budget{budget} and whether you're buying to live in or invest. "
                    "Happy to share a quick comparison of recent transaction prices and communities — send me a DM "
                    "if you'd like me to pull that together for you.")
        notes.append("Post only after human review; follow the subreddit/group rules and disclose you are an agent.")
        notes.append("Do not paste phone numbers or ask for personal details in a public thread.")
    elif channel in ("whatsapp", "dm", "sms"):
        verb = _LABEL_EN.get(label, "properties")
        if lang == "ar":
            body = (f"السلام عليكم {lead.get('display_name') or ''}، معك {settings.agent_name} من {settings.agency_name}. "
                    f"شفت اهتمامك في {where}{budget}. تحب أرسل لك خيارات مناسبة ونرتب معاينة؟\nللإيقاف أرسل: إيقاف")
        else:
            body = (f"Hi {lead.get('display_name') or 'there'}, this is {settings.agent_name} from {settings.agency_name}. "
                    f"You showed interest in {verb} in {where}{budget}. Want me to send a short list of matching options and "
                    f"set up viewings?\nReply STOP to opt out.")
        notes.append("WhatsApp: outside the 24h window use a pre-approved template message.")
    elif channel == "email":
        subj = f"Options in {where}" if lang == "en" else f"خيارات في {where}"
        body = (f"Subject: {subj}\n\nHi {lead.get('display_name') or 'there'},\n\nThanks for your interest in {where}{budget}. "
                f"I've shortlisted a few options that fit — can I send them over?\n\n{sig}\n"
                "You're receiving this because you asked us to get in touch. Reply 'unsubscribe' to stop.")
    else:  # call
        body = (f"Call opener: \"Hi, this is {settings.agent_name} from {settings.agency_name}. You asked about {where}"
                f"{budget} — is now a good time for two minutes?\"  Mention source of enquiry; offer to stop calling if asked.")
    footer = "" if channel == "email" else f"\n— {sig} | Permit: {settings.trakheesi}"
    return {"channel": channel, "language": lang, "body": body + footer, "compliance": verdict.to_dict(),
            "requires_human_approval": True, "notes": notes}
