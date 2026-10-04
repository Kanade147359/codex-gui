import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

from conftest import FakeRunner


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings, FakeRunner())) as c:
        yield c


def wait_done(client, task_id, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = client.get(f"/api/tasks/{task_id}").json()
        if t["status"] not in ("queued", "starting", "running"):
            return t
        time.sleep(0.1)
    raise AssertionError("task did not finish")


def test_pages(client):
    assert "New Task" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/tasks/missing").status_code == 404
    assert client.get("/api/tasks").json() == {"tasks": [], "counts": {}}


def test_create_error_is_400_with_message(client, tmp_path):
    r = client.post("/api/tasks", json={"repository": str(tmp_path), "prompt": "x"})
    assert r.status_code == 400 and "not a git repository" in r.json()["detail"]["message"]
    assert client.post("/api/tasks", json={"prompt": "x"}).status_code == 422


def test_full_flow(client, git_repo):
    r = client.post("/api/tasks", json={"repository": str(git_repo), "prompt": "ok", "name": "demo"})
    assert r.status_code == 200
    task = r.json()
    done = wait_done(client, task["id"])
    assert done["status"] == "completed"

    listing = client.get("/api/tasks").json()
    assert listing["counts"] == {"completed": 1} and listing["tasks"][0]["id"] == task["id"]
    assert client.get("/api/repos").json()["repos"] == [str(git_repo.resolve())]

    log = client.get(f"/api/tasks/{task['id']}/log").json()
    assert any(e["type"] == "turn.completed" for e in log["entries"])
    assert client.get(f"/api/tasks/{task['id']}/log", params={"offset": log["offset"]}).json()["entries"] == []

    git = client.get(f"/api/tasks/{task['id']}/git").json()
    assert git["available"] and "out.txt" in git["diff"]
    assert client.get(f"/tasks/{task['id']}").status_code == 200

    r = client.delete(f"/api/tasks/{task['id']}/worktree")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "dirty"
    assert client.post(f"/api/tasks/{task['id']}/commit", json={"message": "m"}).status_code == 200
    assert client.delete(f"/api/tasks/{task['id']}/worktree").json()["worktree_removed"] == 1
    assert client.post(f"/api/tasks/{task['id']}/stop").status_code == 409


def test_stop_via_api_and_history_survives_restart(settings, git_repo):
    with TestClient(create_app(settings, FakeRunner())) as c:
        tid = c.post("/api/tasks", json={"repository": str(git_repo), "prompt": "sleep"}).json()["id"]
        deadline = time.time() + 10
        while c.get(f"/api/tasks/{tid}").json()["status"] != "running" and time.time() < deadline:
            time.sleep(0.05)
        assert c.post(f"/api/tasks/{tid}/stop").status_code == 200
        assert wait_done(c, tid)["status"] == "stopped"
    # "restart the GUI": a brand-new app on the same data directory still has the history
    with TestClient(create_app(settings, FakeRunner())) as c2:
        tasks = c2.get("/api/tasks").json()["tasks"]
        assert [(t["id"], t["status"]) for t in tasks] == [(tid, "stopped")]
        assert c2.get(f"/api/tasks/{tid}/log").json()["entries"]


def test_instruction_and_usage_endpoints(client, git_repo):
    tid = client.post("/api/tasks", json={"repository": str(git_repo), "prompt": "ok"}).json()["id"]
    first = wait_done(client, tid)
    thread = first["codex_thread_id"]
    assert thread and first["last_turn_at"]

    r = client.post(f"/api/tasks/{tid}/messages", json={"prompt": "ok more"})
    assert r.status_code == 200 and r.json()["status"] == "queued"
    deadline = time.time() + 15
    while len(client.get(f"/api/tasks/{tid}/usage").json()["turns"]) < 2 and time.time() < deadline:
        time.sleep(0.1)
    done = wait_done(client, tid)
    assert done["codex_thread_id"] == thread
    usage = client.get(f"/api/tasks/{tid}/usage").json()
    assert [t["turn"] for t in usage["turns"]] == [1, 2] and usage["latest"]["cache_hit_rate"] == 80
    assert client.get("/api/tasks").json()["tasks"][0]["cache_hit_rate"] == 80

    assert client.post(f"/api/tasks/{tid}/messages", json={"prompt": " "}).status_code == 400
    assert client.post(f"/api/tasks/{tid}/messages", json={}).status_code == 422
    assert client.post("/api/tasks/missing/messages", json={"prompt": "x"}).status_code == 404
    assert client.get("/api/tasks/missing/usage").status_code == 404

    r = client.post(f"/api/tasks/{tid}/new-session", json={"prompt": "ok fresh"})
    assert r.status_code == 200
    deadline = time.time() + 15
    while len(client.get(f"/api/tasks/{tid}/usage").json()["turns"]) < 3 and time.time() < deadline:
        time.sleep(0.1)
    assert wait_done(client, tid)["codex_thread_id"] != thread


def test_send_while_running_is_409(client, git_repo):
    tid = client.post("/api/tasks", json={"repository": str(git_repo), "prompt": "sleep"}).json()["id"]
    deadline = time.time() + 10
    while client.get(f"/api/tasks/{tid}").json()["status"] != "running" and time.time() < deadline:
        time.sleep(0.05)
    r = client.post(f"/api/tasks/{tid}/messages", json={"prompt": "x"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "active"
    client.post(f"/api/tasks/{tid}/stop")
    wait_done(client, tid)


def test_static_assets_are_versioned_and_revalidated(client):
    # Edited CSS/JS must show up on the next page load, not after heuristic browser caching.
    html = client.get('/dependencies').text
    assert '/static/graph.js?v=' in html and '/static/style.css?v=' in html
    assert client.get('/static/graph.js').headers['cache-control'] == 'no-cache'
