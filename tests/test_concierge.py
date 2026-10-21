import hashlib
import re

from fastapi.testclient import TestClient

from leadengine.api import create_app
from leadengine.config import Settings
from leadengine.db import DB
from leadengine.platform import site


def client():
    return TestClient(create_app(Settings(db_path=":memory:", api_key=""), DB(":memory:")))


def fields(html: str) -> dict:
    return dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)"', html))


def step(c, state, answer, lang="en", **extra):
    r = c.post("/p/concierge", data={"lang": lang, "token": state["token"], "step": state["step"], "answer": answer, **extra})
    return r, fields(r.text)


def start(c, lang="en"):
    r = c.get("/p/concierge", params={"lang": lang})
    assert r.status_code == 200
    return r, fields(r.text)


def leads(c):
    return c.get("/leads").json()


def test_full_english_flow_creates_a_lead_only_after_consent():
    c = client()
    r, f = start(c)
    assert f["step"] == "intent" and f["token"] == "" and "buy, rent or sell" in r.text
    for answer in ("I want to buy", "Dubai Marina", "2M", "this month"):
        r, f = step(c, f, answer)
        assert r.status_code == 200 and leads(c) == []
    assert f["step"] == "consent" and "yes or no" in r.text
    r, f = step(c, f, "yes")
    assert "Last step" in r.text and leads(c) == []                                   # consented, but no contact details yet: still no lead
    r = c.post("/p/concierge", data={"lang": "en", "token": f["token"], "step": "contact", "name": "Sara", "phone": "0501234567", "email": ""})
    assert r.status_code == 200 and "Thank you" in r.text
    got = leads(c)
    assert len(got) == 1 and got[0]["primary_area"] == "Dubai Marina" and got[0]["budget_max"] == 2_000_000.0 and got[0]["consent_status"] == "opted_in"
    assert got[0]["consent_evidence"]["text"].endswith("→ yes") and got[0]["timeline_days"] == 30
    assert c.app.state.db.one("SELECT campaign FROM signals")["campaign"] == "concierge"
    stats = dict(c.app.state.db.q("SELECT key,value FROM stats WHERE key LIKE 'concierge_%'"))
    assert stats == {"concierge_start": 1, "concierge_done": 1}
    assert c.app.state.db.one("SELECT COUNT(*) n FROM web_sessions")["n"] == 0        # the session is gone once the lead exists


def test_full_arabic_flow():
    c = client()
    r, f = start(c, "ar")
    assert 'dir="rtl"' in r.text and "شراء أو إيجار أو بيع" in r.text
    for answer in ("اشتري", "دبي مارينا", "2 مليون", "هذا الشهر"):
        r, f = step(c, f, answer, "ar")
        assert r.status_code == 200 and 'dir="rtl"' in r.text
    r, f = step(c, f, "نعم", "ar")
    assert "الخطوة الأخيرة" in r.text
    r = c.post("/p/concierge", data={"lang": "ar", "token": f["token"], "step": "contact", "name": "سارة", "phone": "0501234567"})
    assert r.status_code == 200 and "شكراً" in r.text
    got = leads(c)[0]
    assert got["primary_area"] == "Dubai Marina" and got["budget_max"] == 2_000_000.0 and got["consent_evidence"]["text"].endswith("→ نعم")


def test_language_follows_the_page_not_the_answer():
    c = client()
    r, f = start(c, "ar")
    r, f = step(c, f, "buy", "ar")                                                   # an English answer on the Arabic page
    assert 'dir="rtl"' in r.text and "المنطقة" in r.text


def test_no_stop_and_abandon_store_nothing():
    c = client()
    r, f = start(c)
    for a in ("buy", "Dubai Marina", "2M", "this month"):
        r, f = step(c, f, a)
    r, f2 = step(c, f, "no")
    assert "We have not saved anything" in r.text and leads(c) == [] and c.app.state.db.one("SELECT COUNT(*) n FROM web_sessions")["n"] == 0
    r, f = start(c)
    r, f = step(c, f, "buy")
    r, _ = step(c, f, "STOP")
    assert "We have not saved anything" in r.text and leads(c) == []
    r, f = start(c)                                                                   # abandoned after the first answer: a row exists, no lead
    r, f = step(c, f, "buy")
    assert c.app.state.db.one("SELECT COUNT(*) n FROM web_sessions")["n"] == 1 and leads(c) == []
    c.app.state.db.conn.execute("UPDATE web_sessions SET expires_at='2000-01-01T00:00:00+00:00'")
    c.app.state.db.conn.commit()
    assert c.app.state.pipeline.compliance.purge_expired()["sessions_deleted"] == 1


def test_token_is_stored_hashed_and_forged_or_expired_tokens_fail_cleanly():
    c = client()
    r, f = start(c)
    r, f = step(c, f, "buy")
    row = c.app.state.db.one("SELECT token_hash FROM web_sessions")
    assert row["token_hash"] != f["token"] and row["token_hash"] == hashlib.sha256(f["token"].encode()).hexdigest()
    for bad in ("x", "a" * 500, "../../etc/passwd", "<script>", f["token"][:-1] + ("A" if f["token"][-1] != "A" else "B")):
        r = c.post("/p/concierge", data={"lang": "en", "token": bad, "step": "area", "answer": "Dubai Marina"})
        assert r.status_code == 200 and "expired" in r.text and "<script>" not in r.text
    c.app.state.db.conn.execute("UPDATE web_sessions SET expires_at='2000-01-01T00:00:00+00:00'")
    c.app.state.db.conn.commit()
    r, _ = step(c, f, "Dubai Marina")
    assert "expired" in r.text
    r, f = start(c)
    for a in ("buy", "Dubai Marina", "2M", "this month", "yes"):
        r, f = step(c, f, a)
    token = f["token"]
    body = {"lang": "en", "token": token, "step": "contact", "name": "Sara", "phone": "0501234567"}
    assert "Thank you" in c.post("/p/concierge", data=body).text
    assert "expired" in c.post("/p/concierge", data={**body, "phone": "0507654321"}).text      # a finished token cannot create a second lead
    assert len(leads(c)) == 1


def test_replay_of_an_old_step_is_idempotent():
    c = client()
    r, f = start(c)
    r1, f1 = step(c, f, "buy")
    assert f1["step"] == "area"
    r2, f2 = step(c, f, "buy")                                                        # the old step again
    assert f2["step"] == "area"
    row = c.app.state.db.one("SELECT state FROM web_sessions")
    import json
    assert json.loads(row["state"])["answers"] == {"intent": "buy"}


def test_contact_step_validation_and_cannot_be_bypassed():
    c = client()
    r, f = start(c)
    r, f = step(c, f, "buy")
    r = c.post("/p/concierge", data={"lang": "en", "token": f["token"], "step": "contact", "name": "X", "phone": "0501234567"})
    assert "Last step" not in r.text and leads(c) == []                                # a fresh session cannot skip to the contact step
    for a in ("buy", "Dubai Marina", "2M", "this month", "yes"):
        r, f = step(c, f, a)
    r = c.post("/p/concierge", data={"lang": "en", "token": f["token"], "step": "contact", "name": "X", "phone": "12", "email": "bad"})
    assert r.status_code == 422 and "valid phone number" in r.text and leads(c) == []
    r = c.post("/p/concierge", data={"lang": "en", "token": f["token"], "step": "contact", "name": "X", "phone": "0501234567", "website": "spam"})
    assert r.status_code == 200 and leads(c) == []                                    # honeypot looks successful, stores nothing


def test_concierge_pages_headers_and_escaping():
    c = client()
    r, f = start(c)
    assert "no-store" in r.headers["cache-control"] and "noindex" in r.text and "script-src 'none'" in r.headers["content-security-policy"]
    r, f2 = step(c, f, "<script>alert(1)</script>")
    assert "<script>alert" not in r.text and "Sorry, I didn" in r.text                  # unparseable answer: re-asked once, escaped


def test_purge_deletes_only_expired_sessions():
    c = client()
    for _ in range(2):
        r, f = start(c)
        step(c, f, "buy")
    c.app.state.db.conn.execute("UPDATE web_sessions SET expires_at='2000-01-01T00:00:00+00:00' WHERE rowid=(SELECT MIN(rowid) FROM web_sessions)")
    c.app.state.db.conn.commit()
    assert c.app.state.pipeline.compliance.purge_expired()["sessions_deleted"] == 1
    assert c.app.state.db.one("SELECT COUNT(*) n FROM web_sessions")["n"] == 1
