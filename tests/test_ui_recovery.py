"""The dependency / recovery UI (Node, stub DOM) rendered from what the real backend produces."""
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from conftest import FakeRunner

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
ROOT = Path(__file__).parent


def page_run(tmp_path, page, task_id, responses):
    f = tmp_path / "responses.json"
    f.write_text(json.dumps(responses))
    res = subprocess.run(["node", str(ROOT / "ui_page_smoke.js"), page, task_id, str(f), str(ROOT.parent / "static")],
                         capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    assert out["errors"] == [], out["errors"]
    return out


def wait_status(c, task_id, wanted, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        t = c.get(f"/api/tasks/{task_id}").json()
        if t["status"] in wanted:
            return t
        time.sleep(0.05)
    raise AssertionError(f"{task_id} never reached {wanted}")


def test_dashboard_and_task_pages_show_waiting_blocked_and_retrying(git_repo, settings, tmp_path):
    settings.retry_backoff_seconds = (300.0,)
    with TestClient(create_app(settings, FakeRunner())) as c:
        def new(name, prompt, **kw):
            r = c.post("/api/tasks", json={"repository": str(git_repo), "prompt": prompt, "name": name, **kw})
            assert r.status_code == 200, r.text
            return r.json()["id"]

        a = new("A", "err 1 boom", auto_retry=False)
        wait_status(c, a, {"failed"})
        d = new("D", "ok", depends_on=[a])
        blocked = wait_status(c, d, {"blocked"})
        b = new("B", "crash")
        retrying = wait_status(c, b, {"retry_wait"})
        sleeper = new("S", "sleep")
        wait_status(c, sleeper, {"running"})
        w2 = new("W2", "ok", depends_on=[sleeper])
        waiting = wait_status(c, w2, {"waiting_dependencies"})
        assert (waiting["deps_done"], waiting["deps_total"]) == (0, 1)

        responses = {"/api/tasks": c.get("/api/tasks").json(), "/api/options": c.get("/api/options").json(), "/api/limits": {"available": False},
                     "/api/efficiency": c.get("/api/efficiency").json(), "/api/repos": {"repos": []},
                     "/api/repo-info": {"git": {"label": "clean"}, "agents_md": {"found": False, "nested": []}}}
        dash = page_run(tmp_path, "dashboard", "", responses)
        rows = dash["html"]["#tasks-body"]
        assert "Blocked" in rows and "Dependency A failed" in rows
        assert "Retry 1/3 in" in rows and 'data-retry-at="' in rows
        assert "Waiting (0/1 complete)" in rows
        for bad in ("undefined", "NaN", "[object Object]"):
            assert bad not in rows

        def task_page(task_id):
            t = c.get(f"/api/tasks/{task_id}").json()
            return page_run(tmp_path, "task", task_id, {
                f"/api/tasks/{task_id}": t, f"/api/tasks/{task_id}/log": {"entries": [], "offset": 0},
                f"/api/tasks/{task_id}/usage": c.get(f"/api/tasks/{task_id}/usage").json(),
                f"/api/tasks/{task_id}/git": c.get(f"/api/tasks/{task_id}/git").json(),
                f"/api/tasks/{task_id}/attempts": c.get(f"/api/tasks/{task_id}/attempts").json(),
                "/api/options": c.get("/api/options").json(), "/api/limits": {"available": False}})

        p = task_page(d)  # blocked
        assert "Blocked" in p["html"]["#task-status"]
        assert "✗" in p["html"]["#deps-items"] and "failed" in p["html"]["#deps-items"] and "(0/1 complete)" in p["html"]["#deps-summary"]
        assert p["hidden"]["#deps-section"] is False and p["hidden"]["#run-anyway-btn"] is False and p["hidden"]["#retry-deps-btn"] is False
        assert p["hidden"]["#stop-btn"] is True
        assert "blocked by a dependency" in p["html"]["#instruction-hint"]

        p = task_page(b)  # retry_wait
        assert "Retry 1/3 in" in p["html"]["#task-status"]
        assert "1 / 3" in p["html"]["#recovery-dl"] and "Next retry" in p["html"]["#recovery-dl"] and "process_exited" in p["html"]["#recovery-dl"]
        assert p["hidden"]["#retry-now-btn"] is False and p["hidden"]["#retry-btn"] is True and p["hidden"]["#stop-btn"] is False
        assert p["hidden"]["#attempts"] is False and "first run" in p["html"]["#attempts-body"] and "interrupted" in p["html"]["#attempts-body"]

        p = task_page(a)  # failed, and the dependency of D
        assert p["hidden"]["#retry-btn"] is False and "Waiting for this task" in p["html"]["#blocks-line"]

        p = task_page(w2)  # waiting
        assert "Waiting for 1 task" in p["html"]["#deps-summary"]
        assert "Waiting (0/1 complete)" in p["html"]["#task-status"] and p["hidden"]["#run-anyway-btn"] is False and p["hidden"]["#retry-deps-btn"] is True
        c.post(f"/api/tasks/{sleeper}/stop")
        wait_status(c, sleeper, {"stopped"})
