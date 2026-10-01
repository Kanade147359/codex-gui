import pytest

from app.usage import cache_hit_rate, extract_usage, turn_delta

FULL = {"input_tokens": 14240, "cached_input_tokens": 11776, "cache_write_input_tokens": 0,
        "output_tokens": 5, "reasoning_output_tokens": 0}


def test_extract_usage_from_turn_completed():
    assert extract_usage({"type": "turn.completed", "usage": FULL}) == FULL


def test_extract_usage_ignores_other_events_and_junk():
    assert extract_usage({"type": "turn.started"}) is None
    assert extract_usage({"type": "turn.completed"}) is None
    assert extract_usage({"type": "turn.completed", "usage": "x"}) is None
    assert extract_usage({"type": "turn.completed", "usage": {}}) is None
    assert extract_usage(None) is None
    # unknown keys and non-integers are dropped, not trusted
    u = extract_usage({"type": "turn.completed", "usage": {"input_tokens": 5, "cached_input_tokens": "7",
                                                          "total_tokens": 99, "output_tokens": True}})
    assert u == {"input_tokens": 5}


def test_optional_keys_may_be_absent():
    u = extract_usage({"type": "turn.completed", "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 2}})
    assert "cache_write_input_tokens" not in u and "reasoning_output_tokens" not in u


def test_turn_delta_first_turn_is_the_total():
    assert turn_delta(FULL, None) == (FULL, False)


def test_turn_delta_subtracts_the_previous_total_of_the_thread():
    # figures taken from a real codex-cli 0.159.2 session: totals of turn 2 and turn 3
    t2 = {"input_tokens": 42909, "cached_input_tokens": 37632, "output_tokens": 118, "reasoning_output_tokens": 9}
    t3 = {"input_tokens": 57384, "cached_input_tokens": 51840, "output_tokens": 123, "reasoning_output_tokens": 9}
    delta, not_cumulative = turn_delta(t3, t2)
    assert delta == {"input_tokens": 14475, "cached_input_tokens": 14208, "output_tokens": 5, "reasoning_output_tokens": 0}
    assert not not_cumulative


def test_turn_delta_falls_back_to_raw_when_totals_go_down():
    small = {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 1}
    assert turn_delta(small, FULL) == (small, True)


def test_cache_hit_rate():
    assert cache_hit_rate(42531, 39872) == pytest.approx(93.748, abs=0.001)
    assert cache_hit_rate(100, 0) == 0
    assert cache_hit_rate(0, 0) is None  # nothing to display
    assert cache_hit_rate(None, 5) is None


# ---------- app-server token usage ----------

from app.usage import (  # noqa: E402
    context_status, is_quota_error, parse_rate_limits, parse_token_usage, quota_exhausted, window_kind, window_label,
)

# shape of a real thread/tokenUsage/updated of codex-cli 0.159.2 (gpt-6.1-sol)
TOKEN_USAGE = {
    "total": {"totalTokens": 65838, "inputTokens": 65688, "cachedInputTokens": 58368, "cacheWriteInputTokens": 0,
              "outputTokens": 150, "reasoningOutputTokens": 7},
    "last": {"totalTokens": 13287, "inputTokens": 13265, "cachedInputTokens": 12928, "cacheWriteInputTokens": 0,
             "outputTokens": 22, "reasoningOutputTokens": 0},
    "modelContextWindow": 258400,
}


def test_parse_token_usage_maps_names_and_reads_context():
    p = parse_token_usage(TOKEN_USAGE)
    assert p["total"] == {"input_tokens": 65688, "cached_input_tokens": 58368, "output_tokens": 150,
                          "cache_write_input_tokens": 0, "reasoning_output_tokens": 7}
    assert p["context_tokens"] == 13287 and p["context_window"] == 258400  # the window is Codex's, not ours


def test_parse_token_usage_survives_missing_and_junk_fields():
    assert parse_token_usage(None) is None and parse_token_usage({}) is None and parse_token_usage({"total": {}}) is None
    p = parse_token_usage({"total": {"inputTokens": 5, "cachedInputTokens": 2, "outputTokens": "x", "unknown": 1}})
    assert p["total"] == {"input_tokens": 5, "cached_input_tokens": 2}
    assert p["context_tokens"] is None and p["context_window"] is None


def test_cache_hit_rate_from_the_documented_formula():
    # 173,104 / 184,320 = 93.9 %
    assert round(cache_hit_rate(184320, 173104), 1) == 93.9
    assert 184320 - 173104 == 11216
    assert cache_hit_rate(0, 0) is None and cache_hit_rate(None, 5) is None


def test_context_status_and_guard():
    assert context_status(181000, 272000)["percent"] == pytest.approx(66.54, abs=0.01)
    assert context_status(181000, 272000)["warn"] is False
    s = context_status(221000, 272000, warn_percent=80)  # 81.25 %
    assert s["warn"] is True and round(s["percent"]) == 81
    assert context_status(221000, 272000, guard=False)["warn"] is False
    assert context_status(221000, 272000, warn_percent=90)["warn"] is False
    unknown = context_status(None, 272000)
    assert unknown["percent"] is None and unknown["warn"] is False
    assert context_status(5000, None)["percent"] is None  # no window known: never guess one


# ---------- rate limits ----------

# the real account/rateLimits/read of this machine's plan: ONE weekly window, no 5 hour window
WEEKLY_ONLY = {
    "ordinaryUsageAllowed": True,
    "rateLimits": {"limitId": "codex", "limitName": None, "planType": "prolite", "rateLimitReachedType": None,
                   "primary": {"usedPercent": 7, "windowDurationMins": 10080, "resetsAt": 1791453983},
                   "secondary": None, "credits": {"hasCredits": False, "unlimited": False, "balance": "0"}},
    "rateLimitResetCredits": {"availableCount": 0, "credits": []},
}
TWO_WINDOWS = {
    "ordinaryUsageAllowed": True,
    "rateLimits": {"limitId": "codex", "planType": "pro", "rateLimitReachedType": None,
                   "primary": {"usedPercent": 63, "windowDurationMins": 300, "resetsAt": 1791400000},
                   "secondary": {"usedPercent": 38, "windowDurationMins": 10080, "resetsAt": 1791900000}},
    "rateLimitResetCredits": {"availableCount": 2, "credits": []},
}


def test_parse_rate_limits_weekly_only_plan():
    p = parse_rate_limits(WEEKLY_ONLY)
    assert p["five_hour_used"] is None and p["weekly_used"] == 7  # classified by duration, not by slot
    assert p["windows"] == [{"kind": "weekly", "label": "Weekly", "used_percent": 7, "duration_mins": 10080,
                             "resets_at": 1791453983}]
    assert p["plan_type"] == "prolite" and p["available_resets"] == 0 and p["ordinary_usage_allowed"] is True


def test_parse_rate_limits_two_windows():
    p = parse_rate_limits(TWO_WINDOWS)
    assert (p["five_hour_used"], p["weekly_used"]) == (63, 38)
    assert [(w["label"], w["used_percent"], w["resets_at"]) for w in p["windows"]] == [
        ("5 hour", 63, 1791400000), ("Weekly", 38, 1791900000)]
    assert p["available_resets"] == 2


def test_window_kinds_follow_the_duration():
    assert (window_kind(300), window_kind(10080), window_kind(1440), window_kind(None)) == \
           ("five_hour", "weekly", "other", "other")
    assert window_label(300) == "5 hour" and window_label(10080) == "Weekly" and window_label(1440) == "24 hour"


def test_parse_rate_limits_of_an_update_notification_and_junk():
    p = parse_rate_limits({"rateLimits": TWO_WINDOWS["rateLimits"]})  # push updates have no allowed flag / credits
    assert p["ordinary_usage_allowed"] is None and p["available_resets"] is None and p["weekly_used"] == 38
    assert parse_rate_limits(None) is None and parse_rate_limits({}) is None
    assert parse_rate_limits({"rateLimits": {"primary": {"usedPercent": "x"}}})["windows"] == []


def test_quota_exhaustion_is_only_what_codex_says():
    assert quota_exhausted(parse_rate_limits({"ordinaryUsageAllowed": False, "rateLimits": {}}))
    assert quota_exhausted(parse_rate_limits({"rateLimits": {"rateLimitReachedType": "rate_limit_reached"}}))
    assert not quota_exhausted(parse_rate_limits(WEEKLY_ONLY))
    # unknown (null) is not "exhausted", and 100 % alone is not either: Codex decides
    assert not quota_exhausted(parse_rate_limits({"ordinaryUsageAllowed": None, "rateLimits": {
        "primary": {"usedPercent": 100, "windowDurationMins": 300}}}))
    assert not quota_exhausted(None)


def test_quota_error_kinds():
    assert is_quota_error("usageLimitExceeded") and is_quota_error("rateLimitExceeded")
    assert not is_quota_error("contextWindowExceeded") and not is_quota_error("other") and not is_quota_error(None)
