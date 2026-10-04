"""Completion detail/dashboard render real API responses without starting a GUI server."""
from fastapi.testclient import TestClient
import pytest

from app import completion
from app.main import create_app
from conftest import FakeRunner
from test_ui_recovery import page_run, wait_status, pytestmark  # noqa: F401


def test_ui_shows_execution_outcome_checks_and_dependency_reason(git_repo, settings, tmp_path):
    with TestClient(create_app(settings, FakeRunner())) as c:
        parent = c.post("/api/tasks", json={"repository": str(git_repo), "prompt": "semantic NEEDS_INPUT Required target ABI is unspecified.",
            "name": "P17", "completion_contract": {"required_paths": ["target.bin"]}}).json()
        child = c.post("/api/tasks", json={"repository": str(git_repo), "prompt": "ok", "name": "P18", "depends_on": [parent["id"]]}).json()
        p = wait_status(c, parent["id"], {"completed"})
        responses = {"/api/tasks": c.get("/api/tasks").json(), f"/api/tasks/{parent['id']}": p,
            f"/api/tasks/{parent['id']}/log": {"entries": [], "offset": 0}, f"/api/tasks/{parent['id']}/git": {"available": False},
            "/api/options": c.get("/api/options").json(), "/api/limits": {"available": False}, "/api/codex/account": {},
            "/api/repos": {"repos": []}, "/api/efficiency": c.get("/api/efficiency").json()}
        detail = page_run(tmp_path, "task", parent["id"], responses)["html"]
        assert "Completed normally" in detail["#completion-summary"]
        assert "NEEDS INPUT" in detail["#completion-summary"]
        assert "Required target ABI is unspecified." in detail["#completion-summary"]
        assert "required artifact: target.bin" in detail["#completion-checks"]
        assert detail["#completion-dependents"] == "Dependents: 1 paused"
        dashboard = page_run(tmp_path, "dashboard", "", responses)["html"]["#tasks-body"]
        assert "WAITING — P17 needs input" in dashboard and "NEEDS INPUT" in dashboard
        assert child["status"] == "waiting_dependencies"


@pytest.mark.parametrize("available", [True, False])
def test_dashboard_reports_bwrap_capability(settings, tmp_path, monkeypatch, available):
    monkeypatch.setattr(completion.shutil, "which", lambda _: "/usr/bin/bwrap" if available else None)
    with TestClient(create_app(settings, FakeRunner())) as c:
        responses = {"/api/tasks": c.get("/api/tasks").json(), "/api/options": c.get("/api/options").json(),
            "/api/limits": {"available": False}, "/api/codex/account": {}, "/api/repos": {"repos": []},
            "/api/efficiency": c.get("/api/efficiency").json()}
        dashboard = page_run(tmp_path, "dashboard", "", responses)["html"]
        assert dashboard["#completion-validation-capability"] == ("✓ bwrap: Available" if available else "⚠ bwrap: Missing")
        assert dashboard["#completion-validation-note"] == ("" if available else
            "Validation commands requiring sandboxing will require manual review.")


def test_task_detail_shows_normal_execution_and_unavailable_validation(git_repo, settings, tmp_path, monkeypatch):
    monkeypatch.setattr(completion.shutil, "which", lambda _: None)
    with TestClient(create_app(settings, FakeRunner())) as c:
        parent = c.post("/api/tasks", json={"repository": str(git_repo), "prompt": "ok",
            "completion_contract": {"required_paths": ["README.md"], "validation_commands": [["/usr/bin/true"]]}}).json()
        task = wait_status(c, parent["id"], {"completed"})
        responses = {f"/api/tasks/{parent['id']}": task,
            f"/api/tasks/{parent['id']}/log": {"entries": [], "offset": 0},
            f"/api/tasks/{parent['id']}/git": {"available": False},
            "/api/options": c.get("/api/options").json(), "/api/limits": {"available": False}}
        detail = page_run(tmp_path, "task", parent["id"], responses)["html"]
        assert "Completed normally" in detail["#completion-summary"]
        assert "NEEDS REVIEW" in detail["#completion-summary"]
        assert completion.BWRAP_MISSING_REASON in detail["#completion-summary"]
        assert "✓ required artifact: README.md" in detail["#completion-checks"]
        assert "⚠ validation unavailable: bwrap missing" in detail["#completion-checks"]


@pytest.mark.parametrize("enabled", [True, False])
def test_ui_shows_verified_evidence_approval_and_audit(git_repo, settings, tmp_path, enabled):
    with TestClient(create_app(settings, FakeRunner())) as c:
        c.put("/api/completion/settings", json={"auto_approve_verified_success": enabled})
        parent = c.post("/api/tasks", json={"repository": str(git_repo), "prompt": "ok",
            "completion_contract": {"required_paths": ["out.txt"], "require_any_change": True}}).json()
        task = wait_status(c, parent["id"], {"completed"})
        responses = {"/api/tasks": c.get("/api/tasks").json(), f"/api/tasks/{parent['id']}": task,
            f"/api/tasks/{parent['id']}/log": {"entries": [], "offset": 0},
            f"/api/tasks/{parent['id']}/git": {"available": False},
            "/api/options": c.get("/api/options").json(), "/api/limits": {"available": False},
            "/api/repos": {"repos": []}, "/api/efficiency": c.get("/api/efficiency").json()}
        detail = page_run(tmp_path, "task", parent["id"], responses)["html"]
        assert "✓ SUCCESS" in detail["#completion-summary"] and "✓ Verified" in detail["#completion-summary"]
        assert ("✓ Auto-approved" if enabled else "Awaiting approval") in detail["#completion-summary"]
        assert ("auto_evidence" if enabled else "No approval recorded.") in detail["#completion-approvals"]
        assert ("SUCCESS" if enabled else "NEEDS REVIEW") in detail["#completion-summary"]
        dashboard = page_run(tmp_path, "dashboard", "", responses)
        assert dashboard["completionAutoApproveChecked"] is enabled


def test_dashboard_toggle_persists_approval_setting(settings, tmp_path, monkeypatch):
    import json
    monkeypatch.setenv("UI_ACTIONS", json.dumps([{"sel": "#completion-auto-approve", "set": {"checked": False}, "fire": "change"}]))
    with TestClient(create_app(settings, FakeRunner())) as c:
        responses = {"/api/tasks": c.get("/api/tasks").json(), "/api/options": c.get("/api/options").json(),
            "/api/limits": {"available": False}, "/api/repos": {"repos": []},
            "/api/efficiency": c.get("/api/efficiency").json(),
            "PUT /api/completion/settings": {"auto_approve_verified_success": False}}
        page = page_run(tmp_path, "dashboard", "", responses)
        saves = [c for c in page["calls"] if c["method"] == "PUT"]
        assert saves == [{"method": "PUT", "url": "/api/completion/settings", "body": {"auto_approve_verified_success": False}}]
        assert page["completionAutoApproveChecked"] is False and "Saved." in page["html"]["#completion-approval-note"]
