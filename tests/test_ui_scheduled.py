"""The scheduled-instruction UI (Node, stub DOM) rendered from what the real backend produces, and the requests its buttons send."""
import json
import os
import subprocess

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from conftest import FakeRunner
from test_ui_recovery import ROOT, pytestmark, wait_status  # noqa: F401  (pytestmark: skip without node)


def run_page(tmp_path, page, task_id, responses, actions=()):
    f = tmp_path / "responses.json"
    f.write_text(json.dumps(responses))
    res = subprocess.run(["node", str(ROOT / "ui_page_smoke.js"), page, task_id, str(f), str(ROOT.parent / "static")],
                         capture_output=True, text=True, timeout=60, env={**os.environ, "UI_ACTIONS": json.dumps(list(actions))})
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    assert out["errors"] == [], out["errors"]
    return out


def task_responses(c, task_id):
    return {
        f"/api/tasks/{task_id}": c.get(f"/api/tasks/{task_id}").json(), f"/api/tasks/{task_id}/log": {"entries": [], "offset": 0},
        f"/api/tasks/{task_id}/usage": c.get(f"/api/tasks/{task_id}/usage").json(), f"/api/tasks/{task_id}/git": c.get(f"/api/tasks/{task_id}/git").json(),
        f"/api/tasks/{task_id}/attempts": c.get(f"/api/tasks/{task_id}/attempts").json(), "/api/tasks": c.get("/api/tasks").json(),
        "/api/options": c.get("/api/options").json(), "/api/limits": {"available": False}}


def test_task_detail_and_dashboard_show_scheduled_instructions(git_repo, settings, tmp_path):
    with TestClient(create_app(settings, FakeRunner())) as c:
        def new(name, prompt):
            r = c.post("/api/tasks", json={"repository": str(git_repo), "prompt": prompt, "name": name})
            assert r.status_code == 200, r.text
            return r.json()["id"]

        x = new("X", "sleep")                            # the target thread is busy: nothing can be sent into it
        wait_status(c, x, {"running"})
        slow = new("P15", "sleep")                       # a dependency that is still running
        wait_status(c, slow, {"running"})
        done = new("P14", "ok")
        wait_status(c, done, {"completed"})
        r = c.post(f"/api/tasks/{x}/scheduled", json={"prompt": "integrate A/B and run all tests", "depends_on": [done, slow]})
        assert r.status_code == 200, r.text
        sid = r.json()["id"]
        assert c.post(f"/api/tasks/{x}/scheduled", json={"prompt": "<b>review</b> and commit", "service_tier": "fast"}).status_code == 200

        page = run_page(tmp_path, "task", x, task_responses(c, x))
        items = page["html"]["#scheduled-items"]
        assert f"#{sid}" in items and "WAITING" in items and "Speed: Standard" in items
        assert "✓" in items and "P14" in items and "…" in items and "P15" in items and "running" in items
        assert "integrate A/B and run all tests" in items and f'data-sid="{sid}"' in items and "Cancel" in items
        assert "&lt;b&gt;review&lt;/b&gt;" in items and "<b>review</b>" not in items          # the prompt is escaped
        assert "Speed: Fast" in items and "WAITING FOR THREAD" in items and "the thread&#39;s task is running" in items
        assert page["hidden"]["#scheduled-section"] is False and page["hidden"]["#schedule-box"] is False
        assert page["instructionDisabled"] is False
        for bad in ("undefined", "NaN", "[object Object]"):
            assert bad not in items

        dash = run_page(tmp_path, "dashboard", "", {
            "/api/tasks": c.get("/api/tasks").json(), "/api/options": c.get("/api/options").json(), "/api/limits": {"available": False},
            "/api/efficiency": c.get("/api/efficiency").json(), "/api/repos": {"repos": []},
            "/api/repo-info": {"git": {"label": "clean"}, "agents_md": {"found": False, "nested": []}}})
        rows = dash["html"]["#tasks-body"]
        assert "Scheduled: 2" in rows and "undefined" not in rows
        for t in (slow, x):
            c.post(f"/api/tasks/{t}/stop")


def test_schedule_button_posts_the_selection_and_cancel_deletes(git_repo, settings, tmp_path):
    with TestClient(create_app(settings, FakeRunner())) as c:
        def new(name, prompt):
            r = c.post("/api/tasks", json={"repository": str(git_repo), "prompt": prompt, "name": name})
            return r.json()["id"]

        x, a, b = new("X", "ok"), new("A", "ok"), new("B", "ok")
        for t in (x, a, b):
            wait_status(c, t, {"completed"})
        resp = task_responses(c, x)
        resp[f"POST /api/tasks/{x}/scheduled"] = {"id": 7}

        # Send after tasks complete + A and B ticked + Fast
        page = run_page(tmp_path, "task", x, resp, [
            {"sel": "#instruction", "set": {"value": "merge A and B, run tests"}},
            {"sel": "#delivery-after", "set": {"checked": True}, "fire": "change"},
            {"sel": "#sched-deps", "fire": "change", "target": {"type": "checkbox", "value": a, "checked": True}},
            {"sel": "#sched-deps", "fire": "change", "target": {"type": "checkbox", "value": b, "checked": True}},
            {"sel": "#sched-speed-fast", "set": {"checked": True}},
            {"sel": "#schedule-btn", "fire": "click"}])
        posts = [k for k in page["calls"] if k["method"] == "POST"]
        assert posts == [{"method": "POST", "url": f"/api/tasks/{x}/scheduled",
                          "body": {"prompt": "merge A and B, run tests", "depends_on": [a, b], "service_tier": "fast"}}]
        assert page["instructionValue"] == ""                       # a successful reservation clears the box

        # Send when thread is idle (no dependencies), Standard
        page = run_page(tmp_path, "task", x, resp, [
            {"sel": "#instruction", "set": {"value": "when idle"}},
            {"sel": "#delivery-idle", "set": {"checked": True}, "fire": "change"},
            {"sel": "#delivery-after", "set": {"checked": False}},
            {"sel": "#schedule-btn", "fire": "click"}])
        posts = [k for k in page["calls"] if k["method"] == "POST"]
        assert posts and posts[0]["body"] == {"prompt": "when idle", "depends_on": [], "service_tier": "standard"}

        # "after tasks" with nothing ticked is refused in the browser; an empty box too: nothing is sent
        for text in ("x", ""):
            page = run_page(tmp_path, "task", x, resp, [
                {"sel": "#instruction", "set": {"value": text}},
                {"sel": "#delivery-after", "set": {"checked": True}},
                {"sel": "#schedule-btn", "fire": "click"}])
            assert [k for k in page["calls"] if k["method"] == "POST"] == []
            assert "Select at least one task" in page["html"]["#action-msg"] or "Write an instruction" in page["html"]["#action-msg"]

        # Cancel
        resp[f"DELETE /api/tasks/{x}/scheduled/7"] = {"id": 7, "status": "cancelled"}
        page = run_page(tmp_path, "task", x, resp, [{"sel": "#scheduled-section", "fire": "click", "target": {"cancelSid": "7"}}])
        assert [k["url"] for k in page["calls"] if k["method"] == "DELETE"] == [f"/api/tasks/{x}/scheduled/7"]
        page = run_page(tmp_path, "task", x, resp, [{"sel": "#scheduled-section", "fire": "click"}])
        assert [k for k in page["calls"] if k["method"] == "DELETE"] == []   # a click that is not on a Cancel button does nothing
