"""The app-server backend: 1 task = 1 Codex thread, turns, usage, quota, steering, compaction.

Everything runs against tests/fake_app_server.py through the real JSON-RPC client and TaskManager.
"""
import asyncio
import json

import pytest

from app.task_manager import OBSERVED_OVERLAP_NOTE, TaskError

from conftest import wait_for

IDLE = ("queued", "starting", "running")


def go(coro):
    return asyncio.run(coro)


async def create(m, repo, prompt="ok", **kw):
    return await m.create_task(repository=str(repo), prompt=prompt, name=kw.pop("name", "task"), **kw)


async def finished(m, task_id, timeout=15.0):
    return await wait_for(lambda: m.get(task_id)["status"] not in IDLE and m.get(task_id), timeout)


async def running(m, task_id, timeout=15.0):
    return await wait_for(lambda: m.get(task_id)["status"] == "running" and m.get(task_id), timeout)


def calls(state, method=None):
    rows = [json.loads(line) for line in (state / "invocations.jsonl").read_text().splitlines()]
    return [r for r in rows if method is None or r["method"] == method]


def messages(m, task_id):
    return [e["message"] for e in m.read_log(task_id)[0] if e["stream"] == "system"]


# ---------- 1 task = 1 thread ----------

def test_thread_id_is_saved_and_the_same_thread_is_reused(git_repo, make_manager, fake_codex_state):
    m = make_manager(backend="app-server")

    async def scenario():
        t = await create(m, git_repo)
        first = await finished(m, t["id"])
        thread = first["codex_thread_id"]
        assert thread.startswith("thr-") and first["last_turn_at"] and first["status"] == "completed"
        for n in range(2):
            await m.send_instruction(t["id"], f"more {n}")
            done = await finished(m, t["id"])
            assert done["codex_thread_id"] == thread and done["status"] == "completed"
        # a new thread was created exactly once; later turns resumed it and ran turn/start on it
        assert len(calls(fake_codex_state, "thread/start")) == 1
        resumes = calls(fake_codex_state, "thread/resume")
        assert [r["params"]["threadId"] for r in resumes] == [thread, thread]
        assert [r["params"]["threadId"] for r in calls(fake_codex_state, "turn/start")] == [thread] * 3
        assert [r["turn"] for r in m.db.list_turns(t["id"])] == [1, 2, 3]
        await m.shutdown()

    go(scenario())


def test_the_thread_survives_a_gui_restart(git_repo, make_manager, fake_codex_state):
    """A new manager (= a new app-server process) resumes the thread recorded in the DB."""
    m1 = make_manager(backend="app-server")

    async def first():
        t = await create(m1, git_repo)
        done = await finished(m1, t["id"])
        await m1.shutdown()
        return t["id"], done["codex_thread_id"]

    task_id, thread = go(first())
    m2 = make_manager(backend="app-server")

    async def second():
        await m2.send_instruction(task_id, "again")
        done = await finished(m2, task_id)
        assert done["codex_thread_id"] == thread and done["status"] == "completed"
        await m2.shutdown()

    go(second())
    assert len(calls(fake_codex_state, "thread/start")) == 1


def test_history_is_never_resent(git_repo, make_manager, fake_codex_state):
    m = make_manager(backend="app-server")

    async def scenario():
        t = await create(m, git_repo, "ok first prompt")
        await finished(m, t["id"])
        await m.send_instruction(t["id"], "ok second")
        await finished(m, t["id"])
        await m.shutdown()

    go(scenario())
    texts = [r["params"]["input"][0]["text"] for r in calls(fake_codex_state, "turn/start")]
    assert texts == ["ok first prompt", "ok second"]  # exactly what the user typed, nothing prepended


# ---------- settings: defaults and what reaches Codex ----------

def test_defaults_are_standard_auto_approve_sandboxed_web_search_off(git_repo, make_manager, fake_codex_state):
    m = make_manager(backend="app-server")

    async def scenario():
        t = await create(m, git_repo)
        assert t["service_tier"] == "default" and t["auto_approval"] == 1
        assert t["sandbox"] == "workspace-write" and t["web_search_enabled"] == 0
        assert t["model_verbosity"] == "low" and t["adaptive_reasoning"] == 1 and t["context_guard"] == 1
        await finished(m, t["id"])
        await m.shutdown()

    go(scenario())
    p = calls(fake_codex_state, "thread/start")[0]["params"]
    assert p["serviceTier"] == "default"                                   # Standard, not Fast
    assert p["approvalPolicy"] == "on-request" and p["approvalsReviewer"] == "auto_review"  # --approve-for-me
    assert p["sandbox"] == "workspace-write"
    assert p["config"]["web_search"] == "disabled" and p["config"]["model_verbosity"] == "low"
    assert "model" not in p  # Codex default model unless one was chosen
    assert "danger" not in json.dumps(p)


def test_model_effort_tier_and_the_rest_are_passed_and_kept_for_every_turn(git_repo, make_manager, fake_codex_state):
    m = make_manager(backend="app-server")

    async def scenario():
        t = await create(m, git_repo, model="gpt-6.1-sol", reasoning_effort="medium", service_tier="priority",
                         model_verbosity="high", web_search=True, auto_approval=False,
                         writable_dirs="/data/shared", feature_flags="foo")
        await finished(m, t["id"])
        await m.send_instruction(t["id"], "next")
        await finished(m, t["id"])
        await m.shutdown()

    go(scenario())
    start = calls(fake_codex_state, "thread/start")[0]["params"]
    resume = calls(fake_codex_state, "thread/resume")[0]["params"]
    for p in (start, resume):  # a resumed thread gets the identical settings: nothing drifts inside a task
        assert p["model"] == "gpt-6.1-sol" and p["serviceTier"] == "priority"
        assert p["approvalPolicy"] == "never" and p["sandbox"] == "workspace-write"  # off: nothing may ask a human
        assert p["config"]["model_verbosity"] == "high" and p["config"]["web_search"] == "live"
        assert p["config"]["model_reasoning_effort"] == "medium"
        assert p["config"]["sandbox_workspace_write"] == {"writable_roots": ["/data/shared"]}
        assert p["config"]["features"] == {"foo": True}
    assert [r["params"]["effort"] for r in calls(fake_codex_state, "turn/start")] == ["medium", "medium"]


def test_effort_default_sends_nothing(git_repo, make_manager, fake_codex_state):
    m = make_manager(backend="app-server")

    async def scenario():
        t = await create(m, git_repo)
        await finished(m, t["id"])
        await m.shutdown()

    go(scenario())
    assert "effort" not in calls(fake_codex_state, "turn/start")[0]["params"]
    assert "model_reasoning_effort" not in calls(fake_codex_state, "thread/start")[0]["params"]["config"]


@pytest.mark.parametrize("kw", [dict(sandbox="danger-full-access"), dict(model_verbosity="loud"),
                                dict(service_tier="Fast!"), dict(writable_dirs="relative/dir"),
                                dict(feature_flags="a;b"), dict(reasoning_effort="HIGH")])
def test_invalid_settings_are_rejected(git_repo, make_manager, kw):
    m = make_manager(backend="app-server")

    async def scenario():
        with pytest.raises(TaskError) as ei:
            await create(m, git_repo, **kw)
        assert ei.value.status == 400

    go(scenario())


def test_common_instructions_are_given_once_to_a_new_thread_and_are_stable(git_repo, make_manager, fake_codex_state):
    m = make_manager(backend="app-server")

    async def scenario():
        a = await create(m, git_repo, name="a")
        b = await create(m, git_repo, name="b")
        await finished(m, a["id"])
        await finished(m, b["id"])
        await m.send_instruction(a["id"], "again")
        await finished(m, a["id"])
        await m.shutdown()

    go(scenario())
    starts = [r["params"] for r in calls(fake_codex_state, "thread/start")]
    assert len(starts) == 2
    text = starts[0]["developerInstructions"]
    assert "Avoid dumping entire large files or logs" in text and "rg" in text
    assert starts[1]["developerInstructions"] == text  # identical for every task: a stable cache prefix
    assert "developerInstructions" not in calls(fake_codex_state, "thread/resume")[0]["params"]
    # nothing that varies between tasks or runs may be in it
    import re
    assert not re.search(r"\d|\b(pid|uuid|today|now)\b", text, re.I)  # no date, id, pid or quota figure


# ---------- subscription only ----------

@pytest.mark.parametrize("account", ["apiKey", "none"])
def test_no_fallback_to_api_key_auth(git_repo, make_manager, fake_codex_state, monkeypatch, account):
    monkeypatch.setenv("FAKE_ACCOUNT", account)
    m = make_manager(backend="app-server")

    async def scenario():
        t = await create(m, git_repo)
        done = await finished(m, t["id"])
        assert done["status"] == "failed" and "ChatGPT" in done["status_detail"]
        assert "does not fall back to API billing" in done["status_detail"]
        await m.shutdown()

    go(scenario())
    assert calls(fake_codex_state, "thread/start") == [] and calls(fake_codex_state, "turn/start") == []


def test_api_key_login_is_only_allowed_when_subscription_only_is_off(git_repo, make_manager, monkeypatch):
    monkeypatch.setenv("FAKE_ACCOUNT", "apiKey")
    m = make_manager(backend="app-server", subscription_only=False)

    async def scenario():
        t = await create(m, git_repo)
        assert (await finished(m, t["id"]))["status"] == "completed"
        await m.shutdown()

    go(scenario())


# ---------- token usage and cache ----------

def test_usage_per_turn_cache_hit_and_latest_figures(git_repo, make_manager):
    m = make_manager(backend="app-server")

    async def scenario():
        t = await create(m, git_repo)
        await finished(m, t["id"])
        await m.send_instruction(t["id"], "second")
        done = await finished(m, t["id"])
        turns = m.db.list_turns(t["id"])
        # fake: every turn adds input 1000 / output 20; cached 0 on the thread's first turn, 900 after
        assert [(r["input_tokens"], r["cached_input_tokens"], r["output_tokens"]) for r in turns] == \
               [(1000, 0, 20), (1000, 900, 20)]
        assert [r["cache_hit_rate"] for r in turns] == [0.0, 90.0]
        assert [r["status"] for r in turns] == ["completed", "completed"] and all(r["kind"] == "turn" for r in turns)
        assert all(r["started_at"] and r["finished_at"] and r["turn_id"] for r in turns)
        # the task carries the latest turn's figures
        assert (done["latest_input_tokens"], done["latest_cached_input_tokens"], done["latest_output_tokens"]) == (1000, 900, 20)
        view = m.usage(t["id"])
        assert view["latest"]["cache_hit_rate"] == 90.0 and view["latest"]["uncached_input_tokens"] == 100
        assert m.list_tasks_view()[0]["cache_hit_rate"] == 90.0
        await m.shutdown()

    go(scenario())


# ---------- context size and Context Guard ----------

def test_context_size_and_guard_follow_the_reported_window(git_repo, make_manager):
    m = make_manager(backend="app-server", context_warn_percent=80)

    async def scenario():
        t = await create(m, git_repo)
        done = await finished(m, t["id"])
        assert (done["context_tokens"], done["context_window"]) == (1000, 10000)  # window comes from Codex
        assert m.present_task(done)["context"] == {"tokens": 1000, "window": 10000, "percent": 10.0, "warn": False}
        for n in range(7):  # fake: the context grows by 1000 a turn
            await m.send_instruction(t["id"], f"more {n}")
            await finished(m, t["id"])
        ctx = m.present_task(m.get(t["id"]))["context"]
        assert ctx["tokens"] == 8000 and ctx["percent"] == 80.0 and ctx["warn"] is True
        await m.shutdown()

    go(scenario())


def test_context_guard_can_be_switched_off(git_repo, make_manager):
    m = make_manager(backend="app-server", context_warn_percent=5)

    async def scenario():
        t = await create(m, git_repo, context_guard=False)
        done = await finished(m, t["id"])
        assert m.present_task(done)["context"]["warn"] is False  # 10% > 5%, but the guard is off
        await m.shutdown()

    go(scenario())


# ---------- rate limits ----------

def test_rate_limit_before_after_and_history(git_repo, make_manager):
    m = make_manager(backend="app-server")

    async def scenario():
        t = await create(m, git_repo)
        done = await finished(m, t["id"])
        # fake: 5 hour window starts at 30 and rises one point per turn; weekly is 12 + number of threads
        assert (done["five_hour_used_before"], done["five_hour_used_after"]) == (30, 31)
        assert (done["weekly_used_before"], done["weekly_used_after"]) == (12, 13)
        view = m.present_task(done)
        assert view["observed_quota"]["five_hour"] == [30, 31] and view["observed_quota"]["overlap"] is False
        assert "Observed only; not an exact per-task cost." in view["observed_quota"]["note"]
        history = m.db.list_rate_limits(task_id=t["id"])
        assert {h["reason"] for h in history} >= {"turn_start", "turn_end"}
        h = history[0]
        assert (h["primary_used_percent"], h["primary_window_mins"], h["secondary_used_percent"],
                h["secondary_window_mins"], h["plan_type"]) == (31, 300, 13, 10080, "pro")
        limits = await m.rate_limits(force=True)
        assert limits["available"] and limits["five_hour_used"] == 31 and limits["weekly_used"] == 13
        assert [w["label"] for w in limits["windows"]] == ["5 hour", "Weekly"]
        assert limits["available_resets"] == 2
        await m.shutdown()

    go(scenario())


def test_overlapping_tasks_are_flagged_not_attributed(git_repo, make_manager):
    m = make_manager(backend="app-server")

    async def scenario():
        a = await create(m, git_repo, "sleep", name="a")
        await running(m, a["id"])
        b = await create(m, git_repo, "ok", name="b")
        done_b = await finished(m, b["id"])
        assert m.present_task(done_b)["observed_quota"]["overlap"] is True
        assert OBSERVED_OVERLAP_NOTE in m.present_task(done_b)["observed_quota"]["note"]
        await m.stop(a["id"])
        await finished(m, a["id"])
        await m.shutdown()

    go(scenario())


def test_the_gui_never_redeems_resets(git_repo, make_manager, fake_codex_state):
    m = make_manager(backend="app-server")

    async def scenario():
        t = await create(m, git_repo)
        await finished(m, t["id"])
        await m.rate_limits(force=True)
        await m.shutdown()

    go(scenario())
    assert not [c for c in calls(fake_codex_state) if "Credit" in c["method"] or "consume" in c["method"]]


# ---------- quota exhaustion ----------

def test_usage_limit_error_puts_the_task_in_waiting_for_quota(git_repo, make_manager, fake_codex_state):
    m = make_manager(backend="app-server")

    async def scenario():
        t = await create(m, git_repo, "quota")
        done = await finished(m, t["id"])
        assert done["status"] == "waiting-for-quota" and "usageLimitExceeded" in done["status_detail"]
        assert m.is_terminal(done)
        assert len(calls(fake_codex_state, "turn/start")) == 1  # nothing was retried
        # the user re-runs later: the same thread continues
        await m.send_instruction(t["id"], "ok")
        assert (await finished(m, t["id"]))["status"] == "completed"
        await m.shutdown()

    go(scenario())


def test_ordinary_usage_unavailable_does_not_even_start_a_turn(git_repo, make_manager, fake_codex_state):
    (fake_codex_state / "rate.json").write_text(json.dumps({
        "ordinaryUsageAllowed": False, "rateLimits": {
            "limitId": "codex", "planType": "pro", "rateLimitReachedType": "rate_limit_reached",
            "primary": {"usedPercent": 100, "windowDurationMins": 300, "resetsAt": 1900000000}, "secondary": None}}))
    m = make_manager(backend="app-server")

    async def scenario():
        t = await create(m, git_repo)
        done = await finished(m, t["id"])
        assert done["status"] == "waiting-for-quota" and "rate limit reached" in done["status_detail"]
        assert calls(fake_codex_state, "turn/start") == [] and calls(fake_codex_state, "thread/start") == []
        # no automatic retry loop: give it a moment and check nothing happened
        await asyncio.sleep(0.5)
        assert calls(fake_codex_state, "turn/start") == []
        await m.shutdown()

    go(scenario())


def test_a_plain_failure_is_failed_not_quota(git_repo, make_manager):
    m = make_manager(backend="app-server")

    async def scenario():
        t = await create(m, git_repo, "fail")
        done = await finished(m, t["id"])
        assert done["status"] == "failed" and done["status_detail"] == "boom"
        await m.shutdown()

    go(scenario())


# ---------- adaptive reasoning (suggestion only) ----------

def test_retry_with_higher_effort_is_offered_after_a_failed_turn_and_never_applied_automatically(git_repo, make_manager, fake_codex_state):
    m = make_manager(backend="app-server")

    async def scenario():
        t = await create(m, git_repo, "fail", reasoning_effort="low")
        done = await finished(m, t["id"])
        assert m.present_task(done)["retry_suggestion"] == {"effort": "medium", "reason": "the last turn failed"}
        assert done["reasoning_effort"] == "low"  # still low: nothing escalated by itself
        assert len(calls(fake_codex_state, "turn/start")) == 1
        # the user accepts: the effort changes for this and later turns
        await m.send_instruction(t["id"], "ok", reasoning_effort="medium")
        done = await finished(m, t["id"])
        assert done["reasoning_effort"] == "medium" and done["status"] == "completed"
        assert [r["params"].get("effort") for r in calls(fake_codex_state, "turn/start")] == ["low", "medium"]
        assert m.present_task(done)["retry_suggestion"] is None
        await m.shutdown()

    go(scenario())


def test_no_suggestion_for_quota_stops_high_effort_or_when_switched_off(git_repo, make_manager):
    m = make_manager(backend="app-server")

    async def scenario():
        q = await finished(m, (await create(m, git_repo, "quota", reasoning_effort="low", name="q"))["id"])
        assert m.present_task(q)["retry_suggestion"] is None          # quota is not a reasoning problem
        h = await finished(m, (await create(m, git_repo, "fail", reasoning_effort="high", name="h"))["id"])
        assert m.present_task(h)["retry_suggestion"] is None          # never above high on its own
        u = await finished(m, (await create(m, git_repo, "fail", reasoning_effort="ultra", name="u"))["id"])
        assert m.present_task(u)["retry_suggestion"] is None          # ultra is explicit only
        off = await finished(m, (await create(m, git_repo, "fail", reasoning_effort="low", adaptive_reasoning=False, name="o"))["id"])
        assert m.present_task(off)["retry_suggestion"] is None
        await m.shutdown()

    go(scenario())


# ---------- steering, stopping, failures of the server ----------

def test_additional_instruction_reaches_a_running_turn(git_repo, make_manager, fake_codex_state):
    m = make_manager(backend="app-server")

    async def scenario():
        t = await create(m, git_repo, "sleep")
        await running(m, t["id"])
        await wait_for(lambda: m._active_turns.get(t["id"], {}).get("turn"))
        await m.send_instruction(t["id"], "also do this")
        done = await finished(m, t["id"])
        assert done["status"] == "completed" and done["last_prompt"] == "also do this"
        steer = calls(fake_codex_state, "turn/steer")[0]["params"]
        assert steer["threadId"] == done["codex_thread_id"] and steer["input"][0]["text"] == "also do this"
        assert steer["expectedTurnId"] == m.db.list_turns(t["id"])[0]["turn_id"]
        assert any("turn/steer" in msg for msg in messages(m, t["id"]))
        assert any("steered: also do this" in e["message"] for e in m.read_log(t["id"])[0])
        await m.shutdown()

    go(scenario())


def test_exec_backend_cannot_steer(git_repo, make_manager):
    m = make_manager()  # exec

    async def scenario():
        t = await create(m, git_repo, "sleep")
        await running(m, t["id"])
        with pytest.raises(TaskError) as ei:
            await m.send_instruction(t["id"], "more")
        assert ei.value.status == 409
        await m.stop(t["id"])
        await finished(m, t["id"])

    go(scenario())


def test_stop_interrupts_the_turn(git_repo, make_manager, fake_codex_state):
    m = make_manager(backend="app-server")

    async def scenario():
        t = await create(m, git_repo, "sleep")
        await running(m, t["id"])
        await wait_for(lambda: m._active_turns.get(t["id"], {}).get("turn"))
        await m.stop(t["id"])
        done = await finished(m, t["id"])
        assert done["status"] == "stopped"
        assert calls(fake_codex_state, "turn/interrupt")[0]["params"]["threadId"] == done["codex_thread_id"]
        # the task, worktree and thread are intact: it can simply be continued
        await m.send_instruction(t["id"], "ok again")
        assert (await finished(m, t["id"]))["status"] == "completed"
        await m.shutdown()

    go(scenario())


def test_stop_gives_up_on_a_turn_that_ignores_the_interrupt(git_repo, make_manager):
    m = make_manager(backend="app-server", stop_grace_seconds=1.0)

    async def scenario():
        t = await create(m, git_repo, "hang")
        await running(m, t["id"])
        await wait_for(lambda: m._active_turns.get(t["id"], {}).get("turn"))
        await m.stop(t["id"])
        done = await finished(m, t["id"], timeout=10)
        assert done["status"] == "stopped"
        assert any("did not finish after the interrupt" in msg for msg in messages(m, t["id"]))
        await m.shutdown()

    go(scenario())


def test_server_dying_mid_turn_fails_the_task_and_the_next_turn_restarts_it(git_repo, make_manager):
    m = make_manager(backend="app-server")

    async def scenario():
        t = await create(m, git_repo)
        await finished(m, t["id"])
        await m.send_instruction(t["id"], "die")
        done = await finished(m, t["id"])
        assert done["status"] == "failed" and "exited" in done["status_detail"]
        await m.send_instruction(t["id"], "ok")  # a fresh app-server process resumes the thread
        assert (await finished(m, t["id"]))["status"] == "completed"
        await m.shutdown()

    go(scenario())


def test_a_missing_binary_is_a_clear_failure(git_repo, make_manager):
    m = make_manager(backend="app-server")
    m._app_server.build_command = lambda: ["/nonexistent/codex", "app-server"]

    async def scenario():
        t = await create(m, git_repo)
        done = await finished(m, t["id"])
        assert done["status"] == "failed" and "cannot start codex app-server" in done["status_detail"]

    go(scenario())


# ---------- compaction ----------

def test_compact_keeps_task_worktree_branch_and_thread(git_repo, make_manager, fake_codex_state):
    m = make_manager(backend="app-server")

    async def scenario():
        t = await create(m, git_repo)
        before = await finished(m, t["id"])
        await m.compact(t["id"])
        after = await finished(m, t["id"])
        assert after["status"] == "completed"
        for key in ("codex_thread_id", "worktree", "branch", "id", "base_sha"):
            assert after[key] == before[key]
        assert calls(fake_codex_state, "thread/compact/start")[0]["params"] == {"threadId": before["codex_thread_id"]}
        turns = m.db.list_turns(t["id"])
        assert [r["kind"] for r in turns] == ["turn", "compact"]
        assert after["context_tokens"] is None  # unknown until the next turn reports it
        assert after["latest_input_tokens"] == before["latest_input_tokens"]  # compaction is not "the latest turn"
        await m.send_instruction(t["id"], "next")
        assert (await finished(m, t["id"]))["codex_thread_id"] == before["codex_thread_id"]
        await m.shutdown()

    go(scenario())


def test_compact_refuses_without_a_thread_or_with_exec(git_repo, make_manager):
    m = make_manager()  # exec

    async def scenario():
        t = await create(m, git_repo)
        await finished(m, t["id"])
        with pytest.raises(TaskError) as ei:
            await m.compact(t["id"])
        assert ei.value.code == "unsupported"

    go(scenario())
