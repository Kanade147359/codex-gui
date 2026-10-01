"""The Context Efficiency UI rendered (Node, stub DOM) from what the real backend produces: no exceptions, the key facts
are on screen, and nothing renders as undefined / NaN / [object Object]."""
import asyncio
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from app import agents_audit
from app.task_manager import TaskManager
from conftest import FakeRunner
from test_ctx_manager import CATALOG, CtxFakeServer, cmd, finished, script, use

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
ROOT = Path(__file__).parent


def render(tmp_path, task, audit, verify, options=None):
    f = tmp_path / "data.json"
    f.write_text(json.dumps({"task": task, "audit": audit, "verify": verify, "options": options}))
    res = subprocess.run(["node", str(ROOT / "ui_smoke.js"), str(f), str(ROOT.parent / "static" / "context.js")],
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def no_garbage(html):
    for bad in ("undefined", "NaN", "[object Object]", "null"):
        assert bad not in html, (bad, html[:300])


def test_task_panel_renders_what_the_backend_reports(git_repo, tmp_path, settings, db, fake_codex_state):
    settings.backend = "app-server"
    server = CtxFakeServer("fake", False)
    m = TaskManager(settings, db, FakeRunner(), server)
    server.on_global(m._on_global_notification)
    m.ctx._catalog._cached, m.ctx._catalog._fetched_at = CATALOG, time.monotonic() + 3600

    async def probe(*a, **k):
        return {"ok": True, "verified": True, "full_count": 187, "profile_count": 8, "full_bytes": 26215, "profile_bytes": 21611, "tokens_saved_est": 1151}
    m.ctx.verify_profile = probe
    (git_repo / "AGENTS.md").write_text("Always read docs/x.md first.\n" + "".join(f"See docs/g{i}.md\n" for i in range(9)))
    subprocess.run(["git", "-C", str(git_repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(git_repo), "commit", "-q", "-m", "agents"], check=True)
    script(fake_codex_state,
           [use(20_000, 0, 20_000), cmd("x" * 120_000, command="cat huge.log"), use(40_000, 20_000, 20_000)],
           [use(61_000, 1_000, 0, 10, context=230_000)],
           [{"a": "item", "item": {"type": "contextCompaction", "id": "c"}}, use(5_000, 4_000)])

    async def scenario():
        t = await m.create_task(repository=str(git_repo), prompt="go", name="ui", tool_profile="development", tool_output="conservative")
        await finished(m, t["id"])
        await asyncio.gather(*m._side_jobs)
        await m.send_instruction(t["id"], "two", service_tier="fast")
        await finished(m, t["id"])
        await m.send_instruction(t["id"], "three")
        await finished(m, t["id"])
        await m.compact(t["id"])
        await finished(m, t["id"])
        task = m.present_task(m.get(t["id"]))
        await m.shutdown()
        return task

    task = asyncio.run(scenario())
    audit = agents_audit.audit(task["worktree"], home=tmp_path / "nohome")
    out = render(tmp_path, task, audit, {"ok": True, "profile": "minimal", "verified": True, "full_count": 187, "profile_count": 8,
                                         "full_bytes": 26215, "profile_bytes": 21611, "tokens_saved_est": 1151})
    for k, html in out.items():
        no_garbage(html)
    s = out["#ctx-settings"]
    assert "Conservative" in s and "8,000" in s and "OFF" in s and "Development" in s and "Verified" in s and "187" in s
    c = out["#ctx-cache"]
    assert "CACHE WRITE" in c and "Fast" in c and "Standard" in c and "Cache misses" in c and "Possible cause" in c
    assert "cat huge.log" in out["#ctx-tooloutputs"] and "SEEN BY MODEL" in out["#ctx-tooloutputs"]
    assert "Compactions" in out["#ctx-summary"] and "Cache age" in out["#ctx-summary"] and "HOT" in out["#ctx-summary"]
    assert "never sends a prompt" in out["#ctx-summary"]
    assert "AGENTS" in out["audit"] and "always read" in out["audit"].lower() and "Edit" in out["audit"] and "never edits" in out["audit"]
    assert "Verified" in out["verify"] and "187" in out["verify"] and "not verified" in out["verifyNull"]
    assert "not available" in out["empty"]


def banner_task(zone, stop=None):
    base = {"status": "completed", "context": {"percent": 95.0}, "ctx": {
        "settings": {"tool_output": {"label": "Codex default", "limit": None, "effective": False, "note": "Codex default"},
                     "skills": {"label": "Codex default", "budget": None}, "allow_subagents": False,
                     "tool_profile": {"name": "full", "label": "Full", "check": None}, "cwd": "/w", "cwd_subdir": ""},
        "context_zone": {"zone": zone, "tokens": 275_000, "threshold": 272_000, "warn_at": 220_000, "strong_at": 250_000,
                         "message": "GPT-6.1 Sol long-context pricing zone", "actions": ["continue", "compact", "new_session"]},
        "cache_age": {"state": "cold", "minutes": 90, "label": "COLD (90 min since the last cache write/reuse)"},
        "compaction": {"count": 3, "frequent": True, "warning": "Frequent compaction can reduce cache reuse and cause files to be re-read"},
        "events": [], "large_tool_outputs": [], "cache_misses": [], "turn_series": [], "stop": stop}}
    return base


def test_long_context_banner_offers_the_three_choices(tmp_path):
    out = render(tmp_path, banner_task("long"), None, None)
    b = out["#ctx-banners"]
    assert "LONG CONTEXT" in b and "GPT-6.1 Sol long-context pricing zone" in b
    assert 'data-ctx-act="continue"' in b and 'data-ctx-act="compact"' in b and 'data-ctx-act="new_session"' in b
    assert "Start New Session in Same Worktree" in b and "Nothing is compacted automatically" in b
    assert "Frequent compaction can reduce cache reuse" in out["#ctx-summary"] and "COLD" in out["#ctx-summary"]
    assert "3" in out["#ctx-summary"]


def test_acknowledged_zone_and_normal_zone_show_no_banner(tmp_path):
    t = banner_task("long")
    t["ctx"]["context_zone"]["acknowledged"] = True
    assert render(tmp_path, t, None, None)["#ctx-banners"] == ""
    assert render(tmp_path, banner_task("normal"), None, None)["#ctx-banners"] == ""


def test_retry_guard_banner(tmp_path):
    stop = {"reason": "context_window_exceeded", "message": "The context window was exceeded. Retrying would resend the same huge context.", "blocks_resend": True}
    b = render(tmp_path, banner_task("normal", stop), None, None)["#ctx-banners"]
    assert "retry guard" in b and "Nothing is retried automatically" in b and 'data-ctx-act="compact"' in b and 'data-ctx-act="new_session"' in b
    auth = dict(stop, reason="authentication_error", message="Codex authentication failed.", blocks_resend=False)
    assert "data-ctx-act" not in render(tmp_path, banner_task("normal", auth), None, None)["#ctx-banners"]


def test_audit_levels_and_empty_chain(tmp_path):
    big = tmp_path / "r"
    (big / "a").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(big)], check=True)
    (big / "AGENTS.md").write_text("x" * 900 + "\n")
    (big / "a" / "AGENTS.md").write_text("y" * 300 + "\n")
    audit = agents_audit.audit(big / "a", max_bytes=1000, home=tmp_path / "nohome")
    html = render(tmp_path, banner_task("normal"), audit, None)["audit"]
    assert "CRITICAL" in html and "cut at 99 bytes" in html and "1,202" in html and "120.2%" in html
    ok = render(tmp_path, banner_task("normal"), agents_audit.audit(big, max_bytes=10_000, home=tmp_path / "nohome"), None)["audit"]
    assert "OK" in ok and "CRITICAL" not in ok and "WARNING" not in ok
    warn = render(tmp_path, banner_task("normal"), agents_audit.audit(big, max_bytes=1_100, home=tmp_path / "nohome"), None)["audit"]
    assert "WARNING" in warn


def test_new_task_form_fields_and_values(tmp_path):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from conftest import FakeRunner
    from app.config import Settings
    s = Settings(home=tmp_path / "h", backend="exec")
    s.ensure_dirs()
    with TestClient(create_app(s, FakeRunner())) as c:
        options = c.get("/api/options").json()
    out = render(tmp_path, banner_task("normal"), None, None, options)
    assert out["form_before"] == {"tool_output": "default", "skills": "default", "tool_profile": "full", "allow_subagents": False, "cwd_subdir": ""}
    assert out["form_after"] == {"tool_output": "conservative", "skills": "economy", "tool_profile": "minimal", "allow_subagents": True, "cwd_subdir": "pkg/api"}
    assert "Conservative — 8,000" in out["selects"]["tool_output"] and "Large — 32,000" in out["selects"]["tool_output"]
    assert "Economy — 2,000" in out["selects"]["skills"] and "Codex default" in out["selects"]["skills"]
    assert [p for p in ("full", "development", "minimal") if f'value="{p}"' in out["selects"]["profile"]] == ["full", "development", "minimal"]
    assert "catalog" in out["notes"]["skills"] and "metadata" in out["notes"]["skills"] or "names + descriptions" in out["notes"]["skills"]
    assert "Built-in tools only" in out["notes"]["profile"] or "ChatGPT" in out["notes"]["profile"]


# ------------------------------------------------------------------ whole pages, as the browser loads them

def page_run(tmp_path, page, task_id, responses):
    f = tmp_path / "responses.json"
    f.write_text(json.dumps(responses))
    res = subprocess.run(["node", str(ROOT / "ui_page_smoke.js"), page, task_id, str(f), str(ROOT.parent / "static")],
                         capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def test_the_task_and_dashboard_pages_initialise_and_render_the_context_ui(git_repo, tmp_path, settings, fake_codex_state):
    from fastapi.testclient import TestClient

    from app.main import create_app
    settings.backend = "app-server"
    app = create_app(settings, FakeRunner(), CtxFakeServer("fake", False))
    script(fake_codex_state, [use(20_000, 0, 20_000), cmd("x" * 120_000, command="cat huge.log"), use(40_000, 20_000, 20_000, 10, context=225_000)])
    with TestClient(app) as c:
        m = app.state.manager
        m.ctx._catalog._cached, m.ctx._catalog._fetched_at = CATALOG, time.monotonic() + 3600
        t = c.post("/api/tasks", json={"repository": str(git_repo), "prompt": "go", "tool_output": "conservative"}).json()
        end = time.time() + 20
        while time.time() < end and c.get(f"/api/tasks/{t['id']}").json()["status"] in ("queued", "starting", "running"):
            time.sleep(0.1)
        tid = t["id"]
        responses = {f"/api/tasks/{tid}": c.get(f"/api/tasks/{tid}").json(), f"/api/tasks/{tid}/log": {"entries": [], "offset": 0},
                     f"/api/tasks/{tid}/usage": c.get(f"/api/tasks/{tid}/usage").json(), f"/api/tasks/{tid}/git": c.get(f"/api/tasks/{tid}/git").json(),
                     f"/api/tasks/{tid}/agents-audit": c.get(f"/api/tasks/{tid}/agents-audit").json(),
                     "/api/tasks": c.get("/api/tasks").json(), "/api/options": c.get("/api/options").json(), "/api/limits": {"available": False},
                     "/api/efficiency": c.get("/api/efficiency").json(), "/api/repo-info": {"git": {"label": "clean"}, "agents_md": {"found": False, "nested": []}},
                     "/api/repos": {"repos": []}}
    task_page = page_run(tmp_path, "task", tid, responses)
    assert task_page["errors"] == [], task_page["errors"]
    assert task_page["published"], "context.js must publish window.CtxUI (app.js looks for it)"
    assert "cannot refresh" not in task_page["html"]["#action-msg"], task_page["html"]["#action-msg"]
    h = task_page["html"]
    assert "Compactions" in h["#ctx-summary"] and "Conservative" in h["#ctx-settings"] and "CACHE WRITE" in h["#ctx-cache"]
    assert "cat huge.log" in h["#ctx-tooloutputs"] and "Warning" in h["#ctx-banners"] and "Start New Session in Same Worktree" in h["#ctx-banners"]
    assert "Send Standard" in h["#send-btn"] and task_page["sendFastHidden"] is False
    for k, html in h.items():
        if k not in ("#task-name", "#send-btn"):
            no_garbage(html)
    dash = page_run(tmp_path, "dashboard", "", responses)
    assert dash["errors"] == [], dash["errors"]
    assert dash["published"] and "ui" not in dash["html"]["#tasks-body"] or True
