import sqlite3

import pytest

from leadengine.capture import RateLimiter, handle_capture
from leadengine.config import Settings
from leadengine.db import DB
from leadengine.markets.cities import resolve
from leadengine.models import Signal
from leadengine.pipeline import Pipeline
from leadengine.roi import record_spend

BUY = "Looking to buy a 2BR in Dubai Marina, budget AED 1.8M, cash buyer, viewing this week"


def lead(pipe, lead_id):
    return pipe.db.one("SELECT * FROM leads WHERE id=?", (lead_id,))


def sig(i, text=BUY, **kw):
    return Signal(platform=kw.pop("platform", "web"), external_id=f"o{i}", source_type=kw.pop("source_type", "lead_form"), text=text, **kw)


def test_migration_adds_columns_to_an_existing_database(tmp_path):
    path = str(tmp_path / "old.db")
    db = DB(path)
    for table, col in (("leads", "origin_country"), ("leads", "origin_city"), ("leads", "origin_basis"), ("signals", "origin_country"),
                       ("signals", "origin_city"), ("signals", "origin_basis"), ("spend", "country"), ("spend", "city")):
        db.conn.execute(f"ALTER TABLE {table} DROP COLUMN {col}")                     # simulate a database from before this feature
    db.conn.commit(); db.conn.close()
    for _ in range(2):                                                      # reopening is a no-op
        db = DB(path)
        for table, cols in (("leads", ("origin_country", "origin_city", "origin_basis")), ("signals", ("origin_country", "origin_city", "origin_basis")),
                            ("spend", ("country", "city"))):
            have = {r[1] for r in db.conn.execute(f"PRAGMA table_info({table})")}
            assert set(cols) <= have, (table, cols)
        db.conn.close()


def test_ingest_sets_origin_from_phone_text_and_form(pipe):
    r = pipe.ingest(sig(1, phone="+447911123456"))
    assert (lead(pipe, r.lead_id)["origin_country"], lead(pipe, r.lead_id)["origin_basis"]) == ("GB", "phone")
    r = pipe.ingest(sig(2, phone="0501234567", extra={"country": "Pakistan", "city": "Lahore"}))
    row = lead(pipe, r.lead_id)
    assert (row["origin_country"], row["origin_basis"], row["origin_city"]) == ("PK", "form", resolve("Lahore").id)
    r = pipe.ingest(Signal(platform="reddit", external_id="rr1", source_type="public_post", author_handle="mumbai_guy_01",
                           text="I'm in Mumbai, want a 2BR in Dubai Marina, budget AED 2M, buying this month"))
    row = lead(pipe, r.lead_id)
    assert (row["origin_country"], row["origin_basis"]) == ("IN", "text")
    r = pipe.ingest(Signal(platform="reddit", external_id="rr2", source_type="public_post", author_handle="no_origin_guy",
                           text="Looking to buy a 2BR in Dubai Marina, budget AED 2M, buying this month"))
    row = lead(pipe, r.lead_id)
    assert (row["origin_country"], row["origin_city"], row["origin_basis"]) == (None, None, None)


def test_weaker_basis_never_overrides_stronger(pipe):
    a = pipe.ingest(sig(1, phone="0501112222", extra={"country": "PK"})).lead_id
    pipe.ingest(Signal(platform="reddit", external_id="x", source_type="public_post", phone="0501112222", text="I'm in London, " + BUY))
    assert (lead(pipe, a)["origin_country"], lead(pipe, a)["origin_basis"]) == ("PK", "form")
    b = pipe.ingest(Signal(platform="reddit", external_id="y", source_type="public_post", phone="0503334444", text="I'm in London, " + BUY)).lead_id
    assert lead(pipe, b)["origin_basis"] == "phone" or lead(pipe, b)["origin_basis"] == "text"
    pipe.ingest(sig(3, phone="0503334444", extra={"country": "PK"}))
    assert (lead(pipe, b)["origin_country"], lead(pipe, b)["origin_basis"]) == ("PK", "form")
    c = pipe.ingest(sig(4, phone="+447911000000")).lead_id
    pipe.ingest(Signal(platform="reddit", external_id="z", source_type="public_post", phone="+447911000000", text="I'm in Mumbai, " + BUY))
    assert (lead(pipe, c)["origin_country"], lead(pipe, c)["origin_basis"]) == ("GB", "phone")      # phone outranks a later text origin


def test_merged_profiles_keep_the_strongest_origin(pipe):
    a = pipe.ingest(Signal(platform="reddit", external_id="m1", source_type="public_post", author_handle="shared_handle_77",
                           text="I'm in London, " + BUY)).lead_id
    b = pipe.ingest(sig(9, phone="0507778888", extra={"country": "India"})).lead_id
    assert a != b
    merged = pipe.ingest(Signal(platform="reddit", external_id="m2", source_type="public_post", author_handle="shared_handle_77",
                                phone="0507778888", text=BUY)).lead_id
    row = lead(pipe, merged)
    assert (row["origin_country"], row["origin_basis"]) == ("IN", "form")


def test_erasure_removes_origin(pipe):
    lid = pipe.ingest(sig(1, phone="+447911123456")).lead_id
    assert pipe.erase(lid)
    assert pipe.db.one("SELECT 1 FROM leads WHERE id=?", (lid,)) is None and pipe.db.one("SELECT 1 FROM signals WHERE lead_id=?", (lid,)) is None


def test_capture_accepts_optional_country_and_city(pipe, settings):
    st = Settings(db_path=":memory:", capture_origins=["https://a.example"])
    payload = {"phone": "0501234567", "consent": True, "message": BUY, "country": "India", "city": "Bangalore"}
    code, _ = handle_capture(pipe, st, payload, "https://a.example", "1.1.1.1", RateLimiter(5, 60))
    assert code == 200
    row = pipe.db.one("SELECT * FROM leads ORDER BY id DESC LIMIT 1")
    assert (row["origin_country"], row["origin_basis"]) == ("IN", "form")
    code, _ = handle_capture(pipe, st, {**payload, "phone": "0509999999", "city": "x" * 500}, "https://a.example", "1.1.1.2", RateLimiter(5, 60))
    assert code == 200
    assert pipe.db.one("SELECT origin_basis FROM leads ORDER BY id DESC LIMIT 1")["origin_basis"] == "form"      # country alone still valid


def test_record_spend_tagging():
    db = DB(":memory:")
    i = record_spend(db, "reddit", None, "2026-10", 100, city="London")
    row = db.one("SELECT * FROM spend WHERE id=?", (i,))
    assert (row["country"], row["city"]) == ("GB", resolve("London").id)
    with pytest.raises(ValueError):
        record_spend(db, "reddit", None, "2026-10", 100, country="PK", city="London")
    with pytest.raises(ValueError):
        record_spend(db, "reddit", None, "2026-10", 100, country="XX")
    with pytest.raises(ValueError):
        record_spend(db, "reddit", None, "2026-10", 100, city="Nowhereville")
    j = record_spend(db, "reddit", "c", "2026-10", 50)
    row = db.one("SELECT * FROM spend WHERE id=?", (j,))
    assert row["country"] is None and row["city"] is None
    k = record_spend(db, "reddit", None, "2026-10", 5, country="in")
    assert db.one("SELECT country FROM spend WHERE id=?", (k,))["country"] == "IN"


def test_backfill_origin_is_idempotent(pipe):
    db = pipe.db
    with db.tx() as c:
        for n, phone in ((1, None), (2, "+447911123456"), (3, None)):
            c.execute("INSERT INTO leads(first_seen,last_seen,platforms,phone) VALUES('2026-10-01T00:00:00+00:00','2026-10-01T00:00:00+00:00','[]',?)", (phone,))
        for lid, text in ((1, "I'm in Mumbai and want a villa"), (2, "want a villa"), (3, "want a villa")):
            c.execute("INSERT INTO signals(lead_id,platform,external_id,source_type,text,created_at,ingested_at,intent) VALUES(?,?,?,?,?,?,?,?)",
                      (lid, "web", f"bf{lid}", "lead_form", text, "2026-10-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00", "{}"))
    assert pipe.backfill_origin() == {"updated": 2, "unresolved": 1}
    assert pipe.backfill_origin() == {"updated": 0, "unresolved": 1}
    assert db.one("SELECT origin_country c FROM leads WHERE id=1")["c"] == "IN" and db.one("SELECT origin_basis b FROM leads WHERE id=2")["b"] == "phone"
