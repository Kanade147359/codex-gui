"""End to end with the REAL codex app-server: GUI TaskManager -> `codex app-server` -> a local fake Responses endpoint.

No model is called and no quota is used. It checks what the unit tests with a fake app-server cannot: that real Codex
reports cache writes and tool items the way the GUI parses them, that the config the GUI sends is really honoured
(nested agents, tool-output limit, working directory), that the speed requested per turn reaches the request, and that a
tool profile is only called verified when a measurement shows a reduction."""
import asyncio
import json
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from app.fake_responses import MockResponses, tool_output_of
from app.task_manager import TaskManager
from app.tool_probe import provider_overrides

from conftest import wait_for
from mock_responses import codex_home

pytestmark = pytest.mark.skipif(shutil.which("codex") is None, reason="codex is not installed")
IDLE = ("queued", "starting", "running")
FAKE_MCP = str(Path(__file__).parent / "fake_mcp_server.py")
BIG = 'const r = await tools.exec_command({cmd: "seq 1 400000"}); text(r.output);'
SPAWN = ("call", "spawn_agent", {"task_name": "child", "message": "x", "fork_turns": "none"}, "collaboration")


@pytest.fixture
def stack(tmp_path, settings, db, monkeypatch):
    """A TaskManager on the real codex app-server whose model is the fake endpoint. Returns (manager, mock, make_task)."""
    mock = MockResponses().start()
    home = codex_home(tmp_path / "codexhome", f'''
[mcp_servers.alpha]
command = "{sys.executable}"
args = ["{FAKE_MCP}", "alpha", "5"]
''')
    flags = " ".join(f"'{a}'" for a in provider_overrides(mock.base_url))
    wrapper = tmp_path / "codex-fake-model"
    wrapper.write_text(f'#!/bin/sh\nif [ "$1" = "app-server" ]; then shift; exec codex app-server {flags} "$@"; fi\nexec codex "$@"\n')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("CODEX_HOME", str(home))
    settings.backend, settings.codex_bin, settings.subscription_only = "app-server", str(wrapper), False
    manager = TaskManager(settings, db)

    async def make(repo, prompt="go", **kw):
        return await manager.create_task(repository=str(repo), prompt=prompt, name="e2e", model="gpt-6.1-sol", **kw)

    yield manager, mock, make
    mock.stop()


async def finished(m, task_id, timeout=60.0):
    return await wait_for(lambda: m.get(task_id)["status"] not in IDLE and m.get(task_id), timeout)


def go(coro):
    return asyncio.run(coro)


def thread_requests(mock, thread_prefix=None):
    return mock.requests


def test_real_codex_cache_write_tool_calls_and_large_outputs_are_recorded(git_repo, stack):
    m, mock, make = stack
    mock.script = [("exec", BIG, {"input": 30_000, "cached": 0, "write": 30_000, "output": 40}),
                   ("text", "done", {"input": 45_000, "cached": 30_000, "write": 15_000, "output": 10})]

    async def scenario():
        t = await make(git_repo)
        done = await finished(m, t["id"])
        assert done["status"] == "completed", done["status_detail"]
        row = m.db.list_turns(t["id"])[0]
        assert (row["input_tokens"], row["cached_input_tokens"], row["cache_write_input_tokens"], row["output_tokens"]) == (75_000, 30_000, 45_000, 50)
        assert row["requests"] == 2 and row["max_request_input"] == 45_000
        assert json.loads(row["requests_json"]) == [{"i": 30000, "c": 0, "w": 30000, "o": 40}, {"i": 45000, "c": 30000, "w": 15000, "o": 10}]
        assert row["tool_calls"] >= 1 and row["large_tool_outputs"] == 1          # the seq output is far above 8k tokens
        assert row["tool_output_tokens_est"] <= 10_100                             # what the model saw is capped
        big = [e for e in m.db.list_context_events(t["id"], ["large_tool_output"])]
        assert len(big) == 1 and big[0]["data"]["raw_tokens_est"] > 100_000 and big[0]["data"]["truncated_for_model"]
        assert row["service_tier"] == "default" and done["last_cache_activity_at"]
        eff = m.usage(t["id"])["efficiency"]
        assert eff["cache_write_input_tokens"] == 45_000 and eff["total"]["usd"]["cache_write_cost"] == pytest.approx(45_000 * 2.5 / 1e6)
        await m.shutdown()
    go(scenario())


def test_real_codex_honours_the_speed_requested_per_turn(git_repo, stack):
    m, mock, make = stack

    async def scenario():
        t = await make(git_repo)
        await finished(m, t["id"])
        await m.send_instruction(t["id"], "fast one", service_tier="fast")
        await finished(m, t["id"])
        await m.send_instruction(t["id"], "standard again", service_tier="standard")
        await finished(m, t["id"])
        assert [r.get("service_tier") for r in mock.requests] == [None, "priority", None]   # Standard sends none; Fast sends priority
        assert [r["service_tier"] for r in m.db.list_turns(t["id"])] == ["default", "priority", "default"]
        assert len({r["prompt_cache_key"] for r in mock.requests}) == 1                      # one thread throughout
        await m.shutdown()
    go(scenario())


def test_real_codex_nested_agents_off_by_default_and_on_when_allowed(git_repo, stack):
    m, mock, make = stack

    async def spawn_output(**kw):
        mock.requests.clear()
        mock.script = [SPAWN, ("text", "done")]
        t = await make(git_repo, **kw)
        await finished(m, t["id"])
        return tool_output_of(mock.requests[1])

    async def scenario():
        assert "thread limit reached" in await spawn_output()                 # default: Codex cannot spawn
        assert "/root/child" in await spawn_output(allow_subagents=True)     # explicitly allowed for this task
        await m.shutdown()
    go(scenario())


def test_real_codex_tool_output_limit_reaches_the_model(git_repo, stack):
    m, mock, make = stack

    async def seen(**kw):
        mock.requests.clear()
        mock.script = [("exec", BIG), ("text", "ok")]
        t = await make(git_repo, **kw)
        await finished(m, t["id"])
        return len(tool_output_of(mock.requests[1]))

    async def scenario():
        default, balanced, conservative = await seen(), await seen(tool_output="balanced"), await seen(tool_output="conservative")
        assert balanced == default                 # Balanced (16000) is above the model's own cap: no effect
        assert conservative < default              # Conservative (8000) really lowers what the model gets back
        await m.shutdown()
    go(scenario())


def test_real_codex_runs_in_the_custom_subdirectory(git_repo, stack):
    m, mock, make = stack
    (git_repo / "pkg").mkdir()
    (git_repo / "pkg" / "x.txt").write_text("x")
    (git_repo / "AGENTS.md").write_text("ROOT_RULES_MARKER\n")
    (git_repo / "pkg" / "AGENTS.md").write_text("PKG_RULES_MARKER\n")
    subprocess.run(["git", "-C", str(git_repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(git_repo), "commit", "-q", "-m", "pkg"], check=True)

    async def scenario():
        t = await make(git_repo, cwd_subdir="pkg")
        await finished(m, t["id"])
        text = json.dumps(mock.requests[0]["input"])
        wt = m.get(t["id"])["worktree"]
        assert f"{wt}/pkg" in text and "ROOT_RULES_MARKER" in text and "PKG_RULES_MARKER" in text   # the chain follows the cwd
        view = await m.context_preview(str(git_repo), "pkg")
        assert [f["relative"] for f in view["agents"]["files"] if f["scope"] == "project"] == ["AGENTS.md", "pkg/AGENTS.md"]
        await m.shutdown()
    go(scenario())


def test_real_codex_profile_is_verified_only_when_tools_really_drop(git_repo, stack):
    m, mock, make = stack

    async def scenario():
        mini = await make(git_repo, tool_profile="minimal")
        dev = await make(git_repo, tool_profile="development")
        await finished(m, mini["id"]), await finished(m, dev["id"])
        await asyncio.gather(*m._side_jobs)
        a, b = json.loads(m.get(mini["id"])["tool_profile_check"]), json.loads(m.get(dev["id"])["tool_profile_check"])
        # Minimal disables the configured MCP server (alpha: 5 tools + 3 MCP resource helpers) -> a measured reduction
        assert a["ok"] and a["verified"] and a["full_count"] - a["profile_count"] == 8 and a["profile_groups"] == {"builtin": 8}
        # Development changes nothing in this isolated home (no ChatGPT apps / plugins): NOT marked optimized
        assert b["ok"] and b["verified"] is False and "No reduction" in b["note"]
        await m.shutdown()
    go(scenario())
