"""Regression tests for the market-intelligence checkpoint review."""
import time
from datetime import datetime, timezone

import pytest

from leadengine.db import DB
from leadengine.identity import normalize_phone
from leadengine.markets.cities import resolve
from leadengine.markets.intel import market_report
from leadengine.markets.origin import country_for_phone, resolve_origin
from leadengine.roi import record_spend

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("text", [
    "Looking for a place to live in Dubai Marina", "best area to live in Dubai?", "I want to live in Dubai", "I would love to live in Dubai",
    "I don't live in London anymore", "do not live in London", "the developer is based in London", "agent based in Mumbai",
    "I'm in Dubai next week, coming from nowhere", "I'm in! London villa please", "I'm in. London?", "We love living in Dubai"])
def test_phrase_false_positives(text):
    o = resolve_origin(text, None)
    assert o is None, (text, o)


@pytest.mark.parametrize("text,country", [("I live in London", "GB"), ("we live in London", "GB"), ("I'm based in London", "GB"),
                                           ("I am living in London", "GB"), ("انا من لندن", "GB"), ("أنا حاليا في لندن", "GB"),
                                           ("I'm in Dubai next week but I'm in London now", "GB")])
def test_first_person_phrases_still_work(text, country):
    o = resolve_origin(text, None)
    assert o and o.country == country, text


def test_alias_must_end_at_a_word_boundary():
    assert resolve_origin("I'm in Londonderry", None) is None


@pytest.mark.parametrize("value,iso", [("UK", "GB"), ("uae", "AE"), ("USA", "US"), ("KSA", "SA"), ("England", "GB"), ("Great Britain", "GB"),
                                       ("Turkey", "TR"), ("Ivory Coast", "CI"), ("Cote d'Ivoire", "CI"), ("Russian Federation", "RU"),
                                       ("Korea", "KR"), ("Türkiye", "TR"), ("India", "IN"), ("  pakistan ", "PK")])
def test_common_country_values(value, iso):
    assert resolve_origin(None, None, country=value).country == iso


def test_form_country_conflicting_with_city_keeps_the_country():
    o = resolve_origin(None, None, country="US", city="Birmingham")
    assert o.country in ("US", "GB")
    assert resolve_origin(None, None, city="london-gb").country == "GB"                     # catalogue ids are accepted too


def test_spend_accepts_country_names_and_raises_typed_errors():
    db = DB(":memory:")
    record_spend(db, "x", None, "2026-10", 1, country="UK")
    assert db.one("SELECT country FROM spend")["country"] == "GB"
    with pytest.raises(ValueError):
        record_spend(db, "x", None, "2026-10", 1, country="Narnia")


def test_arabic_indic_digits_and_jamaica_trinidad():
    assert normalize_phone("٠٥٠١٢٣٤٥٦٧") == "+971501234567" == normalize_phone("0501234567")
    assert country_for_phone("+٤٤٧٩١١١٢٣٤٥٦") == "GB" and country_for_phone("٠٥٠١٢٣٤٥٦٧") == "AE"
    assert country_for_phone("+18765550100") == "JM" and country_for_phone("+18685550100") == "TT" and country_for_phone("+12125550100") is None


def test_big_text_is_fast():
    text = "I'm in London. " * 1500
    t0 = time.perf_counter()
    resolve_origin(text, None)
    assert time.perf_counter() - t0 < 0.5


def seed(db, rows):
    with db.tx() as c:
        for i, (country, city, tier, status, days) in enumerate(rows):
            when = datetime.fromtimestamp(NOW.timestamp() - days * 86400, timezone.utc).isoformat()
            c.execute("INSERT INTO leads(first_seen,last_seen,platforms,tier,status,score,origin_country,origin_city,origin_basis) VALUES(?,?,?,?,?,?,?,?,?)",
                      (when, when, "[]", tier, status, 50, country, city, "phone"))


def test_spend_only_market_is_not_ranked_and_intl_share_ignores_domestic():
    db = DB(":memory:")
    seed(db, [("GB", None, "HOT", "new", 1)] * 5 + [("AE", None, "HOT", "new", 1)] * 95)
    record_spend(db, "x", None, "2026-10", 100, country="FR")
    r = market_report(db, NOW)
    ids = [f["id"] for f in r["focus"]]
    assert "FR" not in ids and "AE" not in ids and ids == ["GB"]
    gb = next(c for c in r["countries"] if c["id"] == "GB")
    assert gb["share_pct"] == 5 and gb["intl_share_pct"] == 100
    assert "high_share" in r["focus"][0]["reasons"]                                       # share among international leads only


def test_period_makes_growth_none_and_limit_none_returns_everything():
    db = DB(":memory:")
    seed(db, [("GB", None, "HOT", "new", d) for d in (1, 2, 3, 4, 5, 6, 30, 35, 40)])
    assert next(c for c in market_report(db, NOW)["countries"] if c["id"] == "GB")["growth_pct"] == 100
    assert next(c for c in market_report(db, NOW, period="2026-10")["countries"] if c["id"] == "GB")["growth_pct"] is None
    from leadengine.markets.cities import COUNTRIES
    db2 = DB(":memory:")
    seed(db2, [(cc, None, "HOT", "new", 1) for cc in COUNTRIES])
    assert len(market_report(db2, NOW, limit=None)["countries"]) == len(COUNTRIES) and len(market_report(db2, NOW)["countries"]) == 25


def test_untapped_skips_cities_of_countries_with_unlocated_leads():
    db = DB(":memory:")
    seed(db, [("GB", None, "HOT", "new", 1)] * 3)                                       # phone-only origins: country known, city unknown
    u = market_report(db, NOW)["untapped"]
    assert all(x["country"] != "GB" for x in u)


def test_backfill_never_touches_leads_that_already_have_an_origin():
    from leadengine.config import Settings
    from leadengine.pipeline import Pipeline
    pipe = Pipeline(DB(":memory:"), Settings(db_path=":memory:"))
    with pipe.db.tx() as c:
        c.execute("INSERT INTO leads(first_seen,last_seen,platforms,phone,origin_country,origin_basis) VALUES('2026-10-01T00:00:00+00:00','2026-10-01T00:00:00+00:00','[]','+447911123456','PK','form')")
        c.execute("INSERT INTO leads(first_seen,last_seen,platforms,phone) VALUES('2026-10-01T00:00:00+00:00','2026-10-01T00:00:00+00:00','[]','+919876543210')")   # no signals at all
    assert pipe.backfill_origin() == {"updated": 1, "unresolved": 0}
    assert pipe.db.one("SELECT origin_country c, origin_basis b FROM leads WHERE id=1")["c"] == "PK"
    assert pipe.db.one("SELECT origin_country c FROM leads WHERE id=2")["c"] == "IN"
