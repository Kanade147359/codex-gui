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
