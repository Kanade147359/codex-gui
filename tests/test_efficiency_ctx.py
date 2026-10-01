"""Savings: cache write is its own price, long-context pricing is per request and only when the request size is known,
and the speed (Standard/Fast) is the one requested for each turn."""
import json

import pytest

from app import efficiency as eff
from app.efficiency import aggregate, turn_efficiency

SOL = "gpt-6.1-sol"


def turn(inp, cached, out, write=None, reqs=None, tier=None, status="completed"):
    t = {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out, "cache_write_input_tokens": write,
         "status": status, "model": SOL, "task_id": "t"}
    if reqs is not None:
        t["requests_json"] = json.dumps([{"i": i, "c": c, "w": w, "o": o} for i, c, w, o in reqs])
    if tier:
        t["service_tier"] = tier
    return t


def test_pricing_rows_have_the_official_sol_rates():
    usd = eff.rate(SOL, "default", "usd")
    assert (usd["input"], usd["cached_input"], usd["cache_write"], usd["output"]) == (2.00, 0.10, 2.50, 10.00)
    fast = eff.rate(SOL, "priority", "usd")
    assert (fast["input"], fast["cached_input"], fast["cache_write"], fast["output"]) == (4.00, 0.20, 5.00, 20.00)
    assert eff.rate(SOL, "default", "credits")["cache_write"] is None  # no credit rate for writes: never invented
    assert eff.long_context_threshold(SOL) == 272_000 and eff.long_context_threshold("gpt-6-astra") is None


def test_cache_write_is_separated_from_uncached_and_read():
    e = turn_efficiency(turn(1_000_000, 600_000, 0, write=200_000), None, "default")
    assert e["cache_write_tokens"] == 200_000 and e["uncached_regular_tokens"] == 200_000 and e["cached_input_tokens"] == 600_000
    assert e["usd"]["actual"] == pytest.approx(200_000 * 2.00 / 1e6 + 600_000 * 0.10 / 1e6 + 200_000 * 2.50 / 1e6)  # 0.4+0.06+0.5
    assert e["usd"]["cache_write_cost"] == pytest.approx(0.5)
    # a write costs MORE than plain input, so it can eat into the savings
    plain = turn_efficiency(turn(1_000_000, 600_000, 0, write=0), None, "default")
    assert e["usd"]["actual"] > plain["usd"]["actual"] and e["usd"]["saved_by_cache"] < plain["usd"]["saved_by_cache"]
    # credits have no write price: the written tokens count as ordinary input there
    assert e["credits"]["actual"] == pytest.approx(plain["credits"]["actual"])


def test_fast_cache_write_is_double():
    std = turn_efficiency(turn(1_000_000, 0, 0, write=1_000_000), None, "default")
    fast = turn_efficiency(turn(1_000_000, 0, 0, write=1_000_000), None, "priority")
    assert std["usd"]["actual"] == pytest.approx(2.50) and fast["usd"]["actual"] == pytest.approx(5.00)


def test_write_missing_means_zero_not_guessed():
    e = turn_efficiency(turn(1000, 500, 10, write=None), None, "default")
    assert e["cache_write_tokens"] == 0 and e["usd"]["cache_write_cost"] == 0


def test_cache_parts_above_input_are_clamped_and_flagged():
    e = turn_efficiency(turn(1000, 800, 0, write=500), None, "default")
    assert e["anomaly"] and e["cache_write_tokens"] == 200 and e["uncached_regular_tokens"] == 0


# ----- long context -----

def test_long_context_applies_per_request_when_the_size_is_known():
    reqs = [(300_000, 100_000, 0, 1000)]
    e = turn_efficiency(turn(300_000, 100_000, 1000, reqs=reqs), None, "default")
    assert e["per_request"] and e["long_context_requests"] == 1
    assert e["usd"]["actual"] == pytest.approx((200_000 * 2 * 2 + 100_000 * 0.1 * 2 + 1000 * 10 * 1.5) / 1e6)  # $4/$0.20/$15
    assert e["usd"]["no_cache"] == pytest.approx((300_000 * 4 + 1000 * 15) / 1e6)
    flat = turn_efficiency(turn(300_000, 100_000, 1000), None, "default")  # no per-request size: nothing is assumed
    assert not flat["per_request"] and flat["long_context_requests"] == 0
    assert flat["usd"]["actual"] == pytest.approx((200_000 * 2 + 100_000 * 0.1 + 1000 * 10) / 1e6)


def test_threshold_is_exceeded_not_reached():
    at = turn_efficiency(turn(272_000, 0, 0, reqs=[(272_000, 0, 0, 0)]), None, "default")
    over = turn_efficiency(turn(272_001, 0, 0, reqs=[(272_001, 0, 0, 0)]), None, "default")
    assert at["long_context_requests"] == 0 and over["long_context_requests"] == 1


def test_only_the_big_request_of_a_turn_gets_the_multiplier():
    reqs = [(100_000, 0, 0, 0), (280_000, 0, 0, 0), (120_000, 0, 0, 0)]
    e = turn_efficiency(turn(500_000, 0, 0, reqs=reqs), None, "default")
    assert e["long_context_requests"] == 1
    assert e["usd"]["actual"] == pytest.approx((100_000 * 2 + 280_000 * 4 + 120_000 * 2) / 1e6)


def test_accumulated_thread_usage_is_not_mistaken_for_a_request_size():
    """Five requests of 180K each = 900K tokens in the turn, but no single request is over 272K."""
    reqs = [(180_000, 150_000, 0, 500)] * 5
    e = turn_efficiency(turn(900_000, 750_000, 2500, reqs=reqs), None, "default")
    assert e["long_context_requests"] == 0
    assert e["usd"]["actual"] == pytest.approx(5 * (30_000 * 2 + 150_000 * 0.1 + 500 * 10) / 1e6)
    no_detail = turn_efficiency(turn(900_000, 750_000, 2500), None, "default")  # one 900K "request" would be wrong: not applied
    assert no_detail["long_context_requests"] == 0


def test_requests_that_do_not_add_up_are_ignored():
    e = turn_efficiency(turn(500_000, 0, 0, reqs=[(300_000, 0, 0, 0)]), None, "default")
    assert not e["per_request"] and e["long_context_requests"] == 0


def test_long_context_fast_is_x2_of_standard_long_context():
    reqs = [(300_000, 0, 0, 1000)]
    std = turn_efficiency(turn(300_000, 0, 1000, reqs=reqs), None, "default")
    fast = turn_efficiency(turn(300_000, 0, 1000, reqs=reqs), None, "priority")
    assert fast["usd"]["actual"] == pytest.approx(std["usd"]["actual"] * 2)
    assert fast["usd"]["actual"] == pytest.approx((300_000 * 8 + 1000 * 30) / 1e6)  # $8 in / $30 out


def test_aggregate_notes_follow_what_was_known():
    a = aggregate([(turn(300_000, 0, 1000, reqs=[(300_000, 0, 0, 1000)]), SOL, "default", 0)])
    assert a["long_context_requests"] == 1 and any("1 request(s) with more than 272K" in n for n in a["notes"])
    assert not any("not applied to them" in n for n in a["notes"])
    b = aggregate([(turn(100, 0, 1), SOL, "default", 0)])
    assert any("not applied to them" in n for n in b["notes"])
    assert a["cache_write_input_tokens"] == 0


# ----- per-turn service tier -----

def test_turn_tier_wins_over_the_task_tier():
    standard_turn = turn_efficiency(turn(1000, 0, 10, tier="default"), None, "priority")
    fast_turn = turn_efficiency(turn(1000, 0, 10, tier="priority"), None, "default")
    assert standard_turn["speed"] == "standard" and fast_turn["speed"] == "fast"
    assert fast_turn["usd"]["actual"] == pytest.approx(standard_turn["usd"]["actual"] * 2)
    unrecorded = turn_efficiency(turn(1000, 0, 10), None, "priority")  # old turn rows fall back to the task's tier
    assert unrecorded["speed"] == "fast"


def test_one_task_can_mix_standard_and_fast_turns():
    a = aggregate([(turn(1000, 0, 10, tier="default"), SOL, "default", 0), (turn(1000, 0, 10, tier="priority"), SOL, "default", 0)])
    assert a["fast_tasks"] == 1
    one = turn_efficiency(turn(1000, 0, 10), None, "default")["usd"]["actual"]
    assert a["total"]["usd"]["actual"] == pytest.approx(one * 3)
