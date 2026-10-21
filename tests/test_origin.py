import pytest

from leadengine.markets.cities import COUNTRIES, resolve
from leadengine.markets.origin import CALLING_CODES, SHARED_PLUS_ONE, Origin, country_for_phone, resolve_origin

LONDON = resolve("London").id


def test_precedence_form_over_phone_over_text():
    karachi = resolve("Karachi").id
    assert resolve_origin("I'm in Mumbai", "+447911123456", country="PK", city="Karachi") == Origin("PK", karachi, "form")
    assert resolve_origin("I'm in Mumbai", "+447911123456") == Origin("GB", None, "phone")
    o = resolve_origin("I'm in Mumbai", None)
    assert o and o.country == "IN" and o.basis == "text"


def test_form_city_fixes_country_and_unresolvable_is_ignored():
    assert resolve_origin(None, None, country="PK", city="London").country == "GB"
    assert resolve_origin(None, None, country="Narnia", city="Nowhereville") is None
    assert resolve_origin(None, None, country="india") == Origin("IN", None, "form")
    assert resolve_origin(None, None, country="IN") == Origin("IN", None, "form")


@pytest.mark.parametrize("phone,expected", [
    ("+971501234567", "AE"), ("+447911123456", "GB"), ("+919876543210", "IN"), ("+923001234567", "PK"), ("+77011234567", "KZ"),
    ("+74951234567", "RU"), ("+12125550100", None), ("+999123", None), ("abc", None), ("", None), (None, None), ("0501234567", "AE")])
def test_phone_prefixes(phone, expected):
    assert country_for_phone(phone) == expected


def test_calling_codes_cover_catalogue():
    missing = set(COUNTRIES) - set(CALLING_CODES) - SHARED_PLUS_ONE - {"RU", "KZ"}
    assert not missing, missing
    assert len(set(CALLING_CODES.values())) == len(CALLING_CODES)             # no two countries share a code here


@pytest.mark.parametrize("text", ["i am in London", "I'm in London looking for a villa", "we're in London", "based in London", "living in London",
                                  "currently in London", "moving from London", "relocating from London", "originally from London",
                                  "I'm from London", "writing from London", "Hello! I'm in London, budget AED 2M"])
def test_self_location_phrases_en(text):
    assert resolve_origin(text, None) == Origin("GB", LONDON, "text")


@pytest.mark.parametrize("text", ["أنا في لندن", "انا في لندن", "مقيم في لندن", "ساكن في لندن", "قادم من لندن", "جاي من لندن", "أعيش في لندن", "اعيش في لندن", "أسكن في لندن"])
def test_self_location_phrases_ar(text):
    assert resolve_origin(text, None) == Origin("GB", LONDON, "text")


@pytest.mark.parametrize("text", ["interested in London property", "buy a flat in London", "looking for a 2BR in Dubai Marina", "invest in London",
                                  "properties from AED 1M", "London", "Dubai", "flights from London are cheap, want a Dubai villa",
                                  "عقار في لندن", "أبغى شقة في دبي مارينا"])
def test_property_location_mentions_are_never_origin(text):
    assert resolve_origin(text, None) is None


def test_uae_city_after_phrase_is_domestic():
    o = resolve_origin("I'm in Sharjah", None)
    assert o.country == "AE" and o.city == "sharjah-ae"


def test_never_raises():
    import os
    junk = os.urandom(200_000).decode("latin-1") + "\x00\x1f" * 100
    resolve_origin(junk, junk)
    assert resolve_origin(None, None) is None
    for bad in (123, [], {}, b"x", object()):
        resolve_origin(bad, bad, bad, bad)
