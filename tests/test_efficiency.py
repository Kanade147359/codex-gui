import pytest

from app.efficiency import ALLOWANCE_MULTIPLIER_FAST, PRICING, aggregate, turn_efficiency

SOL = "gpt-6.1-sol"


def turn(inp, cached, out, status="completed", model=SOL, task_id="t"):
    return {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out, "status": status,
            "model": model, "task_id": task_id}


def test_pricing_rows_carry_metadata():
    assert PRICING and all(r["effective_date"] and r["source"] and r["model"] and r["speed"] for r in PRICING)


def test_standard_sol_with_cache():
    e = turn_efficiency(turn(1_000_000, 800_000, 100_000), None, "default")
    c = e["credits"]
    assert c["actual"] == pytest.approx(200_000 * 50 / 1e6 + 800_000 * 2.5 / 1e6 + 100_000 * 250 / 1e6)  # 10+2+25
    assert c["no_cache"] == pytest.approx(50 + 25)
    assert c["saved_by_cache"] == pytest.approx(38)
    assert e["saved_percent"] == pytest.approx(38 / 75 * 100)
    assert e["usd"]["actual"] == pytest.approx(0.2 * 2 + 0.8 * 0.1 + 0.1 * 10)


def test_fast_sol_is_price_x2_not_allowance_x2_5():
    std = turn_efficiency(turn(1_000_000, 800_000, 100_000), None, "default")
    fast = turn_efficiency(turn(1_000_000, 800_000, 100_000), None, "priority")
    assert fast["credits"]["actual"] == pytest.approx(std["credits"]["actual"] * 2)
    assert fast["usd"]["no_cache"] == pytest.approx(std["usd"]["no_cache"] * 2)
    assert fast["credits"]["actual"] != pytest.approx(std["credits"]["actual"] * ALLOWANCE_MULTIPLIER_FAST)
    assert fast["saved_percent"] == pytest.approx(std["saved_percent"])
    # Astra stays Standard, so Fast Sol saves less vs Astra
    assert fast["credits"]["astra_same_token"] == std["credits"]["astra_same_token"]
    assert fast["credits"]["saved_vs_astra"] < std["credits"]["saved_vs_astra"]


def test_cache_zero_and_full():
    z = turn_efficiency(turn(1000, 0, 10), None, "default")
    assert z["credits"]["saved_by_cache"] == 0 and z["saved_percent"] == 0 and z["cache_hit_rate"] == 0
    f = turn_efficiency(turn(1_000_000, 1_000_000, 0), None, "default")
    assert f["credits"]["actual"] == pytest.approx(2.5) and f["credits"]["no_cache"] == pytest.approx(50)
    assert f["saved_percent"] == pytest.approx(95) and f["cache_hit_rate"] == 100


def test_astra_comparison_same_tokens_standard():
    e = turn_efficiency(turn(1_000_000, 800_000, 100_000), None, "default")
    assert e["credits"]["astra_same_token"] == pytest.approx(200_000 * 250 / 1e6 + 800_000 * 25 / 1e6 + 100_000 * 1250 / 1e6)
    assert e["credits"]["saved_vs_astra"] == pytest.approx(e["credits"]["astra_same_token"] - e["credits"]["actual"])
    assert e["usd"]["astra_same_token"] == pytest.approx(0.2 * 10 + 0.8 * 1 + 0.1 * 50)


def test_aggregate_sums_and_cache_hit():
    a = aggregate([(turn(1000, 500, 10, task_id="a"), SOL, "default", 0), (turn(3000, 3000, 0, task_id="b"), SOL, "priority", 0)])
    assert (a["input_tokens"], a["cached_input_tokens"], a["cache_hit_rate"]) == (4000, 3500, 87.5)
    one = turn_efficiency(turn(1000, 500, 10), None, "default")["credits"]["saved_by_cache"]
    two = turn_efficiency(turn(3000, 3000, 0), None, "priority")["credits"]["saved_by_cache"]
    assert a["total"]["credits"]["saved_by_cache"] == pytest.approx(one + two)
    assert a["fast_tasks"] == 1 and a["allowance_multiplier_fast"] == 2.5 and a["estimated"]["turns"] == 0


def test_zero_input():
    e = turn_efficiency(turn(0, 0, 0), None, "default")
    assert e["cache_hit_rate"] is None and e["saved_percent"] is None and e["credits"]["actual"] == 0
    assert aggregate([(turn(0, 0, 0), SOL, "default", 0)])["cache_hit_rate"] is None


def test_cached_above_input_is_clamped_and_flagged():
    e = turn_efficiency(turn(100, 500, 0), None, "default")
    assert e["anomaly"] and e["cached_input_tokens"] == 100 and e["cache_hit_rate"] == 100
    assert e["credits"]["actual"] >= 0 and e["credits"]["saved_by_cache"] >= 0
    assert any("clamped" in n for n in aggregate([(turn(100, 500, 0), SOL, "default", 0)])["notes"])


@pytest.mark.parametrize("model,tier", [("gpt-9-mystery", "default"), (None, "default"), (SOL, "flex"), (SOL, "ultra-fast")])
def test_unknown_model_or_tier_has_no_price(model, tier):
    e = turn_efficiency(turn(1000, 500, 10, model=None), model, tier)
    assert not e["available"] and e["reason"] == "pricing unavailable" and "credits" not in e
    a = aggregate([(turn(1000, 500, 10, model=None), model, tier, 0)])
    assert a["unpriced_turns"] == 1 and a["total"]["turns"] == 0 and a["total"]["credits"]["saved_by_cache"] == 0
    assert a["input_tokens"] == 1000  # tokens still counted
    assert any("pricing unavailable" in n for n in a["notes"])


def test_usage_exact_false_kept_separate():
    a = aggregate([(turn(1000, 500, 10), SOL, "default", 0), (turn(2000, 0, 5, status="interrupted"), SOL, "default", 0)])
    assert a["exact"]["turns"] == 1 and a["estimated"]["turns"] == 1 and a["total"]["turns"] == 2
    assert a["total"]["credits"]["saved_by_cache"] == pytest.approx(
        a["exact"]["credits"]["saved_by_cache"] + a["estimated"]["credits"]["saved_by_cache"])
    assert not turn_efficiency(turn(1, 0, 0, status="interrupted"), None, "default")["usage_exact"]
    assert any("estimate" in n for n in a["notes"])


def test_notes_state_limits_and_auto_review():
    a = aggregate([(turn(10, 0, 1), SOL, "default", 0)])
    text = " ".join(a["notes"])
    assert "not the ChatGPT subscription" in text and "272K" in text and "same-token estimate" in text
    assert "Auto-review" not in text
    assert "Auto-review usage may not be included." in aggregate([(turn(10, 0, 1), SOL, "default", 1)])["notes"]


def test_uncached_input_counts_and_rate():
    a = aggregate([(turn(1000, 750, 10), SOL, "default", 0), (turn(1000, 0, 0), SOL, "default", 0)])
    assert a["uncached_input_tokens"] == 1250 and a["uncached_input_rate"] == pytest.approx(62.5)
    assert aggregate([])["uncached_input_rate"] is None and aggregate([])["uncached_input_tokens"] == 0


def test_effective_input_multiplier_prices_cached_input_and_ignores_output():
    # 1M input, 800k cached: no-cache 50 credits, actual 200k*50 + 800k*2.5 = 12 credits -> 50/12, not 1/(1-0.8) = 5
    a = aggregate([(turn(1_000_000, 800_000, 5_000_000), SOL, "default", 0)])
    assert a["total"]["effective_input_multiplier"] == pytest.approx(50 / 12)
    assert a["total"]["effective_input_multiplier"] != pytest.approx(5)
    # full cache is capped by the cached rate (1/20 of the input rate), never infinite
    full = aggregate([(turn(1000, 1000, 0), SOL, "default", 0)])
    assert full["total"]["effective_input_multiplier"] == pytest.approx(20)
    # no cache -> 1.0x; Fast scales both sides equally
    assert aggregate([(turn(1000, 0, 0), SOL, "priority", 0)])["total"]["effective_input_multiplier"] == pytest.approx(1)


def test_multiplier_matches_unit_costs_and_stays_estimate_aware():
    a = aggregate([(turn(1000, 500, 10), SOL, "default", 0), (turn(2000, 0, 5, status="interrupted"), SOL, "default", 0)])
    usd = a["total"]["usd"]
    assert a["total"]["effective_input_multiplier"] == pytest.approx(usd["no_cache_input"] / usd["actual_input"])
    assert a["exact"]["effective_input_multiplier"] != pytest.approx(a["total"]["effective_input_multiplier"])
    assert aggregate([(turn(1000, 500, 10, model="other"), "other", "default", 0)])["total"]["effective_input_multiplier"] is None


def test_period_start():
    from datetime import datetime, timezone
    from app.efficiency import period_start
    now = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
    assert period_start("lifetime", now) is None
    assert period_start("7d", now) == "2026-10-01T12:00:00Z"
    today = datetime.fromisoformat(period_start("today", now).replace("Z", "+00:00")).astimezone()
    assert (today.hour, today.minute) == (0, 0) and 0 <= (now - today).total_seconds() <= 24 * 3600 + 3600
    with pytest.raises(ValueError):
        period_start("year", now)
