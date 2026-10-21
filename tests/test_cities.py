import re
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from leadengine.api import create_app
from leadengine.config import Settings
from leadengine.db import DB
from leadengine.markets.cities import COUNTRIES, catalogue, find_cities, load_cities, resolve, search, stats


def test_catalogue_is_large_and_global():
    cs = catalogue()
    assert len(cs) >= 200
    assert len({c.country for c in cs}) >= 60
    assert {c.region for c in cs} >= {"South Asia", "GCC", "Europe", "Africa", "East Asia", "Southeast Asia", "North America",
                                      "Latin America & Caribbean", "Russia & Central Asia", "Oceania", "Middle East & Caucasus"}


def test_requested_indian_cities_and_aliases_resolve():
    assert resolve("Bangalore").name == "Bangalore" == resolve("Bengaluru").name
    assert resolve("Chennai").name == "Chennai" == resolve("madras").name
    assert resolve("Bombay").name == "Mumbai" and resolve("Calcutta").name == "Kolkata" and resolve("Trivandrum").name == "Thiruvananthapuram"
    assert len([c for c in catalogue() if c.country == "IN"]) >= 30


def test_typos_resolve_only_when_fuzzy():
    assert resolve("chennari") is None
    assert resolve("chennari", fuzzy=True).name == "Chennai"
    assert resolve("bangalor", fuzzy=True).name == "Bangalore"
    assert resolve("zzzzzz", fuzzy=True) is None


def test_every_row_is_valid():
    ids = set()
    for c in catalogue():
        assert c.id not in ids and re.fullmatch(r"[a-z0-9-]+-[a-z]{2}", c.id), c.id
        ids.add(c.id)
        ZoneInfo(c.timezone)
        assert c.tier in (1, 2, 3) and c.country in COUNTRIES and re.fullmatch(r"[A-Z]{3}", c.currency)
        assert c.languages and all(re.fullmatch(r"[a-z]{2}", lang) for lang in c.languages), c.id


def test_ids_are_ascii_slugs():
    assert resolve("São Paulo").id == "sao-paulo-br" and resolve("Düsseldorf").id == "dusseldorf-de" and resolve("Bangalore").id == "bangalore-in"


def test_alias_collisions_are_rejected(tmp_path):
    p = tmp_path / "c.psv"
    p.write_text("A|IN|South Asia|Asia/Kolkata|en|1|Shared\nB|IN|South Asia|Asia/Kolkata|en|1|Shared\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Shared"):
        load_cities(p)
    p.write_text("A|XX|South Asia|Asia/Kolkata|en|1|\n", encoding="utf-8")
    with pytest.raises(ValueError, match="country"):
        load_cities(p)
    p.write_text("A|IN|South Asia|Not/AZone|en|1|\n", encoding="utf-8")
    with pytest.raises(ValueError, match="timezone"):
        load_cities(p)


def test_find_cities_in_text_in_order_with_aliases_and_longest_match():
    got = [c.id for c in find_cities("Flats in Bengaluru and New Delhi, maybe Madras next year")]
    assert got == ["bangalore-in", "delhi-in", "chennai-in"]
    assert [c.id for c in find_cities("moving from New York City to Dubai")] == ["new-york-us", "dubai-ae"]


def test_find_cities_handles_arabic_cyrillic_and_accents():
    assert [c.id for c in find_cities("اريد شقة وانا في الرياض وجدة")] == ["riyadh-sa", "jeddah-sa"]
    assert [c.id for c in find_cities("Я живу в Москве")] == ["moscow-ru"]          # inflected form still contains the stem
    assert [c.id for c in find_cities("from sao paulo or Bogota")] == ["sao-paulo-br", "bogota-co"]


def test_no_false_positives_on_short_aliases_or_inside_words():
    assert find_cities("la la land, hk and nyc?") == [resolve("NYC")] and find_cities("a performance in perthshire") == []


def test_search_and_stats():
    assert [c.name for c in search(q="bengal")] == ["Bangalore"]
    assert [c.name for c in search(q="chennari")] == ["Chennai"]                     # falls back to fuzzy when nothing matches
    assert {c.country for c in search(region="GCC")} == {"SA", "KW", "QA", "BH", "OM"}
    assert all(c.tier == 1 for c in search(tier=1)) and all("ru" in c.languages for c in search(lang="ru"))
    assert len(search(limit=5)) == 5 and search(region="Nowhere") == []
    s = stats()
    assert s["cities"] == len(catalogue()) and s["by_region"]["South Asia"] >= 40 and s["countries"] >= 60


@pytest.fixture()
def client():
    return TestClient(create_app(Settings(db_path=":memory:", api_key="k"), DB(":memory:")))


def test_api(client):
    H = {"x-api-key": "k"}
    assert client.get("/markets/cities").status_code == 401
    rows = client.get("/markets/cities?limit=500", headers=H).json()
    assert len(rows) >= 200 and {"id", "name", "country", "currency", "region", "timezone", "languages", "tier", "aliases"} <= set(rows[0])
    assert [r["name"] for r in client.get("/markets/cities?q=bengaluru", headers=H).json()] == ["Bangalore"]
    assert client.get("/markets/cities?country=IN&limit=500", headers=H).json()[0]["currency"] == "INR"
    assert client.get("/markets/cities?limit=9999", headers=H).status_code == 422
    assert client.get("/markets/summary", headers=H).json()["cities"] >= 200
