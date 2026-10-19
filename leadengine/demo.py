"""Synthetic demo signals (English + Arabic, all platforms). Fictional handles — no real people."""
from __future__ import annotations

from datetime import timedelta

from .models import Signal, utcnow

_D = [
    # platform, source_type, handle, text, hours_ago, extra
    ("reddit", "public_post", "demo_user_1", "Looking to buy a 2BR in Dubai Marina, budget AED 1.8M, cash buyer. In Dubai this week for viewings — can anyone recommend a good agent?", 2, {}),
    ("reddit", "public_post", "demo_user_2", "Is it worth investing in JVC off-plan for rental yield? Thinking 1M budget, mortgage pre-approved, golden visa is a bonus.", 30, {}),
    ("reddit", "public_post", "demo_user_3", "Moving to Abu Dhabi next month, need 1 bedroom in Al Reem, around 70k per year, 4 cheques ok", 20, {}),
    ("reddit", "public_post", "demo_user_4", "Dubai Hills vs Arabian Ranches for a family villa? comparing 3 bed townhouse, budget 3-4M", 60, {}),
    ("reddit", "public_post", "demo_user_5", "I want to sell my 2 bed apartment in Business Bay, how much is it worth right now?", 48, {}),
    ("reddit", "public_post", "promo_bot", "EXCLUSIVE LAUNCH! Limited units, book now. WhatsApp +971501234567 BRN 12345", 5, {}),
    ("reddit", "public_post", "sunset_fan", "What a beautiful sunset over Dubai Marina tonight", 4, {}),
    ("facebook", "public_comment", "fb_demo_a", "ابغى اشتري شقه غرفتين وصاله في داون تاون دبي ميزانيتي 2 مليون ومستعجل", 6, {}),
    ("facebook", "public_post", "fb_demo_b", "ابحث عن شقه للايجار في الشارقه قريبه من المواصلات", 70, {}),
    ("instagram", "public_comment", "ig_demo_a", "Interested in Palm Jumeirah penthouse, planning to buy this year, cash", 12, {}),
    ("instagram", "public_comment", "ig_demo_b", "how much for a studio in Business Bay?", 8, {}),
    ("tiktok", "public_comment", "tt_demo_a", "thinking about buying off plan in Dubai South, what's the payment plan? first time buyer", 15, {}),
    ("tiktok", "public_comment", "tt_demo_b", "Looking to rent a 1BR in JLT asap, budget 85k", 3, {}),
    ("linkedin", "public_post", "li_demo_a", "Relocating to Dubai in Q1 with family. Looking to buy a villa in Arabian Ranches or Damac Hills, budget 3.5M. Any broker recommendations?", 26, {}),
    ("linkedin", "public_post", "li_demo_b", "Planning to invest in Dubai real estate for passive income, buy-to-let in Business Bay, 1.2M budget, cash.", 90, {}),
    ("facebook", "lead_form", "", "", 1, {"budget": "AED 2.2M", "preferred_area": "Dubai Hills", "bedrooms": "3", "looking_to": "buy"}),
    ("tiktok", "lead_form", "", "", 9, {"budget": "900k", "preferred_area": "Dubai South", "looking_to": "buy off plan"}),
    ("portal:bayut", "portal_inquiry", "", "Hi, is the 2 bed in Marina still available? Can I view this weekend?", 0.5, {}),
]
_PEOPLE = {  # forms/portals arrive with first-party contact details (fictional numbers)
    "facebook:lead_form": ("Demo Lead One", "0501110001", "demo1@example.com", True),
    "tiktok:lead_form": ("Demo Lead Two", "0502220002", "demo2@example.com", True),
    "portal:bayut:portal_inquiry": ("Demo Lead Three", "0503330003", "demo3@example.com", False),
}


def demo_signals() -> list[Signal]:
    now = utcnow()
    out = []
    for i, (platform, st, handle, text, hrs, extra) in enumerate(_D):
        name = phone = email = None
        consent = False
        key = f"{platform}:{st}"
        if key in _PEOPLE:
            name, phone, email, consent = _PEOPLE[key]
        out.append(Signal(platform=platform, external_id=f"demo-{i}", source_type=st, text=text, author_handle=handle or None,
                          author_name=name, phone=phone, email=email, consent_marketing=consent,
                          consent_text="I agree to be contacted by the agency about properties." if consent else None,
                          created_at=now - timedelta(hours=hrs), extra=extra))
    # same person across two platforms -> identity resolution + corroboration bonus
    out.append(Signal(platform="instagram", external_id="demo-x1", source_type="public_comment", author_handle="demo_user_1",
                      text="Still searching for a 2 bedroom in Dubai Marina, ready to book viewings", created_at=now - timedelta(hours=1)))
    return out


_PROJECTS_CSV = """name,developer,area,emirate,min_price,max_price,bedrooms,handover,payment_plan
Demo Marina Tower,Demo Developer A,Dubai Marina,Dubai,1.3M,2.6M,"1-3",2030,60/40
Demo Hills Residences,Demo Developer B,Dubai Hills Estate,Dubai,2.1M,4.5M,"2-4",2029,70/30
Demo South Gardens,Demo Developer C,Dubai South,Dubai,850000,1.5M,"studio,1,2",2028,80/20
Demo Downtown Edge,Demo Developer A,Downtown Dubai,Dubai,1.9M,3.2M,"1-2",2031,50/50
"""
_AGENTS = [("Layla Hassan", ("en", "ar"), ("Dubai Marina", "Downtown Dubai")), ("Omar Khan", ("en",), ("Jumeirah Village Circle", "Business Bay")),
           ("Sara Ahmed", ("en", "ar"), ("Dubai Hills Estate", "Arabian Ranches"))]


def seed_demo(pipe) -> dict:
    """Seed agents, inventory, spend, signals, a first contact and a viewing. Idempotent: safe to call repeatedly."""
    from datetime import datetime, timedelta, timezone

    from .inventory import import_projects_csv
    from .roi import record_spend
    from .viewings import schedule

    db, out = pipe.db, {"agents": 0, "projects": 0}
    if not db.one("SELECT 1 FROM agents"):                       # agents first so hot leads are routed on ingest
        for name, langs, areas in _AGENTS:
            pipe.distribution.add_agent(name, languages=langs, areas=areas, phone="+97150000000")
            out["agents"] += 1
    if not db.one("SELECT 1 FROM projects"):
        out["projects"] = import_projects_csv(db, _PROJECTS_CSV)["imported"]
    if not db.one("SELECT 1 FROM spend"):
        for source, amount in (("reddit", 1200), ("facebook", 3000), ("tiktok", 900), ("linkedin", 1500)):
            record_spend(db, source, "demo", "2026-10", amount)
    out.update(pipe.ingest_many(demo_signals()))
    out.setdefault("created", 0)
    if not db.one("SELECT 1 FROM viewings"):
        top = db.one("SELECT id, first_seen FROM leads WHERE primary_area='Dubai Marina' ORDER BY score DESC LIMIT 1")
        if top:
            schedule(db, top["id"], datetime.now() + timedelta(days=1), duration_min=45, location="Demo Marina Tower sales gallery")
        # a first contact within SLA on one lead, and a slow one on another, so the ROI view has response times
        for lead_id, minutes in ((r["id"], m) for r, m in zip(db.q("SELECT id FROM leads ORDER BY score DESC LIMIT 2"), (3, 40))):
            seen = datetime.now(timezone.utc) - timedelta(hours=3)        # demo leads "arrived" earlier today
            with db.tx() as c:
                c.execute("UPDATE leads SET first_seen=? WHERE id=?", (seen.isoformat(), lead_id))
                c.execute("INSERT INTO activities(lead_id,ts,kind) VALUES(?,?,?)", (lead_id, (seen + timedelta(minutes=minutes)).isoformat(), "status:contacted"))
                c.execute("UPDATE leads SET status='contacted' WHERE id=? AND status='new'", (lead_id,))
    return out
