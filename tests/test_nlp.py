from leadengine.nlp.areas import extract_areas
from leadengine.nlp.intent import classify, parse_budget, parse_timeline
from leadengine.nlp.text import normalize


def test_english_ready_buyer_full_entities():
    it = classify("Looking to buy a 2BR in Dubai Marina, budget AED 1.8M, cash buyer, want to view this week — can anyone recommend a good agent?")
    assert (it.label, it.stage) == ("buyer", "ready")
    assert it.areas == ["Dubai Marina"] and it.bedrooms == 2 and it.budget_max == 1_800_000
    assert it.financing == "cash" and it.timeline_days == 7 and it.wants_agent


def test_arabic_buyer_gulf_dialect():
    it = classify("ابغى اشتري شقه غرفتين وصاله في داون تاون دبي ميزانيتي 2 مليون ومستعجل")
    assert it.label == "buyer" and it.language == "ar"
    assert it.areas == ["Downtown Dubai"] and it.budget_max == 2_000_000 and it.bedrooms == 2 and it.timeline_days == 3


def test_arabic_diacritics_and_indic_digits():
    assert normalize("أريد شراء فيلّا") == "اريد شراء فيلا"
    it = classify("أريد شراء فيلّا في الشارقة، ميزانيتي ٣ مليون")
    assert it.label == "buyer" and it.budget_max == 3_000_000 and "Sharjah" in it.areas


def test_broker_spam_is_not_a_lead():
    assert classify("Exclusive launch! Limited units, book now. Call +971501234567 BRN 12345").label == "agent_spam"
    assert classify("For sale: 2BR Marina, DM for details. Our team at Acme Properties").label == "agent_spam"


def test_noise():
    assert classify("What a beautiful sunset over Dubai Marina tonight").label == "noise"
    assert classify("ok").label == "noise"


def test_renter_with_annual_budget_and_timeline():
    it = classify("Moving to Abu Dhabi next month, need 1 bedroom in Al Reem, around 70k per year")
    assert it.label == "renter" and it.purchase_type == "rent"
    assert it.areas == ["Al Reem Island"] and it.budget_max == 70_000 and it.timeline_days == 30


def test_investor_golden_visa_offplan():
    it = classify("Is it worth investing in Dubai off-plan for rental yield? golden visa is a bonus")
    assert it.label == "investor" and it.golden_visa and it.purchase_type == "off_plan"


def test_seller():
    it = classify("I want to sell my 2 bed apartment in Business Bay, how much is it worth?")
    assert it.label == "seller" and it.areas == ["Business Bay"]


def test_seeker_resolved_by_budget():
    assert classify("Comparing Dubai Hills vs Arabian Ranches, budget 3-4M for a family villa").label == "buyer"
    assert classify("Searching for a studio in JLT, budget 55k").label == "renter"
    assert classify("Still searching for a 2 bedroom in Dubai Marina").label == "seeker"


def test_budget_parsing_variants():
    assert parse_budget("aed 1,200,000")[1] == 1_200_000
    assert parse_budget("around 800k")[1] == 800_000
    assert parse_budget("1.5 million")[1] == 1_500_000
    assert parse_budget("3-4m") == (3_000_000, 4_000_000)
    assert parse_budget("built in 2026, 2br") == (None, None)       # years / bedroom counts are not budgets


def test_timeline_parsing():
    assert parse_timeline("within 3 months") == 90
    assert parse_timeline("خلال 2 شهر") == 60
    assert parse_timeline("whenever") is None


def test_area_gazetteer_aliases_and_boundaries():
    assert extract_areas("moving to JVC")[0] == ["Jumeirah Village Circle"]
    assert extract_areas("something about palmistry")[0] == []         # 'palm' must not match inside a word
    assert extract_areas("شقة في نخلة جميرا")[0] == ["Palm Jumeirah"]


def test_longest_area_match_wins():
    assert extract_areas("Palm Jumeirah villa")[0] == ["Palm Jumeirah"]
    assert extract_areas("JVC or Jumeirah Village Circle")[0] == ["Jumeirah Village Circle"]
    assert extract_areas("Jumeirah vs Palm Jumeirah")[0] == ["Jumeirah", "Palm Jumeirah"]
