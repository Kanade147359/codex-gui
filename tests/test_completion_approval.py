"""Verified automatic approval: isolated Git, DB, subprocesses and Codex threads."""
import asyncio
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import completion
from app.main import create_app
from conftest import FakeRunner, wait_for
from test_completion import calls, done


@pytest.mark.parametrize("backend", ["exec", "app-server"])
@pytest.mark.parametrize("status,rules,enabled,outcome,evidence", [
    ("SUCCESS", {}, True, "success", "PASS"),
    ("SUCCESS", {"required_paths": ["README.md"]}, True, "success", "PASS"),
    ("SUCCESS", {"required_paths": ["missing.bin"]}, True, "incomplete", "FAIL"),
    ("SUCCESS", {"required_changed_paths": ["README.md"]}, True, "incomplete", "FAIL"),
    ("SUCCESS", {"validation_commands": [["/usr/bin/true"]]}, True, "needs_review", "UNKNOWN"),
    ("MISSING", {}, True, "needs_review", "UNKNOWN"),
    ("BLOCKED", {}, True, "blocked", "PASS"),
    ("NEEDS_INPUT", {}, True, "needs_input", "PASS"),
    ("PARTIAL", {}, True, "incomplete", "PASS"),
    ("SUCCESS", {"manual_approval": True}, True, "needs_review", "PASS"),
    ("SUCCESS", {}, False, "needs_review", "PASS"),
])
def test_verified_approval_controls_dependencies_and_never_retries(
        backend, status, rules, enabled, outcome, evidence, git_repo, make_manager, fake_codex_state, monkeypatch):
    monkeypatch.setattr(completion.shutil, "which", lambda _: None)
    m = make_manager(backend=backend, retry_backoff_seconds=(0.01,))
    m.set_completion_approval(enabled)
    async def scenario():
        m.scheduler.start(0.02)
        try:
            # Fake Codex records its semantic result; the contract is explicit, never prompt-derived.
            parent = await m.create_task(repository=str(git_repo), prompt=f"semantic {status} Final result.", completion_contract=rules)
            child = await m.create_task(repository=str(git_repo), prompt="ok", depends_on=[parent["id"]])
            final = await done(m, parent)
            assert final["status"] == "completed" and final["task_outcome"] == outcome
            assert final["evidence_result"] == evidence
            assert final["retry_count"] == 0 and final["next_retry_at"] is None
            audit = m.db.completion_approvals(parent["id"])
            if outcome == "success":
                assert final["approval_source"] == "auto_evidence" and final["approved_at"]
                assert not final["manual_override"] and len(audit) == 1
                assert audit[0]["task_id"] == parent["id"] and audit[0]["semantic_status"] == "SUCCESS"
                assert audit[0]["evidence_result"] == "PASS" and audit[0]["approval_source"] == "auto_evidence"
                assert audit[0]["approved_at"] == final["approved_at"] and audit[0]["reason"] == final["outcome_reason"]
                assert json.loads(audit[0]["completion_checks"]) == json.loads(final["completion_checks"])
                assert m.db.completion_overrides(parent["id"]) == []
                await done(m, child)
                for _ in range(10):
                    m.tick()
                assert len(m.db.list_attempts(child["id"])) == 1
            else:
                assert final["approval_source"] == "" and final["approved_at"] is None and audit == []
                await asyncio.sleep(0.1)
                m.tick()
                assert m.get(child["id"])["status"] == "waiting_dependencies"
                assert m.db.list_attempts(child["id"]) == [] and len(m.db.list_attempts(parent["id"])) == 1
            wire = calls(fake_codex_state)
            turns = wire if backend == "exec" else [c for c in wire if c["method"] == "turn/start"]
            assert len(turns) == (2 if outcome == "success" else 1)  # No extra AI review or retries.
        finally:
            await m.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["unknown", "validation_failed"])
def test_uncertain_or_failed_evidence_cannot_be_auto_approved(kind, git_repo, make_manager, monkeypatch):
    m = make_manager(retry_backoff_seconds=(0.01,))
    if kind == "unknown":
        original = completion.check
        async def uncertain(*args):
            fields = await original(*args)
            return dict(fields, evidence_result="UNKNOWN", evidence_reason="Local facts cannot be verified.")
        monkeypatch.setattr(completion, "check", uncertain)
        rules, expected = {}, "needs_review"
    else:
        async def failed(*args):
            return False, "validation exited 1"
        monkeypatch.setattr(completion, "validate_command", failed)
        rules, expected = {"validation_commands": [["/usr/bin/false"]]}, "incomplete"
    async def scenario():
        try:
            parent = await m.create_task(repository=str(git_repo), prompt="ok", completion_contract=rules)
            child = await m.create_task(repository=str(git_repo), prompt="ok", depends_on=[parent["id"]])
            final = await done(m, parent)
            assert final["task_outcome"] == expected and final["approval_source"] == ""
            assert final["evidence_result"] == ("UNKNOWN" if kind == "unknown" else "FAIL")
            await asyncio.sleep(0.05)
            m.tick()
            assert m.get(child["id"])["status"] == "waiting_dependencies"
            assert not m.db.list_attempts(child["id"]) and final["retry_count"] == 0
            assert m.db.completion_approvals(parent["id"]) == []
        finally:
            await m.shutdown()
    asyncio.run(scenario())


def test_approval_setting_is_read_after_checks_and_rerun_uses_no_codex_call(git_repo, make_manager, fake_codex_state, monkeypatch):
    m = make_manager()
    original = completion.check
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(*args):
            fields = await original(*args)
            entered.set()
            await release.wait()
            return fields
        monkeypatch.setattr(completion, "check", slow)
        try:
            parent = await m.create_task(repository=str(git_repo), prompt="ok")
            child = await m.create_task(repository=str(git_repo), prompt="ok", depends_on=[parent["id"]])
            await asyncio.wait_for(entered.wait(), 10)
            m.set_completion_approval(False)
            release.set()
            assert (await done(m, parent))["task_outcome"] == "needs_review"
            assert not m.db.completion_approvals(parent["id"])
            m.set_completion_approval(True)
            approved = await m.rerun_completion(parent["id"])
            assert approved["task_outcome"] == "success" and approved["approval_source"] == "auto_evidence"
            await done(m, child)
            assert len(calls(fake_codex_state)) == 2 and len(m.db.list_attempts(parent["id"])) == 1
        finally:
            await m.shutdown()
    asyncio.run(scenario())


def test_manual_override_has_separate_approval_source_and_persistent_history(git_repo, make_manager):
    m = make_manager()
    async def scenario():
        try:
            task = await m.create_task(repository=str(git_repo), prompt="ok", completion_contract={"manual_approval": True})
            await done(m, task)
            manual = m.override_completion(task["id"], "success", "Reviewed artifacts.", True)
            assert manual["approval_source"] == "manual" and manual["approved_at"] == manual["manual_override_at"]
            audit = m.db.completion_approvals(task["id"])
            assert len(audit) == 1 and audit[0]["approval_source"] == "manual"
            assert audit[0]["actor"] == manual["manual_override_by"] and audit[0]["evidence_result"] == "PASS"
            await m.rerun_completion(task["id"])
            assert m.get(task["id"])["approval_source"] == "" and m.db.completion_approvals(task["id"]) == audit
            assert len(m.db.completion_overrides(task["id"])) == 1
        finally:
            await m.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("rules,enabled,release", [({}, True, True),
    ({"required_paths": ["missing.bin"]}, True, False),
    ({"validation_commands": [["/usr/bin/true"]]}, True, False), ({}, False, False)])
def test_scheduled_instruction_dependencies_share_approval_semantics(
        rules, enabled, release, git_repo, make_manager, fake_codex_state, monkeypatch):
    monkeypatch.setattr(completion.shutil, "which", lambda _: None)
    m = make_manager(backend="app-server")
    async def scenario():
        m.scheduler.start(0.02)
        try:
            target = await m.create_task(repository=str(git_repo), prompt="ok")
            target = await done(m, target)
            m.set_completion_approval(enabled)
            parent = await m.create_task(repository=str(git_repo), prompt="ok", completion_contract=rules)
            scheduled = await m.schedule_instruction(target["id"], "ok scheduled continuation", [parent["id"]])
            await done(m, parent)
            if release:
                await wait_for(lambda: m.db.get_scheduled(scheduled["id"])["status"] == "completed")
                assert m.get(target["id"])["codex_thread_id"] == target["codex_thread_id"]
                assert m.get(target["id"])["approval_source"] == "auto_evidence"
            else:
                await asyncio.sleep(0.1)
                m.tick()
                assert m.db.get_scheduled(scheduled["id"])["status"] == "waiting_dependencies"
                assert len(m.db.list_attempts(target["id"])) == 1
            starts = [c for c in calls(fake_codex_state) if c["method"] == "turn/start"]
            assert len(starts) == (3 if release else 2)
        finally:
            await m.shutdown()
    asyncio.run(scenario())


def test_audit_failure_rolls_back_success_and_keeps_dependencies_paused(git_repo, make_manager, monkeypatch):
    m = make_manager()
    m.set_completion_approval(False)
    async def scenario():
        try:
            parent = await m.create_task(repository=str(git_repo), prompt="ok")
            before = await done(m, parent)
            child = await m.create_task(repository=str(git_repo), prompt="ok", depends_on=[parent["id"]])
            m.set_completion_approval(True)
            fields = await m._completion_fields(parent["id"], before["semantic_result"])
            def unavailable(*args):
                raise sqlite3.OperationalError("audit unavailable")
            monkeypatch.setattr(m.db, "_insert_completion_approval", unavailable)
            with pytest.raises(sqlite3.OperationalError):
                m.db.update_task(parent["id"], **fields)
            assert m.get(parent["id"]) == before
            assert m.db.completion_approvals(parent["id"]) == [] and not m.db.queue_if_ready(child["id"])
        finally:
            await m.shutdown()
    asyncio.run(scenario())


def test_gui_approval_setting_defaults_on_and_survives_restart(settings, git_repo):
    from test_ui_recovery import wait_status
    with TestClient(create_app(settings, FakeRunner())) as c:
        assert c.get("/api/completion/settings").json() == {"auto_approve_verified_success": True}
        task = c.post("/api/tasks", json={"repository": str(git_repo), "prompt": "ok"}).json()
        accepted = wait_status(c, task["id"], {"completed"})
        assert accepted["approval_source"] == "auto_evidence"
        assert c.put("/api/completion/settings", json={"auto_approve_verified_success": False}).json() == {"auto_approve_verified_success": False}
        assert not c.get("/api/options").json()["completion_approval"]["auto_approve_verified_success"]
        assert c.put("/api/completion/settings", json={}).status_code == 422
    with TestClient(create_app(settings, FakeRunner())) as c:
        assert not c.get("/api/completion/settings").json()["auto_approve_verified_success"]
        historical = c.get(f"/api/tasks/{task['id']}").json()
        assert historical["task_outcome"] == "success" and historical["approval_source"] == "auto_evidence"
        assert historical["completion_approvals"] == accepted["completion_approvals"]
        assert len(c.app.state.manager.db.list_attempts(task["id"])) == 1


def test_compaction_preserves_approval_without_duplicate_audit(git_repo, make_manager):
    m = make_manager(backend="app-server")
    async def scenario():
        try:
            task = await m.create_task(repository=str(git_repo), prompt="ok")
            first = await done(m, task)
            audit = m.db.completion_approvals(task["id"])
            assert len(audit) == 1 and first["approval_source"] == "auto_evidence"
            await m.compact(task["id"])
            last = await done(m, task)
            assert last["task_outcome"] == "success" and last["approval_source"] == "auto_evidence"
            assert last["approved_at"] == first["approved_at"] and last["evidence_result"] == "PASS"
            assert m.db.completion_approvals(task["id"]) == audit
        finally:
            await m.shutdown()
    asyncio.run(scenario())
