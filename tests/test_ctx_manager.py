"""Context Efficiency inside the TaskManager: frozen per-task settings, per-turn speed, cache accounting and events,
compaction monitor, long-context zone, tool-output records, and the retry guard. Runs against tests/fake_app_server_ctx.py
(a scriptable app-server) through the real JSON-RPC client; nothing talks to Codex or a model."""
import ast
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.appserver import AppServerClient
from app.task_manager import TaskManager, TaskError

from conftest import FakeRunner, wait_for

FAKE = str(Path(__file__).parent / "fake_app_server_ctx.py")
IDLE = ("queued", "starting", "running")
CATALOG = {"models": [{"slug": "gpt-6.1-sol", "name": "GPT-6.1-Sol", "tool_output_cap": 10000, "context_window": 272000,
                       "multi_agent_version": "v2", "efforts": [], "service_tiers": [], "priority": 1}],
           "default_model": "gpt-6.1-sol", "default_effort": "", "recommended_model": "", "error": ""}


class CtxFakeServer(AppServerClient):
    def build_command(self):
        return [sys.executable, FAKE]


def go(coro):
    return asyncio.run(coro)


@pytest.fixture
def mgr(settings, db):
    settings.backend = "app-server"
    server = CtxFakeServer("fake", False)
    m = TaskManager(settings, db, FakeRunner(), server)
    server.on_global(m._on_global_notification)
    m.ctx._catalog._cached, m.ctx._catalog._fetched_at = CATALOG, time.monotonic() + 3600  # hermetic: no `codex debug models`

    async def no_probe(*a, **k):  # the live tool-profile measurement is covered by tests/test_ctx_integration.py
        return {"ok": True, "verified": True, "full_count": 187, "profile_count": 8}
    m.ctx.verify_profile = no_probe
    yield m
    proc = getattr(server, "_proc", None)
    if proc is not None and proc.returncode is None:
        proc.kill()


def script(state, *turns):
    (state / "script.json").write_text(json.dumps({"turns": [list(t) for t in turns]}))


def calls(state, method=None):
    path = state / "invocations.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    return [r for r in rows if method is None or r["method"] == method]


async def create(m, repo, prompt="go", **kw):
    return await m.create_task(repository=str(repo), prompt=prompt, name="t", **kw)


async def finished(m, task_id, timeout=20.0):
    return await wait_for(lambda: m.get(task_id)["status"] not in IDLE and m.get(task_id), timeout)


async def turn(m, task_id, prompt="more", **kw):
    await m.send_instruction(task_id, prompt, **kw)
    return await finished(m, task_id)


def use(inp, cached=0, write=0, out=10, **extra):
    return {"a": "usage", "input": inp, "cached": cached, "write": write, "output": out, **extra}


def events(m, task_id, kind=None):
    return [e for e in m.db.list_context_events(task_id) if kind is None or e["kind"] == kind]


# ------------------------------------------------------------------ frozen settings

def test_new_tasks_default_to_nested_agents_off(git_repo, mgr, fake_codex_state):
    async def scenario():
        t = await create(mgr, git_repo)
        await finished(mgr, t["id"])
        cfg = calls(fake_codex_state, "thread/start")[0]["params"]["config"]
        assert cfg["features"]["multi_agent"] is False
        assert cfg["features"]["multi_agent_v2"] == {"max_concurrent_threads_per_session": 1}
        assert cfg["agents"] == {"max_concurrent_threads_per_session": 1}
        assert "tool_output_token_limit" not in cfg and "skills" not in cfg   # presets default to Codex's own behaviour
        assert mgr.get(t["id"])["allow_subagents"] == 0
        await mgr.shutdown()
    go(scenario())


def test_allow_subagents_turns_the_overrides_off(git_repo, mgr, fake_codex_state):
    async def scenario():
        t = await create(mgr, git_repo, allow_subagents=True)
        await finished(mgr, t["id"])
        cfg = calls(fake_codex_state, "thread/start")[0]["params"]["config"]
        assert "agents" not in cfg and "multi_agent" not in cfg.get("features", {}) and "multi_agent_v2" not in cfg.get("features", {})
        assert mgr.get(t["id"])["allow_subagents"] == 1
        await mgr.shutdown()
    go(scenario())


def test_old_tasks_keep_codex_behaviour(git_repo, mgr, db, fake_codex_state):
    """A task row from before these settings (allow_subagents defaults to 1) gets no override."""
    async def scenario():
        t = await create(mgr, git_repo, allow_subagents=True)
        await finished(mgr, t["id"])
        assert ctx_cfg(db.get_task(t["id"])) == {}
        await mgr.shutdown()
    from app.ctx_config import efficiency_config as ctx_cfg
    go(scenario())


def test_presets_reach_codex_and_stay_the_same_every_turn(git_repo, mgr, fake_codex_state):
    async def scenario():
        t = await create(mgr, git_repo, tool_output="conservative", skills="economy", tool_profile="development")
        await finished(mgr, t["id"])
        await turn(mgr, t["id"])
        await turn(mgr, t["id"])
        configs = [c["params"]["config"] for c in calls(fake_codex_state, "thread/start") + calls(fake_codex_state, "thread/resume")]
        assert len(configs) == 3
        for cfg in configs:
            assert cfg["tool_output_token_limit"] == 8000 and cfg["skills"] == {"max_context_tokens": 2000}
            assert cfg["features"]["apps"] is False and cfg["features"]["plugins"] is False
        assert configs[0] == configs[1] == configs[2]
        row = mgr.get(t["id"])
        assert (row["tool_output_preset"], row["tool_output_limit"], row["skills_preset"], row["skills_budget"]) == ("conservative", 8000, "economy", 2000)
        await mgr.shutdown()
    go(scenario())


def test_invalid_settings_are_rejected(git_repo, mgr):
    async def scenario():
        for kw in ({"tool_output": "huge"}, {"skills": "x"}, {"tool_profile": "everything"}, {"tool_output_limit": 3}, {"skills_budget": 10 ** 9}):
            with pytest.raises(TaskError):
                await create(mgr, git_repo, **kw)
        assert mgr.db.list_tasks() == []  # nothing was created
        await mgr.shutdown()
    go(scenario())


def test_minimal_disables_the_mcp_servers_found_in_codex_config(git_repo, mgr, fake_codex_state):
    (fake_codex_state / "config.json").write_text(json.dumps({"mcp_servers": {"alpha": {}, "beta": {}}}))

    async def scenario():
        t = await create(mgr, git_repo, tool_profile="minimal")
        await finished(mgr, t["id"])
        cfg = calls(fake_codex_state, "thread/start")[0]["params"]["config"]
        assert cfg["mcp_servers"] == {"alpha": {"enabled": False}, "beta": {"enabled": False}} and cfg["features"]["apps"] is False
        await mgr.shutdown()
    go(scenario())


def test_the_tool_profile_is_frozen_and_changing_it_needs_confirmation(git_repo, mgr, fake_codex_state):
    (fake_codex_state / "config.json").write_text(json.dumps({"mcp_servers": {"alpha": {}}}))

    async def scenario():
        t = await create(mgr, git_repo, tool_profile="development")
        await finished(mgr, t["id"])
        # a change of the user's MCP servers afterwards must not leak into this thread
        (fake_codex_state / "config.json").write_text(json.dumps({"mcp_servers": {"alpha": {}, "gamma": {}}}))
        await turn(mgr, t["id"])
        starts = [c["params"]["config"] for c in calls(fake_codex_state, "thread/start") + calls(fake_codex_state, "thread/resume")]
        assert starts[0] == starts[1]
        with pytest.raises(TaskError) as e:
            await mgr.change_tool_profile(t["id"], "minimal")
        assert e.value.code == "confirm_cache_loss" and "may reduce prompt cache reuse" in str(e.value)
        assert mgr.get(t["id"])["tool_profile"] == "development"
        with pytest.raises(TaskError):
            await mgr.change_tool_profile(t["id"], "development", confirm=True)  # unchanged
        changed = await mgr.change_tool_profile(t["id"], "minimal", confirm=True)
        assert changed["tool_profile"] == "minimal"
        ev = events(mgr, t["id"], "tool_profile_change")
        assert len(ev) == 1 and "prompt cache may not be reused" in ev[0]["message"]
        await turn(mgr, t["id"])
        last = calls(fake_codex_state, "thread/resume")[-1]["params"]["config"]
        assert last["mcp_servers"] == {"alpha": {"enabled": False}, "gamma": {"enabled": False}}
        await mgr.shutdown()
    go(scenario())


def test_the_profile_cannot_change_while_the_task_runs(git_repo, mgr, fake_codex_state):
    script(fake_codex_state, [{"a": "wait_interrupt", "max": 10}])

    async def scenario():
        t = await create(mgr, git_repo, tool_profile="development")
        await wait_for(lambda: mgr.get(t["id"])["status"] == "running")
        with pytest.raises(TaskError):
            await mgr.change_tool_profile(t["id"], "minimal", confirm=True)
        await mgr.stop(t["id"])
        await finished(mgr, t["id"])
        await mgr.shutdown()
    go(scenario())


def test_custom_working_directory(git_repo, mgr, fake_codex_state):
    sub = git_repo / "pkg" / "inner"
    sub.mkdir(parents=True)
    (sub / "x.txt").write_text("x")
    subprocess.run(["git", "-C", str(git_repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(git_repo), "commit", "-q", "-m", "pkg"], check=True)

    async def scenario():
        t = await create(mgr, git_repo, cwd_subdir="pkg/inner")
        await finished(mgr, t["id"])
        p = calls(fake_codex_state, "thread/start")[0]["params"]
        assert p["cwd"] == str(Path(mgr.get(t["id"])["worktree"]) / "pkg" / "inner")
        assert mgr.get(t["id"])["worktree"] in p["config"]["sandbox_workspace_write"]["writable_roots"]  # whole worktree stays writable
        default = await create(mgr, git_repo)
        await finished(mgr, default["id"])
        assert calls(fake_codex_state, "thread/start")[1]["params"]["cwd"] == mgr.get(default["id"])["worktree"]  # the repo root by default
        for bad in ("../outside", "/etc", "nope/missing", "pkg/../..", "-x"):
            with pytest.raises(TaskError):
                await create(mgr, git_repo, cwd_subdir=bad)
        await mgr.shutdown()
    go(scenario())


# ------------------------------------------------------------------ Standard / Fast per turn

def test_the_speed_is_requested_and_recorded_per_turn(git_repo, mgr, fake_codex_state):
    async def scenario():
        t = await create(mgr, git_repo)
        await finished(mgr, t["id"])
        await turn(mgr, t["id"], service_tier="fast")
        await turn(mgr, t["id"], service_tier="standard")
        await turn(mgr, t["id"])                       # unspecified: the task's own speed
        rows = mgr.db.list_turns(t["id"])
        assert [r["service_tier"] for r in rows] == ["default", "priority", "default", "default"]
        starts = [c["params"].get("serviceTierForTurn") for c in calls(fake_codex_state, "turn/start")]
        assert starts == ["default", "priority", "default", "default"]
        # the thread's own default was never changed: serviceTier (sticky) is not sent on turn/start
        assert all("serviceTier" not in c["params"] for c in calls(fake_codex_state, "turn/start"))
        assert [r["service_tier"] for r in mgr.usage(t["id"])["turns"]] == ["default", "priority", "default", "default"]
        with pytest.raises(TaskError):
            await mgr.send_instruction(t["id"], "x", service_tier="turbo!")
        await mgr.shutdown()
    go(scenario())


def test_a_fast_task_records_priority_for_its_turns(git_repo, mgr, fake_codex_state):
    async def scenario():
        t = await create(mgr, git_repo, service_tier="priority")
        await finished(mgr, t["id"])
        await turn(mgr, t["id"], service_tier="standard")
        assert [r["service_tier"] for r in mgr.db.list_turns(t["id"])] == ["priority", "default"]
        await mgr.shutdown()
    go(scenario())


def test_fast_standard_savings_follow_the_turns(git_repo, mgr, fake_codex_state):
    script(fake_codex_state, [use(200_000, 0, 0, 0)], [use(200_000, 0, 0, 0)])  # below 272K: no long-context rate

    async def scenario():
        t = await create(mgr, git_repo, model="gpt-6.1-sol")
        await finished(mgr, t["id"])
        await turn(mgr, t["id"], service_tier="fast")
        eff = mgr.usage(t["id"])["efficiency"]["total"]["usd"]
        assert eff["actual"] == pytest.approx(0.2 * 2.00 + 0.2 * 4.00)   # Standard $2/M, then Fast $4/M
        await mgr.shutdown()
    go(scenario())


# ------------------------------------------------------------------ cache accounting

def test_cache_read_write_and_requests_are_stored(git_repo, mgr, fake_codex_state):
    script(fake_codex_state, [use(10_000, 6_000, 3_000, 50), use(12_000, 9_000, 500, 20)])

    async def scenario():
        t = await create(mgr, git_repo, model="gpt-6.1-sol")
        done = await finished(mgr, t["id"])
        row = mgr.db.list_turns(t["id"])[0]
        assert (row["input_tokens"], row["cached_input_tokens"], row["cache_write_input_tokens"], row["output_tokens"]) == (22_000, 15_000, 3_500, 70)
        assert row["requests"] == 2 and row["max_request_input"] == 12_000
        assert json.loads(row["requests_json"]) == [{"i": 10000, "c": 6000, "w": 3000, "o": 50}, {"i": 12000, "c": 9000, "w": 500, "o": 20}]
        assert done["last_cache_activity_at"] and done["last_request_input"] == 12_000
        view = mgr.usage(t["id"])
        assert view["turns"][0]["cache_write_input_tokens"] == 3_500 and view["turns"][0]["uncached_input_tokens"] == 7_000
        assert view["efficiency"]["cache_write_input_tokens"] == 3_500
        assert view["efficiency"]["total"]["usd"]["cache_write_cost"] == pytest.approx(3_500 * 2.5 / 1e6)
        ctx = mgr.present_task(mgr.get(t["id"]))["ctx"]
        assert ctx["turn_series"][0]["cache_write"] == 3_500 and ctx["cache_age"]["state"] == "hot"
        await mgr.shutdown()
    go(scenario())


def test_a_duplicate_usage_update_is_counted_once(git_repo, mgr, fake_codex_state):
    script(fake_codex_state, [use(1000, 0, 0, 10), {"a": "usage", "input": 0, "cached": 0, "output": 0}])  # total does not move

    async def scenario():
        t = await create(mgr, git_repo)
        await finished(mgr, t["id"])
        assert mgr.db.list_turns(t["id"])[0]["requests"] == 1
        await mgr.shutdown()
    go(scenario())


def test_cache_miss_is_an_event_with_possible_causes(git_repo, mgr, fake_codex_state):
    script(fake_codex_state,
           [use(20_000, 0, 20_000)],                 # 1: a new thread writes its cache (expected)
           [use(21_000, 20_000, 1_000)],             # 2: hit
           [use(61_000, 21_000, 0)],                 # 3: 40,000 uncached: a miss, nothing the GUI changed
           [use(62_000, 1_000, 0)],                  # 4: a miss after a speed change
           [use(63_000, 2_000, 0)])                  # 5: a miss after a reasoning change

    async def scenario():
        t = await create(mgr, git_repo)
        await finished(mgr, t["id"])
        first = events(mgr, t["id"], "cache_miss")
        assert [e["severity"] for e in first] == ["info"] and "first turn" in first[0]["data"]["possible_causes"][0]
        await turn(mgr, t["id"])
        assert len(events(mgr, t["id"], "cache_miss")) == 1
        await turn(mgr, t["id"])
        miss = events(mgr, t["id"], "cache_miss")[-1]
        assert miss["severity"] == "warning" and miss["turn"] == 3 and miss["data"]["uncached_tokens"] == 40_000
        assert miss["data"]["cause_unknown"] and "may have changed inside Codex" in miss["data"]["possible_causes"][0]
        await turn(mgr, t["id"], service_tier="fast")
        assert any("service tier change" in c for c in events(mgr, t["id"], "cache_miss")[-1]["data"]["possible_causes"])
        await turn(mgr, t["id"], reasoning_effort="high")
        assert any("reasoning effort change" in c for c in events(mgr, t["id"], "cache_miss")[-1]["data"]["possible_causes"])
        shown = mgr.present_task(mgr.get(t["id"]))["ctx"]["cache_misses"]
        assert len(shown) == 4
        await mgr.shutdown()
    go(scenario())


def test_cache_miss_thresholds_are_configurable(git_repo, mgr, fake_codex_state):
    script(fake_codex_state, [use(20_000, 0, 20_000)], [use(26_000, 20_000, 0)])   # 6,000 uncached on turn 2

    async def scenario():
        mgr.ctx.set_thresholds({"cache_miss_uncached_tokens": 5_000})
        t = await create(mgr, git_repo)
        await finished(mgr, t["id"])
        await turn(mgr, t["id"])
        assert events(mgr, t["id"], "cache_miss")[-1]["turn"] == 2
        with pytest.raises(ValueError):
            mgr.ctx.set_thresholds({"cache_miss_uncached_tokens": 1})
        assert mgr.ctx.threshold_values()["values"]["cache_miss_uncached_tokens"] == 5_000
        mgr.ctx.reset_thresholds()
        assert mgr.ctx.threshold_values()["values"]["cache_miss_uncached_tokens"] == 10_000
        await mgr.shutdown()
    go(scenario())


def test_idle_gap_is_a_possible_cause(git_repo, mgr, fake_codex_state):
    script(fake_codex_state, [use(20_000, 0, 20_000)], [use(61_000, 1_000, 0)])

    async def scenario():
        t = await create(mgr, git_repo)
        await finished(mgr, t["id"])
        # pretend the first turn ended 45 minutes ago
        with mgr.db._lock, mgr.db._conn:
            mgr.db._conn.execute("UPDATE turns SET finished_at = '2000-01-01T00:00:00Z' WHERE task_id = ?", (t["id"],))
        await turn(mgr, t["id"])
        assert any("idle for" in c for c in events(mgr, t["id"], "cache_miss")[-1]["data"]["possible_causes"])
        assert mgr.db.list_turns(t["id"])[1]["idle_before_seconds"] > 30 * 60
        await mgr.shutdown()
    go(scenario())


# ------------------------------------------------------------------ compaction monitor

def test_compactions_are_counted_and_frequent_ones_warned(git_repo, mgr, fake_codex_state):
    auto = [{"a": "item", "item": {"type": "contextCompaction", "id": "c"}}, use(5000, 4000, 0)]

    async def scenario():
        script(fake_codex_state, [use(5000, 0, 5000)], auto)
        t = await create(mgr, git_repo)
        await finished(mgr, t["id"])
        assert mgr.present_task(mgr.get(t["id"]))["ctx"]["compaction"]["count"] == 0
        await turn(mgr, t["id"])                                  # Codex compacted by itself during this turn
        assert mgr.get(t["id"])["compactions"] == 1
        assert mgr.db.list_turns(t["id"])[1]["compactions"] == 1
        c = mgr.present_task(mgr.get(t["id"]))["ctx"]["compaction"]
        assert c["count"] == 1 and not c["frequent"] and c["warning"] is None
        await mgr.compact(t["id"])                                # a second one, requested by the user
        await finished(mgr, t["id"])
        c = mgr.present_task(mgr.get(t["id"]))["ctx"]["compaction"]
        assert c["count"] == 2 and c["frequent"]
        assert c["warning"] == "Frequent compaction can reduce cache reuse and cause files to be re-read"
        assert len(events(mgr, t["id"], "frequent_compaction")) == 1
        assert [e["data"]["source"] for e in events(mgr, t["id"], "compaction")] == ["auto", "manual"]
        await mgr.shutdown()
    go(scenario())


def test_nothing_compacts_by_itself(git_repo, mgr, fake_codex_state):
    """Even at 99% of the window the GUI only warns; it never starts a compaction."""
    script(fake_codex_state, [use(250_000, 0, 0, 10, context=256_000)])

    async def scenario():
        t = await create(mgr, git_repo)
        await finished(mgr, t["id"])
        await asyncio.sleep(1.0)
        assert calls(fake_codex_state, "thread/compact/start") == [] and len(calls(fake_codex_state, "turn/start")) == 1
        await mgr.shutdown()
    go(scenario())


# ------------------------------------------------------------------ long-context zone

@pytest.mark.parametrize("context,zone", [(100_000, "normal"), (221_000, "warning"), (255_000, "strong")])
def test_zone_comes_from_the_request_context_size(git_repo, mgr, fake_codex_state, context, zone):
    script(fake_codex_state, [use(200_000, 0, 0, 10, context=context)])

    async def scenario():
        t = await create(mgr, git_repo)
        await finished(mgr, t["id"])
        z = mgr.present_task(mgr.get(t["id"]))["ctx"]["context_zone"]
        assert z["zone"] == zone and z["tokens"] == context and z["threshold"] == 272_000
        assert bool(events(mgr, t["id"], "long_context")) == (zone != "normal")
        await mgr.shutdown()
    go(scenario())


def test_accumulated_usage_is_never_taken_for_the_context(git_repo, mgr, fake_codex_state):
    """Six turns of ~200K tokens each is 1.2M tokens of usage, but every request context stays small."""
    script(fake_codex_state, *[[use(200_000, 190_000, 0, 10, context=90_000)] for _ in range(6)])

    async def scenario():
        t = await create(mgr, git_repo)
        await finished(mgr, t["id"])
        for _ in range(5):
            await turn(mgr, t["id"])
        total = json.loads(mgr.db.list_turns(t["id"])[-1]["total_json"])["input_tokens"]
        assert total == 1_200_000
        assert mgr.present_task(mgr.get(t["id"]))["ctx"]["context_zone"]["zone"] == "normal"
        assert events(mgr, t["id"], "long_context") == []
        await mgr.shutdown()
    go(scenario())


def test_long_context_zone_and_the_continue_choice(git_repo, mgr, fake_codex_state):
    script(fake_codex_state, [use(273_000, 0, 0, 10, context=273_010, window=900_000)])

    async def scenario():
        t = await create(mgr, git_repo)
        await finished(mgr, t["id"])
        z = mgr.present_task(mgr.get(t["id"]))["ctx"]["context_zone"]
        assert z["zone"] == "long" and "GPT-6.1 Sol long-context pricing zone" in z["message"]
        assert z["actions"] == ["continue", "compact", "new_session"] and not z.get("acknowledged")
        mgr.acknowledge_long_context(t["id"])
        assert mgr.present_task(mgr.get(t["id"]))["ctx"]["context_zone"]["acknowledged"] is True
        assert calls(fake_codex_state, "thread/compact/start") == []     # [Continue] changes nothing
        # a request over 272K is priced at the long-context rates ONLY because its size was reported
        eff = mgr.usage(t["id"])["efficiency"]
        assert eff["long_context_requests"] == 1
        await mgr.shutdown()
    go(scenario())


def test_acknowledging_needs_a_warning_zone(git_repo, mgr, fake_codex_state):
    async def scenario():
        t = await create(mgr, git_repo)
        await finished(mgr, t["id"])
        with pytest.raises(TaskError):
            mgr.acknowledge_long_context(t["id"])
        await mgr.shutdown()
    go(scenario())


# ------------------------------------------------------------------ tool outputs

def cmd(output, exit_code=0, command="rg foo"):
    return {"a": "item", "item": {"type": "commandExecution", "id": "c", "command": command, "aggregatedOutput": output, "exitCode": exit_code,
                                  "status": "completed"}}


def test_large_tool_outputs_are_recorded_and_a_context_jump_warned(git_repo, mgr, fake_codex_state):
    script(fake_codex_state, [use(10_000, 0, 0, 10), cmd("x" * 20_000), cmd("y" * 120_000, command="cat huge.log"), cmd("small"),
                              use(48_000, 10_000, 0, 10)])

    async def scenario():
        t = await create(mgr, git_repo, tool_output="conservative")
        await finished(mgr, t["id"])
        row = mgr.db.list_turns(t["id"])[0]
        assert row["tool_calls"] == 3 and row["large_tool_outputs"] == 1
        # raw 5,000 / 30,000 tokens; the model saw at most the cap (8000 for Conservative, below the model's 10000)
        assert row["tool_output_tokens_est"] == 5_000 + 8_000 + 2
        out = events(mgr, t["id"], "large_tool_output")
        assert len(out) == 1 and out[0]["severity"] == "warning" and out[0]["data"]["raw_tokens_est"] == 30_000
        assert out[0]["data"]["model_tokens_est"] == 8_000 and out[0]["data"]["truncated_for_model"] is True
        jump = events(mgr, t["id"], "context_jump")
        assert len(jump) == 1 and jump[0]["data"]["grew_tokens"] == 38_000 and "cat huge.log" in jump[0]["message"]
        view = mgr.present_task(mgr.get(t["id"]))["ctx"]
        assert [e["data"]["label"] for e in view["large_tool_outputs"]] == ["cat huge.log"]
        assert view["settings"]["tool_output"]["limit"] == 8000 and view["settings"]["tool_output"]["effective"] is True
        await mgr.shutdown()
    go(scenario())


def test_a_balanced_limit_is_shown_as_having_no_effect(git_repo, mgr, fake_codex_state):
    async def scenario():
        t = await create(mgr, git_repo, tool_output="balanced")
        await finished(mgr, t["id"])
        s = mgr.present_task(mgr.get(t["id"]))["ctx"]["settings"]["tool_output"]
        assert s["limit"] == 16000 and s["effective"] is False and "no effect" in s["note"] and s["cap"] == 10000
        await mgr.shutdown()
    go(scenario())


# ------------------------------------------------------------------ retry guard

def err(kind, message="x"):
    return {"message": message, "codexErrorInfo": kind, "additionalDetails": None}


def test_context_overflow_is_not_retried_and_blocks_a_plain_resend(git_repo, mgr, fake_codex_state):
    script(fake_codex_state, [use(1000), {"a": "error", "error": err("contextWindowExceeded"), "willRetry": True}, {"a": "wait_interrupt", "max": 10}])

    async def scenario():
        t = await create(mgr, git_repo, auto_retry=True)
        done = await finished(mgr, t["id"])
        assert done["status"] == "failed" and done["stop_reason"] == "context_window_exceeded" and done["failure_source"] == "guard"
        await asyncio.sleep(1.2)                                   # no automatic retry shows up later either
        assert mgr.get(t["id"])["status"] == "failed" and len(calls(fake_codex_state, "turn/start")) == 1
        assert len(mgr.attempts(t["id"])) == 1
        view = mgr.present_task(mgr.get(t["id"]))["ctx"]["stop"]
        assert view["blocks_resend"] and "Compact the thread or Start New Session" in view["message"]
        with pytest.raises(TaskError) as e:                        # the same huge context must not be sent again
            await mgr.send_instruction(t["id"], "try again")
        assert e.value.code == "context_overflow" and e.value.status == 409
        assert len(calls(fake_codex_state, "turn/start")) == 1
        assert [x["data"]["reason"] for x in events(mgr, t["id"], "retry_guard")][0] == "context_window_exceeded"
        # the way out is the user's choice: compacting clears the block
        await mgr.compact(t["id"])
        await finished(mgr, t["id"])
        assert mgr.get(t["id"])["stop_reason"] == ""
        await turn(mgr, t["id"])
        assert len(calls(fake_codex_state, "turn/start")) == 2
        await mgr.shutdown()
    go(scenario())


def test_context_overflow_that_ends_the_turn_by_itself_is_not_retried_either(git_repo, mgr, fake_codex_state):
    script(fake_codex_state, [use(1000), {"a": "error", "error": err("contextWindowExceeded"), "willRetry": False},
                              {"a": "complete", "status": "failed", "error": err("contextWindowExceeded")}])

    async def scenario():
        t = await create(mgr, git_repo, auto_retry=True)
        done = await finished(mgr, t["id"])
        assert done["status"] == "failed" and done["stop_reason"] == "context_window_exceeded"
        await asyncio.sleep(1.2)
        assert len(calls(fake_codex_state, "turn/start")) == 1 and mgr.get(t["id"])["status"] == "failed"
        with pytest.raises(TaskError) as e:
            await mgr.send_instruction(t["id"], "again")
        assert e.value.code == "context_overflow"
        await mgr.shutdown()
    go(scenario())


def test_start_new_session_is_the_other_way_out(git_repo, mgr, fake_codex_state):
    script(fake_codex_state, [{"a": "complete", "status": "failed", "error": err("contextWindowExceeded")}])

    async def scenario():
        t = await create(mgr, git_repo, auto_retry=False)
        await finished(mgr, t["id"])
        assert mgr.get(t["id"])["stop_reason"] == "context_window_exceeded"
        await mgr.start_new_session(t["id"], "fresh start")
        done = await finished(mgr, t["id"])
        assert done["status"] == "completed" and done["stop_reason"] == "" and len(calls(fake_codex_state, "thread/start")) == 2
        await mgr.shutdown()
    go(scenario())


def test_authentication_error_that_would_loop_is_stopped(git_repo, mgr, fake_codex_state):
    script(fake_codex_state, [{"a": "error", "error": err("unauthorized"), "willRetry": True}, {"a": "wait_interrupt", "max": 10}])

    async def scenario():
        t = await create(mgr, git_repo, auto_retry=True)
        done = await finished(mgr, t["id"])
        assert done["status"] == "failed" and done["stop_reason"] == "authentication_error"
        assert "codex login" in mgr.present_task(done)["ctx"]["stop"]["message"]
        assert not mgr.present_task(done)["ctx"]["stop"]["blocks_resend"]    # the user may try again after logging in
        await asyncio.sleep(1.0)
        assert len(calls(fake_codex_state, "turn/start")) == 1
        await mgr.shutdown()
    go(scenario())


def test_quota_exhaustion_waits_instead_of_retrying(git_repo, mgr, fake_codex_state):
    script(fake_codex_state, [{"a": "error", "error": err("usageLimitExceeded", "limit"), "willRetry": True}, {"a": "wait_interrupt", "max": 10}])

    async def scenario():
        t = await create(mgr, git_repo, auto_retry=True)
        done = await finished(mgr, t["id"])
        assert done["status"] in ("failed", "waiting-for-quota") and done["stop_reason"] == "quota_exhausted"
        await asyncio.sleep(1.0)
        assert len(calls(fake_codex_state, "turn/start")) == 1
        await mgr.shutdown()
    go(scenario())


def test_the_same_tool_failure_over_and_over_stops_the_turn(git_repo, mgr, fake_codex_state):
    bad = cmd("make: *** No rule to make target", exit_code=2, command="make test")
    script(fake_codex_state, [use(1000), bad, bad, bad, {"a": "wait_interrupt", "max": 10}])

    async def scenario():
        t = await create(mgr, git_repo, auto_retry=True)
        done = await finished(mgr, t["id"])
        assert done["status"] == "failed" and done["stop_reason"] == "repeated_tool_failure"
        assert "make test" in events(mgr, t["id"], "retry_guard")[0]["message"]
        await asyncio.sleep(1.0)
        assert len(calls(fake_codex_state, "turn/start")) == 1                  # and it is not started again
        await mgr.shutdown()
    go(scenario())


def test_different_or_interleaved_failures_do_not_trip_the_guard(git_repo, mgr, fake_codex_state):
    a = cmd("error A", 1, "make test")
    ok = cmd("fine", 0, "make test")
    script(fake_codex_state, [use(1000), a, a, ok, a, a, cmd("error B", 1, "make test"), a, use(1100)])

    async def scenario():
        t = await create(mgr, git_repo)
        done = await finished(mgr, t["id"])
        assert done["status"] == "completed" and done["stop_reason"] == ""
        await mgr.shutdown()
    go(scenario())


def test_the_repeat_limit_is_configurable(git_repo, mgr, fake_codex_state):
    bad = cmd("nope", 1, "false")
    mgr.ctx.set_thresholds({"repeat_failure_limit": 2})
    script(fake_codex_state, [use(1000), bad, bad, {"a": "wait_interrupt", "max": 10}])

    async def scenario():
        t = await create(mgr, git_repo)
        done = await finished(mgr, t["id"])
        assert done["stop_reason"] == "repeated_tool_failure"
        await mgr.shutdown()
    go(scenario())


# ------------------------------------------------------------------ cache age: never kept warm artificially

def test_no_turn_is_ever_started_to_keep_the_cache_warm(git_repo, mgr, fake_codex_state):
    async def scenario():
        t = await create(mgr, git_repo)
        await finished(mgr, t["id"])
        # even if the cache looks cold, nothing is sent by itself
        with mgr.db._lock, mgr.db._conn:
            mgr.db._conn.execute("UPDATE tasks SET last_cache_activity_at = '2000-01-01T00:00:00Z' WHERE id = ?", (t["id"],))
        assert mgr.present_task(mgr.get(t["id"]))["ctx"]["cache_age"]["state"] == "cold"
        mgr.tick()
        await asyncio.sleep(1.5)
        assert len(calls(fake_codex_state, "turn/start")) == 1 and mgr.get(t["id"])["status"] == "completed"
        await mgr.shutdown()
    go(scenario())


def test_the_code_has_no_keepalive_path():
    """Static check: no function or attribute named like a keep-alive, and cache_health never imports anything that can send."""
    root = Path(__file__).parent.parent / "app"
    names = set()
    for f in root.glob("*.py"):
        for node in ast.walk(ast.parse(f.read_text())):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name.lower())
            elif isinstance(node, ast.Attribute):
                names.add(node.attr.lower())
    assert not [n for n in names if "keepalive" in n or "keep_alive" in n or "keepwarm" in n or "keep_warm" in n or "warm_cache" in n]
    imports = {n.module for n in ast.walk(ast.parse((root / "cache_health.py").read_text())) if isinstance(n, ast.ImportFrom)}
    assert not imports & {"asyncio", "subprocess", "app.appserver", ".appserver", "appserver"}


def test_concurrent_profile_checks_share_one_measurement(settings, db, monkeypatch):
    """Twenty tasks created at once must not start forty `codex` probes."""
    from app import tool_probe
    from app.ctx_manager import ContextFeatures
    runs = []

    async def fake_catalog(codex_bin, cwd, overrides=None, **kw):
        runs.append(overrides)
        await asyncio.sleep(0.05)
        n = 187 if not overrides else 8
        return {"ok": True, "error": "", "nested_count": n, "catalog_bytes": 26_000 if not overrides else 21_000, "groups": {"builtin": n}}
    monkeypatch.setattr(tool_probe, "catalog", fake_catalog)

    async def scenario():
        cf = ContextFeatures(db, settings)
        res = await asyncio.gather(*[cf.verify_profile("/repo", "development", []) for _ in range(20)])
        assert all(r["verified"] and r["full_count"] == 187 and r["profile_count"] == 8 for r in res)
        assert len(runs) == 2                                    # one baseline + one profile measurement
        await cf.verify_profile("/repo", "minimal", [])           # another profile reuses the baseline
        assert len(runs) == 3
        again = await cf.verify_profile("/repo", "development", [])
        assert len(runs) == 3 and again["verified"]
        full = await cf.verify_profile("/repo", "full", [])
        assert full["verified"] is False and "nothing to verify" in full["note"]
    asyncio.run(scenario())


def test_a_failed_measurement_is_never_called_verified(settings, db, monkeypatch):
    from app import tool_probe
    from app.ctx_manager import ContextFeatures

    async def broken(codex_bin, cwd, overrides=None, **kw):
        return {"ok": False, "error": "codex exploded", "nested_count": None, "catalog_bytes": None, "groups": {}}
    monkeypatch.setattr(tool_probe, "catalog", broken)
    r = asyncio.run(ContextFeatures(db, settings).verify_profile("/repo", "minimal", []))
    assert r["ok"] is False and r["verified"] is False and "codex exploded" in r["error"]


def test_a_profile_without_a_reduction_is_not_marked_optimized(settings, db, monkeypatch):
    from app import tool_probe
    from app.ctx_manager import ContextFeatures

    async def same(codex_bin, cwd, overrides=None, **kw):
        return {"ok": True, "error": "", "nested_count": 8, "catalog_bytes": 21_611, "groups": {"builtin": 8}}
    monkeypatch.setattr(tool_probe, "catalog", same)
    r = asyncio.run(ContextFeatures(db, settings).verify_profile("/repo", "development", []))
    assert r["ok"] and r["verified"] is False and r["reduced"] is False and "NOT marked optimized" in r["note"]
