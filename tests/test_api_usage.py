"""HTTP surface of the usage-optimization features, against the fake app-server."""
import time

import pytest
from fastapi.testclient import TestClient

from app.catalog import parse_catalog
from app.main import create_app

from conftest import FakeAppServer, FakeRunner


@pytest.fixture
def client(settings):
    settings.backend = "app-server"
    server = FakeAppServer("fake", True)
    app = create_app(settings, FakeRunner(), server)
    app.state.manager._app_server.on_global(app.state.manager._on_global_notification)
    with TestClient(app) as c:
        yield c


def wait_done(client, task_id, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = client.get(f"/api/tasks/{task_id}").json()
        if t["status"] not in ("queued", "starting", "running"):
            return t
        time.sleep(0.1)
    raise AssertionError("task did not finish")


def test_new_task_defaults_through_the_api(client, git_repo):
    t = client.post("/api/tasks", json={"repository": str(git_repo), "prompt": "ok"}).json()
    assert (t["service_tier"], t["auto_approval"], t["web_search_mode"], t["sandbox"]) == ("default", 1, "cached", "workspace-write")
    assert (t["model_verbosity"], t["adaptive_reasoning"], t["context_guard"]) == ("low", 1, 1)
    done = wait_done(client, t["id"])
    assert done["status"] == "completed" and done["backend"] == "app-server"
    assert done["context"]["window"] == 10000 and done["cache_hit_rate"] == 0.0
    assert done["observed_quota"]["note"].startswith("Observed only")


def test_dashboard_rows_carry_cache_and_context(client, git_repo):
    t = client.post("/api/tasks", json={"repository": str(git_repo), "prompt": "ok"}).json()
    wait_done(client, t["id"])
    client.post(f"/api/tasks/{t['id']}/messages", json={"prompt": "ok again"})
    wait_done(client, t["id"])
    row = client.get("/api/tasks").json()["tasks"][0]
    assert row["cache_hit_rate"] == 90.0 and row["context"]["tokens"] == 2000
    usage = client.get(f"/api/tasks/{t['id']}/usage").json()
    assert [u["cache_hit_rate"] for u in usage["turns"]] == [0.0, 90.0] and usage["latest"]["turn"] == 2


def test_limits_endpoint_and_history(client, git_repo):
    limits = client.get("/api/limits").json()
    assert limits["available"] and [w["label"] for w in limits["windows"]] == ["5 hour", "Weekly"]
    assert limits["available_resets"] == 2 and limits["plan_type"] == "pro"
    t = client.post("/api/tasks", json={"repository": str(git_repo), "prompt": "ok"}).json()
    wait_done(client, t["id"])
    history = client.get("/api/limits/history", params={"task_id": t["id"]}).json()["history"]
    assert {h["reason"] for h in history} == {"turn_start", "turn_end"}
    assert client.get("/api/limits/history", params={"limit": 1}).json()["history"][0]["ts"]


def test_limits_not_available_with_the_exec_backend(settings):
    settings.backend = "exec"
    with TestClient(create_app(settings, FakeRunner())) as c:
        r = c.get("/api/limits").json()
        assert r["available"] is False and "app-server" in r["error"]


def test_compact_endpoint(client, git_repo):
    t = client.post("/api/tasks", json={"repository": str(git_repo), "prompt": "ok"}).json()
    wait_done(client, t["id"])
    assert client.post(f"/api/tasks/{t['id']}/compact").status_code == 200
    done = wait_done(client, t["id"])
    assert done["status"] == "completed" and done["codex_thread_id"] == client.get(f"/api/tasks/{t['id']}").json()["codex_thread_id"]
    assert [u["kind"] for u in client.get(f"/api/tasks/{t['id']}/usage").json()["turns"]] == ["turn", "compact"]
    assert client.post("/api/tasks/nope/compact").status_code == 404


def test_retry_with_more_effort_over_http(client, git_repo):
    t = client.post("/api/tasks", json={"repository": str(git_repo), "prompt": "fail", "reasoning_effort": "low", "auto_retry": False}).json()
    done = wait_done(client, t["id"])
    assert done["retry_suggestion"]["effort"] == "medium" and done["reasoning_effort"] == "low"
    assert client.post(f"/api/tasks/{t['id']}/messages", json={"prompt": "ok", "reasoning_effort": "medium"}).status_code == 200
    done = wait_done(client, t["id"])
    assert done["reasoning_effort"] == "medium" and done["status"] == "completed"


def test_invalid_settings_are_400(client, git_repo):
    for body in ({"sandbox": "danger-full-access"}, {"model_verbosity": "x"}, {"service_tier": "!"}):
        r = client.post("/api/tasks", json={"repository": str(git_repo), "prompt": "ok", **body})
        assert r.status_code == 400, body


# ---------- model catalog ----------

SOL = {"slug": "gpt-6.1-sol", "display_name": "GPT-6.1-Sol", "visibility": "list", "priority": 1,
       "default_reasoning_level": "low", "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "xhigh", "max", "ultra")],
       "service_tiers": [{"id": "priority", "name": "Fast", "description": "2x speed, increased usage"}],
       "support_verbosity": True, "default_verbosity": "low", "context_window": 272000}


def test_catalog_keeps_what_the_form_needs():
    m = parse_catalog({"models": [SOL, {"slug": "hidden", "visibility": "hide"}]})
    assert [x["slug"] for x in m] == ["gpt-6.1-sol"]
    sol = m[0]
    assert sol["efforts"] == ["low", "medium", "high", "xhigh", "max", "ultra"]  # only what the model supports
    assert sol["service_tiers"] == [{"id": "priority", "name": "Fast", "description": "2x speed, increased usage"}]
    assert sol["supports_verbosity"] and sol["context_window"] == 272000


def test_recommended_model_only_when_codex_lists_it(monkeypatch):
    import asyncio
    from app import catalog

    async def load(listed):
        async def fake_exec(*a, **k):
            class P:
                async def communicate(self):
                    import json
                    return json.dumps({"models": listed}).encode(), b""
            return P()
        monkeypatch.setattr(catalog.asyncio, "create_subprocess_exec", fake_exec)
        return await catalog.ModelCatalog("codex")._load()

    assert asyncio.run(load([SOL]))["recommended_model"] == "gpt-6.1-sol"
    assert asyncio.run(load([{**SOL, "slug": "other"}]))["recommended_model"] == ""  # not installed: Codex default
    monkeypatch.setenv("CODEX_GUI_PREFERRED_MODEL", "other")
    assert asyncio.run(load([{**SOL, "slug": "other"}]))["recommended_model"] == "other"
