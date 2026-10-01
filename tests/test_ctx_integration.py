"""Integration tests with the REAL codex binary and no model: Codex talks to a local fake Responses endpoint, so what is
asserted is what Codex really sends to the model / hands back from tools. No quota is used; ~/.codex is not modified
(an isolated CODEX_HOME for most tests; the real one only through `--ephemeral`, read-only in effect)."""
import asyncio
import json
import shutil
import sys
from pathlib import Path

import pytest

from app import ctx_config as cc
from app import tool_probe as tp
from app.appserver import AppServerClient
from app.codex_runner import nested
from app.fake_responses import MockResponses, tool_output_of

pytestmark = pytest.mark.skipif(shutil.which("codex") is None, reason="codex is not installed")

FAKE_MCP = str(Path(__file__).parent / "fake_mcp_server.py")
MCP_TOML = f'''
[mcp_servers.alpha]
command = "{sys.executable}"
args = ["{FAKE_MCP}", "alpha", "5"]
[mcp_servers.beta]
command = "{sys.executable}"
args = ["{FAKE_MCP}", "beta", "3"]
'''


def make_home(path: Path, toml: str = "") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    cache = Path("~/.codex/models_cache.json").expanduser()
    if cache.exists():  # the real metadata: truncation policy 10000, multi_agent_version v2, ...
        (path / "models_cache.json").write_bytes(cache.read_bytes())
    (path / "config.toml").write_text('model = "gpt-6.1-sol"\n' + toml)
    return path


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    import subprocess
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    return r


@pytest.fixture
def home(tmp_path):
    return make_home(tmp_path / "codexhome")


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- tool output

def test_tool_output_token_limit_only_lowers_the_cap(repo, home):
    """Measured facts behind the presets: 16000 / 32000 change nothing, 8000 trims a little, smaller values trim more."""
    sizes = {lim: run(tp.measure_tool_output("codex", repo, lim, codex_home=home))["tokens_est"]
             for lim in (None, 2000, 4000, 8000, 16000, 32000)}
    assert all(sizes.values())
    assert sizes[16000] == sizes[None] and sizes[32000] == sizes[None]   # no-ops on this model
    assert sizes[8000] < sizes[None]                                     # Conservative really does reduce
    assert sizes[2000] < sizes[4000] < sizes[8000] < sizes[None]
    assert sizes[None] <= 10_100                                         # the model's own cap is ~10000 tokens


def test_model_cannot_ask_for_more_than_the_cap(repo, home):
    js = 'const r = await tools.exec_command({cmd: "seq 1 400000", max_output_tokens: 60000}); text(r.output);'
    res = run(tp.run_probe("codex", repo, {"tool_output_token_limit": 32000}, [("exec", js), ("text", "ok")], codex_home=home))
    assert tp.est_tokens(tool_output_of(res.requests[1])) <= 10_100


def test_tool_output_effect_matches_the_measurement():
    assert cc.tool_output_effect(16000, 10000)["effective"] is False
    assert cc.tool_output_effect(8000, 10000)["effective"] is True
    assert cc.tool_output_effect(None, 10000)["note"] == "Codex default"


# ---------------------------------------------------------------- nested agents

SPAWN = [("call", "spawn_agent", {"task_name": "child", "message": "do it", "fork_turns": "none"}, "collaboration"), ("text", "done")]


def spawn_result(repo, home, overrides):
    res = run(tp.run_probe("codex", repo, overrides, list(SPAWN), codex_home=home))
    assert res.ok, res.error
    return tool_output_of(res.requests[1])


def test_default_allows_spawn_and_multi_agent_false_alone_does_not_stop_it(repo, home):
    assert "/root/child" in spawn_result(repo, home, None)
    # the documented-looking switch is not effective for gpt-6.1-sol (multi_agent_version v2)
    assert "/root/child" in spawn_result(repo, home, {"features.multi_agent": False})


def test_subagents_off_overrides_really_block_spawn(repo, home):
    out = spawn_result(repo, home, cc.SUBAGENTS_OFF)
    assert "thread limit reached" in out and "/root/child" not in out


def test_subagents_off_does_not_break_a_plain_turn(repo, home):
    res = run(tp.run_probe("codex", repo, cc.SUBAGENTS_OFF, [("text", "fine")], codex_home=home))
    assert res.ok and len(res.requests) == 1


class AppServerWithProbeProvider(AppServerClient):
    def __init__(self, base_url, home):
        super().__init__("codex", False)
        self._extra = tp.provider_overrides(base_url)
        self._home = home

    def build_command(self):
        return [self.codex_bin, "app-server", *self._extra]


def test_subagents_off_through_the_app_server_thread_config(repo, home, monkeypatch):
    """The GUI sends these keys as the `config` object of thread/start, not as -c flags: same effect?"""
    monkeypatch.setenv("CODEX_HOME", str(home))

    async def main(overrides):
        with MockResponses(list(SPAWN)) as mock:
            client = AppServerWithProbeProvider(mock.base_url, home)
            try:
                res = await client.request("thread/start", {
                    "cwd": str(repo), "approvalPolicy": "never", "sandbox": "read-only", "config": nested(overrides)})
                tid = res["thread"]["id"]
                q = client.subscribe(tid)
                await client.request("turn/start", {"threadId": tid, "input": [{"type": "text", "text": "go"}]})
                while True:
                    method, _ = await asyncio.wait_for(q.get(), 60)
                    if method == "turn/completed":
                        break
                return tool_output_of(mock.requests[1])
            finally:
                await client.close()

    assert "/root/child" in run(main({}))
    out = run(main(cc.SUBAGENTS_OFF))
    assert "thread limit reached" in out and "/root/child" not in out


# ---------------------------------------------------------------- MCP / tool profile

def test_disabling_mcp_servers_and_enabled_tools_reduce_the_tools_the_model_can_reach(repo, tmp_path):
    home = make_home(tmp_path / "mcphome", MCP_TOML)

    def cat(overrides):
        r = run(tp.catalog("codex", repo, overrides, codex_home=home))
        assert r["ok"], r["error"]
        return r

    full = cat(None)
    assert full["groups"].get("alpha") == 5 and full["groups"].get("beta") == 3
    one_off = cat({"mcp_servers.alpha.enabled": False})
    assert "alpha" not in one_off["groups"] and one_off["groups"]["beta"] == 3
    minimal = cat(cc.profile_config("minimal", ["alpha", "beta"]))
    assert minimal["groups"] == {"builtin": 8} and minimal["nested_count"] == 8
    assert full["nested_count"] - minimal["nested_count"] == 11            # 8 MCP tools + 3 MCP resource helpers
    assert minimal["catalog_bytes"] < full["catalog_bytes"]               # the model-visible declarations shrink too
    limited = cat({"mcp_servers.alpha.enabled_tools": ["alpha_1", "alpha_2"]})
    assert limited["groups"]["alpha"] == 2
    dev = cat(cc.profile_config("development", ["alpha", "beta"]))        # MCP stays in Development
    assert dev["groups"].get("alpha") == 5 and dev["nested_count"] == full["nested_count"]


def test_disabling_an_undefined_mcp_server_breaks_codex_so_only_real_names_are_used(repo, home):
    """Why mcp_server_names() must come from the effective config: `enabled=false` on a name that is not defined is an error."""
    res = run(tp.catalog("codex", repo, {"mcp_servers.not_defined_anywhere.enabled": False}, codex_home=home))
    assert not res["ok"] and "invalid transport" in res["error"] + str(res)


def test_profile_on_the_real_config_never_adds_tools_and_removes_apps(repo):
    """Uses the real ~/.codex read-only (ephemeral, fake endpoint). Strict reduction only if the user has ChatGPT apps."""
    full = run(tp.catalog("codex", repo, None))
    if not full["ok"]:
        pytest.skip(full["error"])
    dev = run(tp.catalog("codex", repo, cc.profile_config("development")))
    mini = run(tp.catalog("codex", repo, cc.profile_config("minimal", cc.mcp_server_names(None))))
    assert dev["ok"] and mini["ok"]
    assert mini["nested_count"] <= dev["nested_count"] <= full["nested_count"]
    if full["groups"].get("codex_apps"):
        assert "codex_apps" not in dev["groups"] and dev["nested_count"] < full["nested_count"]
        assert dev["catalog_bytes"] < full["catalog_bytes"]


# ---------------------------------------------------------------- skills catalog budget

def write_skills(home: Path, n: int) -> None:
    for i in range(n):
        d = home / "skills" / f"skill-{i:02d}"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: skill-{i:02d}\ndescription: {'Use this skill when working with topic number %d, ' % i * 12}\n---\n# Skill {i}\nBody.\n")


def test_skills_budget_bounds_the_catalog(repo, tmp_path):
    home = make_home(tmp_path / "skillhome")
    write_skills(home, 40)

    def stats(budget):
        ov = {"skills.max_context_tokens": budget} if budget else None
        s = tp.skills_catalog_stats(run(tp.prompt_input("codex", repo, ov, codex_home=home)))
        assert s, "no skills block in the prompt"
        return s

    default, eco, big = stats(None), stats(2000), stats(8000)
    assert default["skills"] >= 40 and default["tokens_est"] > 4000   # the catalog really is large here
    assert eco["tokens_est"] < default["tokens_est"]                  # the budget bites ...
    assert eco["tokens_est"] <= 2000 * 1.5                            # ... and keeps the catalog near the budget
    assert stats(100)["tokens_est"] < eco["tokens_est"]
    # Codex's own default budget (~5.4k tokens here) is BELOW the 8000 "Large" preset: Large can only add tokens.
    assert eco["tokens_est"] < stats(4000)["tokens_est"] < default["tokens_est"] < big["tokens_est"]


def test_skills_budget_is_not_binding_for_a_small_catalog(repo, tmp_path):
    home = make_home(tmp_path / "smallhome")
    write_skills(home, 2)
    a = tp.skills_catalog_stats(run(tp.prompt_input("codex", repo, None, codex_home=home)))
    b = tp.skills_catalog_stats(run(tp.prompt_input("codex", repo, {"skills.max_context_tokens": 2000}, codex_home=home)))
    assert a["chars"] == b["chars"]


def test_probes_and_audits_leave_the_users_codex_home_alone(repo):
    """The real ~/.codex/config.toml is never edited by what the GUI does for Context Efficiency (probe, preview)."""
    import hashlib
    cfg = Path("~/.codex/config.toml").expanduser()
    if not cfg.exists():
        pytest.skip("no ~/.codex/config.toml")
    digest = lambda: hashlib.sha256(cfg.read_bytes()).hexdigest()  # noqa: E731
    before = digest()
    run(tp.catalog("codex", repo, cc.profile_config("development")))
    run(tp.prompt_input("codex", repo, {"skills.max_context_tokens": 2000}))
    run(tp.measure_tool_output("codex", repo, 8000))
    assert digest() == before
