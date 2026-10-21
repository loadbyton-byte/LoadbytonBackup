import json
from datetime import datetime, timedelta, timezone

import pytest

from leadengine.db import DB
from leadengine.markets.cities import resolve
from leadengine.markets.intel import MEASURED_MIN, SHRINK_K, contact_window, market_report
from leadengine.roi import record_spend

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
LON, MUM = resolve("London").id, resolve("Mumbai").id


def seed(db, rows):
    """rows: (country, city, tier, status, score, days_ago[, name])"""
    with db.tx() as c:
        for i, r in enumerate(rows):
            country, city, tier, status, score, days = r[:6]
            name = r[6] if len(r) > 6 else f"Person {i}"
            when = (NOW - timedelta(days=days)).isoformat()
            c.execute("INSERT INTO leads(display_name,phone,email,first_seen,last_seen,platforms,tier,status,score,origin_country,origin_city,origin_basis) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (name, f"+4479{i:08d}", f"p{i}@x.example", when, when, "[]", tier, status, score, country, city, "phone" if country else None))


@pytest.fixture()
def db():
    return DB(":memory:")


def row(rows, id_):
    return next(r for r in rows if r["id"] == id_)


def test_counts_and_ratios(db):
    seed(db, [("GB", LON, "HOT", "won", 80, 1), ("GB", LON, "HOT", "new", 70, 2), ("GB", LON, "WARM", "new", 50, 3), ("GB", LON, "NURTURE", "new", 20, 4),
              ("IN", MUM, "WARM", "new", 40, 1), ("IN", MUM, "NURTURE", "new", 10, 2)])
    r = market_report(db, NOW)
    london, gb = row(r["cities"], LON), row(r["countries"], "GB")
    assert (london["leads"], london["qualified"], london["won"], london["share_pct"], london["avg_score"]) == (4, 3, 1, 67, 55.0)
    assert (gb["leads"], gb["qualified"], gb["won"]) == (4, 3, 1) and gb["name"] == "United Kingdom" and gb["top_city"] == "London"
    assert r["total_leads"] == 6 and r["known_origin_pct"] == 100


def test_spend_tagging_and_unallocated(db):
    seed(db, [("GB", LON, "HOT", "won", 80, 1), ("GB", LON, "HOT", "new", 70, 2), ("GB", LON, "WARM", "new", 50, 3), ("GB", LON, "NURTURE", "new", 20, 4),
              ("IN", MUM, "WARM", "new", 40, 1)])
    record_spend(db, "reddit", None, "2026-11", 1000, city="London")
    record_spend(db, "reddit", None, "2026-11", 500, country="IN")
    record_spend(db, "reddit", None, "2026-11", 200)
    r = market_report(db, NOW)
    london = row(r["cities"], LON)
    assert (london["spend_aed"], london["cost_per_lead"], london["cost_per_qualified"], london["cost_per_won"]) == (1000.0, 250.0, 333.33, 1000.0)
    assert row(r["countries"], "GB")["spend_aed"] == 1000.0 and row(r["countries"], "IN")["spend_aed"] == 500.0
    mumbai = row(r["cities"], MUM)
    assert mumbai["spend_aed"] == 0.0 and mumbai["cost_per_lead"] is None
    assert row(r["countries"], "IN")["cost_per_lead"] == 500.0
    assert r["unallocated_spend_aed"] == 200.0


def test_ratios_are_none_not_errors(db):
    seed(db, [("GB", LON, "NURTURE", "new", 10, 1)])
    record_spend(db, "reddit", None, "2026-11", 100, city="London")
    london = row(market_report(db, NOW)["cities"], LON)
    assert london["cost_per_won"] is None and london["cost_per_qualified"] is None and london["cost_per_lead"] == 100.0
    empty = market_report(DB(":memory:"), NOW)
    assert empty["known_origin_pct"] is None and empty["total_leads"] == 0 and empty["countries"] == [] and empty["cities"] == [] and empty["focus"] == []
    assert empty["untapped"]


def test_growth_windows(db):
    seed(db, [("GB", LON, "HOT", "new", 50, d) for d in (1, 2, 3, 4, 5, 6)] + [("GB", LON, "HOT", "new", 50, d) for d in (30, 35, 40)])
    assert row(market_report(db, NOW)["countries"], "GB")["growth_pct"] == 100
    db2 = DB(":memory:")
    seed(db2, [("GB", LON, "HOT", "new", 50, d) for d in (1, 2, 3)] + [("GB", LON, "HOT", "new", 50, d) for d in (30, 35)])
    assert row(market_report(db2, NOW)["countries"], "GB")["growth_pct"] is None


def test_period_filter_limits_leads_and_spend(db):
    seed(db, [("GB", LON, "HOT", "new", 50, 1), ("GB", LON, "HOT", "new", 50, 2), ("GB", LON, "HOT", "new", 50, 40)])        # 2 in Nov, 1 in Oct
    record_spend(db, "reddit", None, "2026-11", 100, city="London")
    record_spend(db, "reddit", None, "2026-10", 900, city="London")
    r = market_report(db, NOW, period="2026-11")
    assert row(r["cities"], LON)["leads"] == 2 and row(r["cities"], LON)["spend_aed"] == 100.0
    with pytest.raises(ValueError):
        market_report(db, NOW, period="Oct")


def test_shrinkage_and_basis(db):
    big = [("GB", LON, "HOT", "new", 50, 1)] * 50
    small = [("IN", MUM, "HOT", "new", 50, 1)]
    seed(db, big + small)
    f = {r["id"]: r for r in market_report(db, NOW)["focus"]}
    assert f["IN"]["basis"] == "editorial" and "low_sample" in f["IN"]["reasons"] and f["GB"]["basis"] == "measured"
    assert f["IN"]["leads"] == 1 and f["GB"]["leads"] == 50
    # exact formula: 20 GB leads of 40 known (share 50), half qualified, tier-1 prior 80
    db2 = DB(":memory:")
    seed(db2, [("GB", LON, "HOT", "new", 50, 1)] * 10 + [("GB", LON, "NURTURE", "new", 10, 1)] * 10 + [("IN", MUM, "NURTURE", "new", 10, 1)] * 20)
    g = {r["id"]: r for r in market_report(db2, NOW)["focus"]}["GB"]
    assert g["share_pct"] == 50 and g["score"] == round((20 * 80.0 + SHRINK_K * 80) / (20 + SHRINK_K), 1) == 80.0


def test_domestic_is_tabled_but_not_ranked(db):
    seed(db, [("AE", resolve("Dubai").id, "HOT", "new", 70, 1), ("AE", resolve("Sharjah").id, "WARM", "new", 50, 1), ("GB", LON, "HOT", "new", 50, 1)])
    r = market_report(db, NOW)
    assert row(r["countries"], "AE")["leads"] == 2 and {c["name"] for c in r["cities"]} >= {"Dubai", "Sharjah"}
    assert all(f["id"] != "AE" for f in r["focus"])


def test_untapped_candidates(db):
    seed(db, [("GB", LON, "HOT", "new", 50, 1)])
    u = market_report(db, NOW)["untapped"]
    assert 0 < len(u) <= 10 and all(x["tier"] <= 2 and x["country"] != "AE" and x["id"] != LON for x in u)
    assert [x["tier"] for x in u] == sorted(x["tier"] for x in u) and all(x["contact_window"].endswith(" UAE") for x in u)


def test_contact_window_values():
    winter = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)
    assert contact_window("Europe/London", winter) == "13:00–00:00 UAE"
    assert contact_window("Asia/Kolkata", winter) == "07:30–18:30 UAE"
    assert contact_window("Asia/Dubai", winter) == "09:00–20:00 UAE"
    assert contact_window("Nowhere/Land", winter) is None and contact_window(None, winter) is None


def test_report_contains_no_personal_data(db):
    seed(db, [("GB", LON, "HOT", "new", 50, 1, "Zed Person")])
    blob = json.dumps(market_report(db, NOW))
    for leaked in ("Zed Person", "+4479", "p0@x.example"):
        assert leaked not in blob
    for key in ("lead_id", "phone", "email", "display_name"):
        assert f'"{key}"' not in blob
    assert MEASURED_MIN == 10
