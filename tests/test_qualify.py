import re

import pytest

from leadengine.qualify import advance, is_stop

AR = re.compile("[؀-ۿ]")


def run(messages, state=None):
    state, replies, done = state or {}, [], False
    for m in messages:
        state, reply, done = advance(state, m)
        replies.append(reply)
    return state, replies, done


def test_flow_collects_area_budget_timeline_then_asks_consent():
    state, r, done = run(["Hi, I'm looking to buy a flat", "Dubai Marina", "around 1.5M", "next month"])
    assert "area" in r[0].lower() and "budget" in r[1].lower() and "when" in r[2].lower() and "YES" in r[3] and not done
    state, reply, done = advance(state, "yes")
    a = state["answers"]
    assert done and a == {"intent": "buy", "area": "Dubai Marina", "budget": 1_500_000, "timeline": 30, "consent": True}
    assert "STOP" in reply


def test_arabic_flow_replies_in_arabic():
    state, r, done = run(["ابغى اشتري شقه", "دبي مارينا", "ميزانيتي 2 مليون", "الشهر الجاي"])
    assert all(AR.search(x) for x in r)
    state, reply, done = advance(state, "نعم")
    assert done and state["answers"]["consent"] is True and state["answers"]["budget"] == 2_000_000 and AR.search(reply)


def test_unparseable_answer_is_reasked_once_then_skipped():
    state, r, _ = run(["I want to rent an apartment", "Dubai Marina", "banana"])
    assert "didn't catch" in r[2].lower() and "budget" in r[2].lower()
    state, r2, _ = run(["blah"], state)
    assert state["answers"].get("budget") is None and "when" in r2[0].lower()          # moved on to timeline


def test_consent_no_ends_without_consent():
    state, r, done = run(["I want to buy", "JVC", "1M", "asap", "no"])
    assert done and state["answers"]["consent"] is False and "STOP" not in r[-1]


@pytest.mark.parametrize("text", ["STOP", "Stop.", "  stop!! ", "ايقاف", "إيقاف", "وقف", "Unsubscribe", "stop all"])
def test_stop_variants(text):
    assert is_stop(text)


@pytest.mark.parametrize("text", ["Can I stop by tomorrow?", "stop by at 5", "non-stop flights", "", "yes"])
def test_non_stop_messages(text):
    assert not is_stop(text)
