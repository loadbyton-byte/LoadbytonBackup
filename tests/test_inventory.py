from datetime import datetime

from leadengine.inventory import import_projects_csv, match

CSV = """name,developer,area,emirate,min_price,max_price,bedrooms,handover,payment_plan
Marina Edge,Acme Dev,Dubai Marina,Dubai,"AED 1,200,000",2.4M,"1-3",2030,60/40
South Gardens,Beta Dev,Dubai South,Dubai,900000,1.4M,"studio,1,2",2029,70/30
Hills Crest,Gamma,Dubai Hills Estate,Dubai,2.5M,6M,"3,4",2031,50/50
No Price,Delta,JVC,Dubai,,,2,2030,
Bad Price,Delta,JVC,Dubai,lots,lots,2,2030,
,Nameless,JVC,Dubai,1M,2M,2,2030,
Inverted,Delta,Business Bay,Dubai,3M,1M,2,2030,
"""


def seeded(pipe):
    res = import_projects_csv(pipe.db, CSV)
    return res


def lead(**kw):
    base = {"primary_area": "Dubai South", "budget_max": 1_000_000, "bedrooms": 1, "purchase_type": "off_plan",
            "label": "buyer", "profile": {"areas": ["Dubai South"]}}
    base.update(kw)
    return base


def test_import_reports_bad_rows(pipe):
    res = seeded(pipe)
    assert res["imported"] == 3
    reasons = dict(res["skipped"])
    assert set(reasons) == {4, 5, 6, 7}                       # 1-based data rows: blank price, 'lots', no name, min>max
    assert "price" in reasons[4] and "price" in reasons[5] and "name" in reasons[6] and "min_price" in reasons[7]


def test_price_formats_parsed(pipe):
    seeded(pipe)
    row = pipe.db.one("SELECT min_price,max_price FROM projects WHERE name='Marina Edge'")
    assert (row["min_price"], row["max_price"]) == (1_200_000, 2_400_000)


def test_match_ranks_area_then_budget(pipe):
    seeded(pipe)
    m = match(pipe.db, lead(), golden_min=2_000_000, now=datetime(2026, 10, 19))
    assert m[0]["project"] == "South Gardens" and m[0]["score"] == 50 + 30 + 10 + 10
    assert any("area" in r for r in m[0]["reasons"])
    assert [x["project"] for x in m] == ["South Gardens"]      # area is a hard filter: Marina Edge shares only bedrooms


def test_partial_budget_fit(pipe):
    seeded(pipe)
    m = match(pipe.db, lead(budget_max=1_550_000, bedrooms=None), golden_min=2_000_000, now=datetime(2026, 10, 19))
    assert m[0]["project"] == "South Gardens" and m[0]["score"] == 50 + 15 + 10      # within +15% of max 1.4M; no beds


def test_golden_visa_flag_uses_config(pipe):
    seeded(pipe)
    hills = lead(primary_area="Dubai Hills Estate", profile={"areas": ["Dubai Hills Estate"]}, bedrooms=3)
    assert match(pipe.db, {**hills, "budget_max": 1_900_000}, golden_min=2_000_000)[0]["golden_visa_fit"] is False
    assert match(pipe.db, {**hills, "budget_max": 2_600_000}, golden_min=2_000_000)[0]["golden_visa_fit"] is True
    assert match(pipe.db, {**hills, "budget_max": 2_600_000}, golden_min=3_000_000)[0]["golden_visa_fit"] is False


def test_no_match_and_rent_leads_return_empty(pipe):
    seeded(pipe)
    assert match(pipe.db, lead(primary_area="Sharjah", profile={"areas": ["Sharjah"]}, budget_max=None), golden_min=2e6) == []
    assert match(pipe.db, lead(label="renter", purchase_type="rent"), golden_min=2e6) == []


def test_api_import_and_matches():
    from fastapi.testclient import TestClient

    from leadengine.api import create_app
    from leadengine.config import Settings
    from leadengine.db import DB

    c = TestClient(create_app(Settings(db_path=":memory:", api_key=""), DB(":memory:")))
    assert c.post("/inventory", content=CSV).json()["imported"] == 3
    c.post("/demo/seed")
    lid = next(l["id"] for l in c.get("/leads").json() if l["primary_area"] == "Dubai South")
    assert c.get(f"/leads/{lid}/matches").json()[0]["project"] == "South Gardens"
    assert c.get("/leads/9999/matches").status_code == 404


def test_api_lists_projects():
    from fastapi.testclient import TestClient

    from leadengine.api import create_app
    from leadengine.config import Settings
    from leadengine.db import DB

    c = TestClient(create_app(Settings(db_path=":memory:", api_key=""), DB(":memory:")))
    assert c.get("/inventory").json() == []
    c.post("/inventory", content=CSV)
    rows = c.get("/inventory").json()
    assert [r["name"] for r in rows] == ["Marina Edge", "South Gardens", "Hills Crest"] and rows[0]["bedrooms"] == [1, 2, 3]
