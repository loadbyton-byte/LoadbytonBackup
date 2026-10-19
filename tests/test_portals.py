from pathlib import Path

import pytest

from leadengine.connectors.forms import parse_portal_email
from leadengine.models import Signal

FX = Path(__file__).parent / "fixtures"


def body(name):
    return (FX / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("portal,fixture,name,phone,email,ref", [
    ("bayut", "portal_bayut.txt", "Sara Ahmed", "+971 50 765 4321", "Sara@Example.com", "BAY-12345"),
    ("propertyfinder", "portal_propertyfinder.txt", "Omar Khan", "0507654321", "omar@example.com", "PF-998"),
    ("dubizzle", "portal_dubizzle.txt", "Lina Haddad", "00971501112222", "lina@example.com", "DBZ-77"),
    ("bayut", "portal_bayut_ar.txt", "خالد المنصوري", "٠٥٠٩٩٩٨٨٨٧", "khalid@example.com", "BAY-5551"),
])
def test_portal_parsers(portal, fixture, name, phone, email, ref):
    s = parse_portal_email(portal, "New enquiry", body(fixture), "msg-1")
    assert (s.author_name, s.phone, s.email, s.listing_ref) == (name, phone, email, ref)
    assert s.platform == f"portal:{portal}" and s.source_type == "portal_inquiry" and s.text


def mk(pipe, n, ref, phone="0507654321", portal="bayut"):
    return pipe.ingest(Signal(platform=f"portal:{portal}", external_id=f"p{n}", source_type="portal_inquiry",
                              text="is this still available?", phone=phone, listing_ref=ref)).lead_id


def lead(pipe, lid):
    import json
    r = pipe.db.one("SELECT score, breakdown FROM leads WHERE id=?", (lid,))
    return r["score"], json.loads(r["breakdown"])


def test_repeat_enquiry_bonus_at_three_distinct_listings(pipe):
    lid = mk(pipe, 1, "A")
    mk(pipe, 2, "B")
    s2, bd2 = lead(pipe, lid)
    assert "repeat_enquiry" not in bd2
    mk(pipe, 3, "C")
    s3, bd3 = lead(pipe, lid)
    assert bd3["repeat_enquiry"] == 6 and s3 == s2 + 6


def test_same_listing_twice_counts_once(pipe):
    lid = mk(pipe, 1, "A")
    mk(pipe, 2, "A")
    mk(pipe, 3, "B")
    assert "repeat_enquiry" not in lead(pipe, lid)[1]


def test_old_enquiries_do_not_count(pipe):
    from datetime import datetime, timedelta, timezone
    old = datetime.now(timezone.utc) - timedelta(days=20)
    for i, ref in enumerate("ABC"):
        pipe.ingest(Signal(platform="portal:bayut", external_id=f"o{i}", source_type="portal_inquiry", text="is this still available?",
                           phone="0507654321", listing_ref=ref, created_at=old))
    assert "repeat_enquiry" not in lead(pipe, 1)[1]


def test_cross_portal_merge_by_phone(pipe):
    a = mk(pipe, 1, "A", phone="0507654321", portal="bayut")
    b = mk(pipe, 2, "B", phone="+971 50 765 4321", portal="propertyfinder")
    assert a == b and pipe.db.one("SELECT COUNT(*) n FROM leads")["n"] == 1
    assert set(__import__("json").loads(pipe.db.one("SELECT platforms FROM leads")["platforms"])) == {"portal:bayut", "portal:propertyfinder"}
