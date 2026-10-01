import pytest

from app import ctx_guard as cg
from app.cache_health import Thresholds

TH = Thresholds()
SOL = "gpt-6.1-sol"


@pytest.mark.parametrize("tokens,zone", [
    (0, "normal"), (219_999, "normal"), (220_000, "warning"), (249_999, "warning"), (250_000, "strong"),
    (271_999, "strong"), (272_000, "long"), (500_000, "long")])
def test_zone_bands_for_sol(tokens, zone):
    z = cg.context_zone(tokens, SOL)
    assert z["zone"] == zone and z["threshold"] == 272_000 and (z["warn_at"], z["strong_at"]) == (220_000, 250_000)


def test_long_zone_names_the_pricing_zone_and_offers_the_three_actions():
    z = cg.context_zone(280_000, SOL)
    assert "GPT-6.1 Sol long-context pricing zone" in z["message"]
    assert z["actions"] == ["continue", "compact", "new_session"]
    assert cg.context_zone(100_000, SOL)["actions"] == []


def test_unknown_size_is_unknown_not_guessed():
    z = cg.context_zone(None, SOL)
    assert z["zone"] == "unknown" and z["actions"] == []


def test_models_without_a_threshold_in_the_pricing_table_are_not_judged():
    for model in ("gpt-6-astra", "gpt-5.5", None, ""):
        z = cg.context_zone(900_000, model)
        assert z["zone"] == "n/a" and z["threshold"] is None and z["actions"] == []


def test_zone_uses_the_context_size_and_has_no_notion_of_accumulated_usage():
    import inspect
    assert list(inspect.signature(cg.context_zone).parameters) == ["context_tokens", "model", "context_window"]
    # a long thread has used millions of tokens in total; each request is small -> normal
    assert cg.context_zone(90_000, SOL)["zone"] == "normal"


def test_note_when_the_window_is_below_the_threshold():
    z = cg.context_zone(255_000, SOL, 258_400)
    assert z["zone"] == "strong" and "cannot be reached" in z["note"] and z["reachable"] is False
    assert "note" not in cg.context_zone(255_000, SOL, 872_000) and cg.context_zone(1, SOL, 872_000)["reachable"] is True


# ----- tool output records -----

def cmd(output, exit_code=0, command="rg foo"):
    return {"type": "commandExecution", "command": command, "aggregatedOutput": output, "exitCode": exit_code}


def test_tool_output_sizes_and_the_model_visible_cap():
    small = cg.tool_output_record(cmd("x" * 4_000), 10_000, TH)
    assert small["size"] == "normal" and small["raw_tokens_est"] == 1_000 and not small["truncated_for_model"]
    big = cg.tool_output_record(cmd("x" * 100_000), 10_000, TH)  # 25,000 tokens raw
    assert big["size"] == "large" and big["raw_tokens_est"] == 25_000 and big["model_tokens_est"] == 10_000 and big["truncated_for_model"]
    warn = cg.tool_output_record(cmd("x" * 40_000), None, TH)    # 10,000 tokens: above 8k, below 16k
    assert warn["size"] == "warn" and warn["model_tokens_est"] == 10_000
    assert cg.tool_output_record(cmd("x" * 32_000), None, TH)["size"] == "warn"   # exactly 8,000
    assert cg.tool_output_record(cmd("x" * 31_996), None, TH)["size"] == "normal"


def test_8k_16k_thresholds_are_configurable():
    t = Thresholds(tool_output_warn_tokens=1_000, tool_output_large_tokens=2_000)
    assert cg.tool_output_record(cmd("x" * 4_000), None, t)["size"] == "warn"
    assert cg.tool_output_record(cmd("x" * 8_000), None, t)["size"] == "large"


def test_other_items_have_no_output_record():
    assert cg.tool_output_record({"type": "agentMessage", "text": "hi"}, None, TH) is None
    assert cg.tool_output_record(None, None, TH) is None
    mcp = cg.tool_output_record({"type": "mcpToolCall", "server": "s", "tool": "t", "status": "completed", "result": {"content": "y" * 400}}, None, TH)
    assert mcp["tool"] == "mcp" and mcp["label"] == "s/t" and not mcp["failed"]


def test_context_jump_after_a_large_output():
    big = cg.tool_output_record(cmd("x" * 120_000, command="cat huge.log"), 10_000, TH)
    ev = cg.context_jump(50_000, 62_000, big, TH)
    assert ev["kind"] == "context_jump" and ev["grew_tokens"] == 12_000 and "cat huge.log" in ev["message"]
    assert cg.context_jump(50_000, 52_000, big, TH) is None      # small growth
    assert cg.context_jump(50_000, 62_000, None, TH) is None     # no big output before it
    assert cg.context_jump(None, 62_000, big, TH) is None        # unknown previous size: no guess


# ----- retry guard -----

@pytest.mark.parametrize("kind,reason", [("contextWindowExceeded", "context_window_exceeded"), ("usageLimitExceeded", "quota_exhausted"),
                                         ("rateLimitExceeded", "quota_exhausted"), ("unauthorized", "authentication_error"),
                                         ("sessionBudgetExceeded", "session_budget_exceeded")])
def test_non_retryable_errors_have_a_stop_reason_and_a_message(kind, reason):
    assert cg.stop_reason_for_error(kind) == reason and cg.STOP_MESSAGES[reason]


@pytest.mark.parametrize("kind", ["serverOverloaded", "httpConnectionFailed", "", "other", None])
def test_other_errors_are_left_to_the_normal_recovery(kind):
    assert cg.stop_reason_for_error(kind) == ""


def test_only_context_overflow_blocks_a_plain_resend():
    assert cg.blocks_resend("context_window_exceeded")
    for r in ("", "quota_exhausted", "authentication_error", "repeated_tool_failure"):
        assert not cg.blocks_resend(r)


def test_repeated_identical_failure_is_detected():
    d = cg.RepeatFailureDetector(3)
    bad = cg.tool_output_record(cmd("boom", exit_code=2, command="make test"), None, TH)
    assert d.feed(bad) is None and d.feed(bad) is None
    stop = d.feed(bad)
    assert stop["reason"] == "repeated_tool_failure" and stop["count"] == 3 and "make test" in stop["message"]


def test_a_success_or_a_different_failure_resets_the_count():
    d = cg.RepeatFailureDetector(3)
    a = cg.tool_output_record(cmd("boom", 2, "make test"), None, TH)
    b = cg.tool_output_record(cmd("other error", 2, "make test"), None, TH)   # same command, different output
    ok = cg.tool_output_record(cmd("fine", 0, "make test"), None, TH)
    assert d.feed(a) is None and d.feed(a) is None
    assert d.feed(ok) is None and d.feed(a) is None and d.feed(a) is None   # reset by the success
    assert d.feed(b) is None and d.feed(a) is None and d.feed(a) is None    # reset by the different failure
    assert d.feed(None) is None


def test_failures_of_different_commands_do_not_add_up():
    d = cg.RepeatFailureDetector(3)
    for i in range(10):
        assert d.feed(cg.tool_output_record(cmd("boom", 1, f"cmd{i}"), None, TH)) is None
