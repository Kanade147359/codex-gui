"""Migration freeze tests use only temporary task data and fake CLI providers."""
import asyncio

import pytest
from fastapi.testclient import TestClient

from app.autopilot import workflow_controls
from app.config import Settings
from app.main import create_app
from app.task_manager import TaskError, _Turn
from autopilot_helpers import drive, install, plan
from conftest import FakeRunner


def test_freeze_default_and_explicit_environment_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_GUI_HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_GUI_AUTOPILOT_NEW_STARTS", raising=False)
    assert Settings.from_env().autopilot_new_starts is False
    for value in ("0", "true", "yes", ""):
        monkeypatch.setenv("CODEX_GUI_AUTOPILOT_NEW_STARTS", value)
        assert Settings.from_env().autopilot_new_starts is False
    monkeypatch.setenv("CODEX_GUI_AUTOPILOT_NEW_STARTS", "1")
    assert Settings.from_env().autopilot_new_starts is True


@pytest.mark.parametrize("url", ["javascript:alert(1)", "https://example.com/", "//localhost/x", "http://user@localhost/", "http://127.0.0.1:bad/", "/\\example.com", " /hiro/"])
def test_hiro_link_rejects_nonlocal_and_credential_urls(tmp_path, url):
    with pytest.raises(ValueError):
        Settings(home=tmp_path, hiro_url=url)


@pytest.mark.parametrize("url", ["http://127.0.0.1:18094/", "http://localhost:8080/", "http://[::1]:8080/", "/hiro/"])
def test_hiro_link_and_preserved_history(settings, url):
    settings.hiro_url = url
    with TestClient(create_app(settings, FakeRunner())) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert f'href="{url}"' in page.text and ">HIRO</a>" in page.text
        assert "Autopilot history" in page.text
        history = client.get("/autopilot").text
        assert "新規起動は凍結" in history and 'data-new-starts="false"' in history
        assert client.get("/api/autopilot/workflows").json() == {"workflows": []}


def test_api_combined_start_is_atomic_and_draft_cannot_resume(settings, git_repo):
    with TestClient(create_app(settings, FakeRunner())) as client:
        raw = plan(git_repo)
        preview = client.post("/api/autopilot/dry-run", json={"manifest": raw}).json()
        body = dict(manifest=raw, expected_digest=preview["input_digest"], opt_in=True, start=True)
        rejected = client.post("/api/autopilot/workflows", json=body)
        assert rejected.status_code == 400 and "frozen" in rejected.text
        assert client.get("/api/autopilot/workflows").json() == {"workflows": []}
        assert client.get("/api/tasks").json()["tasks"] == []
        body["start"] = False
        workflow = client.post("/api/autopilot/workflows", json=body).json()
        identity = workflow["id"]
        for action in ("start", "pause", "resume", "reconcile", "start"):
            response = client.post(f"/api/autopilot/workflows/{identity}/{action}")
            assert response.status_code in (200, 400)
            assert client.get(f"/api/autopilot/workflows/{identity}").json()["state"] == "draft"
        assert client.get("/api/tasks").json()["tasks"] == []
        # Removing request opt-in remains rejected even if the operator enables legacy starts.
        settings.autopilot_new_starts = True
        body["opt_in"] = False
        assert client.post("/api/autopilot/workflows", json=body).status_code == 400
        assert client.post(f"/api/autopilot/workflows/{identity}/start").status_code == 200


def test_direct_controller_gate_and_existing_workflow_continuation(make_manager, git_repo):
    async def scenario():
        manager = make_manager()
        ap = install(manager)
        manager.settings.autopilot_new_starts = False
        raw = plan(git_repo, ("A", "B"))
        preview = await ap.dry_run(raw)
        workflow = await ap.register(raw, preview["input_digest"])
        identity = workflow["id"]
        with pytest.raises(ValueError, match="frozen"):
            await ap.control(identity, "start")
        manager.settings.autopilot_new_starts = True
        await ap.control(identity, "start")
        # Deploying the freeze does not change the existing run or its pause/resume behavior.
        manager.settings.autopilot_new_starts = False
        await ap.control(identity, "pause")
        assert ap.ledger.view(identity)["state"] == "paused"
        await ap.control(identity, "resume")
        await drive(manager, lambda: ap.ledger.view(identity)["state"] == "completed")
        completed = ap.ledger.view(identity)
        assert all(n["state"] == "accepted" for n in completed["nodes"])
        assert len(ap.ledger.rows("acceptances")) == 2
        node = ap.ledger.get("nodes", identity + ":A")
        task = manager.get(node["task_id"])
        with pytest.raises(TaskError, match="cannot restart"):
            ap.queue_owner_turn(task, _Turn("more", resume_thread=task["codex_thread_id"]), {})
        with pytest.raises(ValueError, match="frozen"):
            workflow_controls.add_node(ap, identity, dict(owner_action=True, request_key="frozen-add-01",
                expected_target=workflow_controls.control_target(ap.ledger.get("workflows", identity)),
                node=plan(git_repo, ("C",))["nodes"][0]))
        assert ap.ledger.view(identity)["state"] == "completed"
        assert len(ap.ledger.rows("nodes")) == 2
        await manager.shutdown()

    asyncio.run(scenario())


def test_markdown_and_planner_import_cannot_launch_when_frozen(settings, git_repo):
    from test_autopilot_documents import uploads

    with TestClient(create_app(settings, FakeRunner())) as client:
        imported = client.post("/api/autopilot/import", files=uploads())
        assert imported.status_code == 200
        converted = client.post("/api/autopilot/import/convert", json=dict(imported.json(),
            repository=str(git_repo), permissions=dict(commit_owned_changes=True),
            policy=dict(extra_usage_disabled_confirmed=True)))
        assert converted.status_code == 200
        converted = converted.json()
        # This fixture includes a planner artifact and draft nodes. Neither is run by import.
        assert any(node.get("plan_artifact") for node in converted["preview"]["plan"]["nodes"])
        response = client.post("/api/autopilot/workflows", json=dict(manifest=converted["manifest"],
            expected_digest=converted["preview"]["input_digest"], opt_in=True, start=True))
        assert response.status_code == 400 and "frozen" in response.text
        assert client.get("/api/autopilot/workflows").json() == {"workflows": []}
        assert not client.get("/api/tasks").json()["tasks"]
