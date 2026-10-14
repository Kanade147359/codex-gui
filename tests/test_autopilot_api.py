import pytest
from fastapi.testclient import TestClient
from app.main import create_app
from conftest import FakeRunner

from autopilot_helpers import plan


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings, FakeRunner())) as c:
        yield c


def test_workflow_ui_dry_run_opt_in_and_atomic_registration(client, git_repo):
    raw = plan(git_repo)
    assert "Workflow manifest" in client.get("/autopilot").text
    assert client.get("/static/autopilot.js").status_code == 200
    preview = client.post("/api/autopilot/dry-run", json=dict(manifest=raw)).json()
    assert preview["plan"]["base_sha"] and preview["target"] == "workflow-owned integration ref"
    assert (
        client.post(
            "/api/autopilot/workflows", json=dict(manifest=raw, expected_digest=preview["input_digest"])
        ).status_code
        == 400
    )
    response = client.post(
        "/api/autopilot/workflows", json=dict(manifest=raw, expected_digest=preview["input_digest"], opt_in=True)
    )
    assert response.status_code == 200
    workflow = response.json()
    assert client.get("/api/autopilot/workflows/" + workflow["id"]).json()["nodes"][0]["state"] == "pending"
    assert (
        client.post(
            "/api/autopilot/workflows", json=dict(manifest=raw, expected_digest=preview["input_digest"], opt_in=True)
        ).status_code
        == 400
    )
    assert not client.get("/api/tasks").json()["tasks"]
    assert client.get("/api/autopilot/backlog").json() == {"tasks": []}
    assert client.post("/api/autopilot/backlog/dry-run", json=dict(tasks=[])).status_code == 400
    assert client.get("/api/autopilot/tasks/unregistered").json() == {"autopilot": False}


def test_bad_yaml_path_and_zip_inputs_do_not_create_partial_workflows(client, git_repo):
    raw = plan(git_repo)
    raw["inputs"] = {"../escape.md": "unsafe"}
    assert client.post("/api/autopilot/dry-run", json=dict(manifest=raw)).status_code == 400
    assert client.post(
        "/api/autopilot/dry-run", json=dict(manifest="!!python/object/apply:os.system [echo unsafe]")
    ).status_code in (400, 422)
    raw = plan(git_repo)
    raw["input_zip"] = "not supported"
    assert client.post("/api/autopilot/dry-run", json=dict(manifest=raw)).status_code == 400
    assert client.get("/api/autopilot/workflows").json() == {"workflows": []}
