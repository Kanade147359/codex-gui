"""HTTP API of Context Efficiency (routes + JSON shapes the UI relies on), on the scriptable fake app-server."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

from conftest import FakeRunner
from test_ctx_manager import CATALOG, CtxFakeServer, script, use


@pytest.fixture
def client(settings, monkeypatch):
    settings.backend = "app-server"
    app = create_app(settings, FakeRunner(), CtxFakeServer("fake", False))
    with TestClient(app) as c:
        m = app.state.manager
        m.ctx._catalog._cached, m.ctx._catalog._fetched_at = CATALOG, time.monotonic() + 3600

        async def no_probe(*a, **k):
            return {"ok": True, "verified": True, "full_count": 187, "profile_count": 8, "full_bytes": 26215, "profile_bytes": 21611}
        m.ctx.verify_profile = no_probe
        c.manager = m
        yield c


def wait_done(client, task_id, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        t = client.get(f"/api/tasks/{task_id}").json()
        if t["status"] not in ("queued", "starting", "running"):
            return t
        time.sleep(0.1)
    raise AssertionError("task did not finish")


def test_options_expose_the_presets_as_gui_presets(client):
    ce = client.get("/api/options").json()["context_efficiency"]
    assert [(p["id"], p["limit"]) for p in ce["tool_output"]] == [("default", None), ("conservative", 8000), ("balanced", 16000), ("large", 32000)]
    assert [(p["id"], p["budget"]) for p in ce["skills"]] == [("default", None), ("economy", 2000), ("balanced", 4000), ("large", 8000)]
    assert [p["id"] for p in ce["tool_profiles"]] == ["full", "development", "minimal"]
    assert ce["default_allow_subagents"] is False and ce["default_tool_output"] == "default" and ce["default_tool_profile"] == "full"


def test_create_with_context_settings_and_read_them_back(client, git_repo, fake_codex_state):
    script(fake_codex_state, [use(5000, 0, 5000)])
    r = client.post("/api/tasks", json={"repository": str(git_repo), "prompt": "go", "tool_output": "conservative", "skills": "economy",
                                        "tool_profile": "development", "allow_subagents": False})
    assert r.status_code == 200, r.text
    t = wait_done(client, r.json()["id"])
    ctx = t["ctx"]
    assert ctx["settings"]["tool_output"]["limit"] == 8000 and ctx["settings"]["skills"]["budget"] == 2000
    assert ctx["settings"]["allow_subagents"] is False and ctx["settings"]["tool_profile"]["name"] == "development"
    assert ctx["settings"]["tool_profile"]["check"]["verified"] is True            # filled in by the background verification
    assert ctx["cache_age"]["state"] == "hot" and ctx["compaction"]["count"] == 0 and ctx["context_zone"]["zone"] in ("normal", "n/a")
    listing = client.get("/api/tasks").json()["tasks"][0]
    assert "ctx_zone" in listing and "compactions" in listing


@pytest.mark.parametrize("body", [{"tool_output": "x"}, {"skills": "x"}, {"tool_profile": "x"}, {"cwd_subdir": "../x"}, {"tool_output_limit": 1}])
def test_bad_context_settings_are_400(client, git_repo, body):
    r = client.post("/api/tasks", json={"repository": str(git_repo), "prompt": "go", **body})
    assert r.status_code == 400 and r.json()["detail"]["message"]


def test_send_standard_and_fast(client, git_repo, fake_codex_state):
    t = client.post("/api/tasks", json={"repository": str(git_repo), "prompt": "go"}).json()
    wait_done(client, t["id"])
    assert client.post(f"/api/tasks/{t['id']}/messages", json={"prompt": "again", "service_tier": "fast"}).status_code == 200
    wait_done(client, t["id"])
    assert client.post(f"/api/tasks/{t['id']}/messages", json={"prompt": "x", "service_tier": "no way!"}).status_code == 400
    usage = client.get(f"/api/tasks/{t['id']}/usage").json()
    assert [r["service_tier"] for r in usage["turns"]] == ["default", "priority"]


def test_tool_profile_change_needs_the_cache_warning_to_be_accepted(client, git_repo, fake_codex_state):
    t = client.post("/api/tasks", json={"repository": str(git_repo), "prompt": "go", "tool_profile": "development"}).json()
    wait_done(client, t["id"])
    r = client.post(f"/api/tasks/{t['id']}/tool-profile", json={"profile": "minimal"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "confirm_cache_loss"
    assert "may reduce prompt cache reuse" in r.json()["detail"]["message"]
    assert client.get(f"/api/tasks/{t['id']}").json()["tool_profile"] == "development"
    r = client.post(f"/api/tasks/{t['id']}/tool-profile", json={"profile": "minimal", "confirm": True})
    assert r.status_code == 200 and r.json()["tool_profile"] == "minimal"
    events = client.get(f"/api/tasks/{t['id']}/context-events", params={"kind": "tool_profile_change"}).json()["events"]
    assert len(events) == 1


def test_context_ack_and_events_endpoints(client, git_repo, fake_codex_state):
    script(fake_codex_state, [use(230_000, 0, 0, 10, context=230_010)])
    t = client.post("/api/tasks", json={"repository": str(git_repo), "prompt": "go"}).json()
    done = wait_done(client, t["id"])
    assert done["ctx"]["context_zone"]["zone"] == "warning" and not done["ctx"]["context_zone"].get("acknowledged")
    assert client.post(f"/api/tasks/{t['id']}/context-ack").status_code == 200
    assert client.get(f"/api/tasks/{t['id']}").json()["ctx"]["context_zone"]["acknowledged"] is True
    assert client.get("/api/tasks/nope/context-events").status_code == 404
    ev = client.get(f"/api/tasks/{t['id']}/context-events").json()["events"]
    assert sorted(e["kind"] for e in ev) == ["cache_miss", "long_context"]  # (the first turn of a thread is an info-level miss)


def test_context_overflow_blocks_the_resend_with_a_clear_409(client, git_repo, fake_codex_state):
    script(fake_codex_state, [{"a": "complete", "status": "failed",
                               "error": {"message": "x", "codexErrorInfo": "contextWindowExceeded", "additionalDetails": None}}])
    t = client.post("/api/tasks", json={"repository": str(git_repo), "prompt": "go", "auto_retry": True}).json()
    done = wait_done(client, t["id"])
    assert done["ctx"]["stop"]["reason"] == "context_window_exceeded" and done["ctx"]["stop"]["blocks_resend"]
    r = client.post(f"/api/tasks/{t['id']}/messages", json={"prompt": "again"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "context_overflow"
    assert client.post(f"/api/tasks/{t['id']}/compact").status_code == 200
    assert wait_done(client, t["id"])["stop_reason"] == ""


def test_thresholds_api(client):
    s = client.get("/api/context/settings").json()
    assert s["values"]["cache_miss_uncached_tokens"] == 10_000 and s["defaults"]["tool_output_warn_tokens"] == 8_000
    assert s["bounds"]["cache_recent_turns"] == [1, 50]
    r = client.put("/api/context/settings", json={"values": {"cache_miss_uncached_tokens": 12_000}})
    assert r.status_code == 200 and r.json()["values"]["cache_miss_uncached_tokens"] == 12_000
    assert client.get("/api/context/settings").json()["values"]["cache_miss_uncached_tokens"] == 12_000
    for bad in ({"nope": 1}, {"cache_miss_uncached_tokens": 3}, {"tool_output_warn_tokens": 30_000}):
        r = client.put("/api/context/settings", json={"values": bad})
        assert r.status_code == 400 and r.json()["detail"]["message"], bad
    assert client.get("/api/context/settings").json()["values"]["cache_miss_uncached_tokens"] == 12_000   # a bad PUT changes nothing
    assert client.delete("/api/context/settings").json()["values"]["cache_miss_uncached_tokens"] == 10_000


def test_agents_audit_endpoint_is_read_only_and_follows_the_task_cwd(client, git_repo, fake_codex_state):
    import hashlib
    import subprocess
    (git_repo / "AGENTS.md").write_text("Always read docs/a.md before every task.\n")
    subprocess.run(["git", "-C", str(git_repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(git_repo), "commit", "-q", "-m", "agents"], check=True)
    t = client.post("/api/tasks", json={"repository": str(git_repo), "prompt": "go"}).json()
    wait_done(client, t["id"])
    wt = client.get(f"/api/tasks/{t['id']}").json()["worktree"]
    digest = lambda: hashlib.sha256(open(f"{wt}/AGENTS.md", "rb").read()).hexdigest()  # noqa: E731
    before = digest()
    a = client.get(f"/api/tasks/{t['id']}/agents-audit").json()
    assert a["read_only"] and a["cwd"] == wt and a["budget"]["max_bytes"] == 32768
    assert {w["id"] for w in a["warnings"]} >= {"always_read", "every_task"}
    assert digest() == before
    # the budget is Codex's effective project_doc_max_bytes (config/read) when it reports one
    (fake_codex_state / "config.json").write_text(json.dumps({"project_doc_max_bytes": 20, "mcp_servers": {}}))
    a = client.get(f"/api/tasks/{t['id']}/agents-audit").json()
    assert a["budget"]["max_bytes"] == 20 and a["budget"]["source"] == "codex config" and a["budget"]["level"] == "critical"
    assert a["files"][0]["status"] == "truncated" and "cut at 20" in a["truncation"]
    assert digest() == before


def test_verify_endpoint_and_preview_errors(client, git_repo, tmp_path):
    r = client.post("/api/tool-profiles/verify", json={"repository": str(git_repo), "profile": "minimal"})
    assert r.status_code == 200 and r.json()["verified"] is True
    assert client.post("/api/tool-profiles/verify", json={"repository": str(git_repo), "profile": "x"}).status_code == 400
    assert client.post("/api/tool-profiles/verify", json={"repository": str(tmp_path), "profile": "full"}).status_code == 400
    assert client.get("/api/context/preview", params={"repository": str(tmp_path)}).status_code == 400


def test_pages_load_the_context_ui(client, git_repo):
    assert "/static/context.js" in client.get("/").text and client.get("/static/context.js").status_code == 200
    index = client.get("/").text
    for needle in ("ctx-tool-output", "ctx-tool-profile", "ctx-subagents", "Allow subagents", "ctx-cwd", "ctx-skills"):
        assert needle in index
    t = client.post("/api/tasks", json={"repository": str(git_repo), "prompt": "go"}).json()
    page = client.get(f"/tasks/{t['id']}").text
    assert "Send Standard" in page and "Send Fast" in page and 'id="ctx-panel"' in page
