from datetime import datetime, timedelta, timezone

import pytest

from app import cache_health as ch
from app.cache_health import Thresholds, TurnFacts

TH = Thresholds()
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def iso(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")


def facts(**kw):
    base = dict(model="gpt-6.1-sol", service_tier="default", reasoning_effort="low", verbosity="low", tool_profile="full:",
                thread_id="th1", started_at=iso(5), finished_at=iso(4))
    base.update(kw)
    return TurnFacts(**base)


def test_split_usage_separates_read_write_uncached():
    s = ch.split_usage(100_000, 60_000, 25_000)
    assert (s["cache_read"], s["cache_write"], s["uncached"], s["uncached_regular"]) == (60_000, 25_000, 40_000, 15_000)
    assert s["hit_rate"] == 60.0 and not s["anomaly"]


def test_split_usage_clamps_impossible_numbers():
    s = ch.split_usage(100, 90, 50)
    assert s["anomaly"] and s["cache_read"] == 90 and s["cache_write"] == 10 and s["uncached_regular"] == 0
    assert ch.split_usage(0, 0, None)["hit_rate"] is None


# ----- cache-miss detection -----

def test_big_uncached_input_is_a_miss():
    s = ch.split_usage(60_000, 20_000, 0)  # 40,000 uncached
    ev = ch.detect_cache_miss(s, [90, 92], TH, prev=facts(), cur=facts(started_at=iso(3)))
    assert ev and ev["kind"] == "cache_miss" and ev["severity"] == "warning" and ev["uncached_tokens"] == 40_000
    assert any("40,000 uncached" in r for r in [ev["message"]])


def test_threshold_boundary_is_strictly_above_and_configurable():
    s = ch.split_usage(60_000, 50_000, 0)  # exactly 10,000 uncached: not above the limit
    assert ch.detect_cache_miss(s, [], TH, prev=facts(), cur=facts()) is None
    s = ch.split_usage(60_001, 50_000, 0)
    assert ch.detect_cache_miss(s, [], TH, prev=facts(), cur=facts()) is not None
    loose = ch.thresholds_from({"cache_miss_uncached_tokens": 20_000})
    assert ch.detect_cache_miss(s, [], loose, prev=facts(), cur=facts()) is None


def test_hit_rate_drop_vs_recent_average_is_a_miss_even_when_uncached_is_modest():
    s = ch.split_usage(8_000, 2_000, 0)  # 25% hit, 6,000 uncached
    ev = ch.detect_cache_miss(s, [95, 96, 94], TH, prev=facts(), cur=facts())
    assert ev and "recent average" in ev["message"]
    assert ch.detect_cache_miss(s, [30, 28], TH, prev=facts(), cur=facts()) is None  # it was always low: no *drop*
    tiny = ch.split_usage(1_000, 0, 0)
    assert ch.detect_cache_miss(tiny, [99], TH, prev=facts(), cur=facts()) is None   # too small to judge


def test_compact_turns_are_never_judged():
    s = ch.split_usage(90_000, 0, 0)
    assert ch.detect_cache_miss(s, [90], TH, prev=facts(), cur=facts(kind="compact")) is None


def test_first_turn_of_a_thread_is_info_with_its_own_cause():
    s = ch.split_usage(50_000, 0, 50_000)
    ev = ch.detect_cache_miss(s, [], TH, prev=None, cur=facts(), first_of_thread=True)
    assert ev["severity"] == "info" and "first turn" in ev["possible_causes"][0]


@pytest.mark.parametrize("change,needle", [
    (dict(model="gpt-6-sol"), "model change"),
    (dict(service_tier="priority"), "service tier change"),
    (dict(reasoning_effort="high"), "reasoning effort change"),
    (dict(verbosity="high"), "verbosity change"),
    (dict(tool_profile="minimal:abc"), "tool profile"),
    (dict(thread_id="th2"), "new Codex session"),
    (dict(started_at=iso(-60)), "idle for"),   # prev finished 4 min ago, this one starts an hour later
])
def test_possible_causes_name_what_changed(change, needle):
    s = ch.split_usage(60_000, 0, 0)
    ev = ch.detect_cache_miss(s, [90], TH, prev=facts(), cur=facts(**change))
    assert ev and any(needle in c for c in ev["possible_causes"]), ev["possible_causes"]
    assert not ev["cause_unknown"]


def test_compaction_is_a_possible_cause():
    s = ch.split_usage(60_000, 0, 0)
    ev = ch.detect_cache_miss(s, [90], TH, prev=facts(kind="compact"), cur=facts())
    assert any("compaction" in c for c in ev["possible_causes"])
    ev = ch.detect_cache_miss(s, [90], TH, prev=facts(), cur=facts(compactions=1))
    assert any("compaction" in c for c in ev["possible_causes"])


def test_idle_cause_uses_the_configurable_gap():
    s = ch.split_usage(60_000, 0, 0)
    cur = facts(started_at=iso(-10))  # 14 minutes after prev finished
    assert not any("idle" in c for c in ch.detect_cache_miss(s, [90], TH, prev=facts(), cur=cur)["possible_causes"])
    short = ch.thresholds_from({"idle_cause_minutes": 10})
    assert any("idle" in c for c in ch.detect_cache_miss(s, [90], short, prev=facts(), cur=cur)["possible_causes"])


def test_no_known_change_is_stated_without_asserting_a_cause():
    s = ch.split_usage(60_000, 0, 0)
    ev = ch.detect_cache_miss(s, [90], TH, prev=facts(), cur=facts())
    assert ev["cause_unknown"] and "may have changed inside Codex" in ev["possible_causes"][0]
    text = " ".join(ev["possible_causes"] + [ev["message"]]).lower()
    assert "caused by" not in text and "because" not in text


# ----- thresholds -----

def test_thresholds_validation():
    assert ch.validate_thresholds({"cache_miss_uncached_tokens": 12_000}) == {"cache_miss_uncached_tokens": 12_000}
    for bad in ({"nope": 1}, {"cache_miss_uncached_tokens": 5}, {"cache_miss_uncached_tokens": "x"}, {"cache_miss_drop_points": True},
                {"tool_output_warn_tokens": 20_000, "tool_output_large_tokens": 10_000}):
        with pytest.raises(ValueError):
            ch.validate_thresholds(bad)
    assert ch.thresholds_from({"cache_miss_uncached_tokens": 1, "zzz": 5}).cache_miss_uncached_tokens == 10_000


# ----- cache age -----

@pytest.mark.parametrize("minutes,state", [(0, "hot"), (9.9, "hot"), (10, "warm"), (29.9, "warm"), (30, "cold"), (300, "cold")])
def test_cache_age_states(minutes, state):
    a = ch.cache_age(iso(minutes), NOW)
    assert a["state"] == state and a["label"].startswith(state.upper())


def test_cache_age_unknown_without_activity_and_thresholds_configurable():
    assert ch.cache_age(None, NOW)["state"] == "unknown"
    t = ch.thresholds_from({"cache_hot_minutes": 2, "cache_warm_minutes": 5})
    assert ch.cache_age(iso(3), NOW, t)["state"] == "warm" and ch.cache_age(iso(6), NOW, t)["state"] == "cold"


# ----- compaction monitor -----

def test_compaction_count_and_frequency():
    assert ch.compaction_status([], TH) == {"count": 0, "frequent": False, "warning": None, "window_minutes": 60}
    one = ch.compaction_status([iso(5)], TH)
    assert one["count"] == 1 and not one["frequent"]
    two = ch.compaction_status([iso(50), iso(10)], TH)
    assert two["frequent"] and two["warning"] == "Frequent compaction can reduce cache reuse and cause files to be re-read"
    spread = ch.compaction_status([iso(500), iso(250), iso(5)], TH)
    assert spread["count"] == 3 and not spread["frequent"]
    ninety = [iso(500), iso(410)]  # 90 minutes apart
    assert ch.compaction_status(ninety, ch.thresholds_from({"frequent_compaction_minutes": 60}))["frequent"] is False
    assert ch.compaction_status(ninety, ch.thresholds_from({"frequent_compaction_minutes": 120}))["frequent"] is True
    assert ch.compaction_status(ninety, ch.thresholds_from({"frequent_compaction_count": 3}))["frequent"] is False
