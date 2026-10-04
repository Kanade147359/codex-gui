"""Legacy compatibility and safe validation preflight, using only isolated databases/worktrees."""
import asyncio
import json
import shutil
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import completion
from app.database import Database, SCHEMA
from app.main import create_app
from app.task_manager import TaskManager
from conftest import FakeRunner
from test_completion import calls, done


@pytest.fixture
def legacy_db_path(settings, git_repo):
    with sqlite3.connect(settings.db_path) as conn:
        # The persisted base schema predates Completion Gate's migration columns.
        conn.executescript(SCHEMA)
        assert "task_outcome" not in {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        for task_id, status in (("a", "completed"), ("b", "waiting_dependencies"),
                                ("c", "completed"), ("f", "failed"), ("s", "stopped")):
            conn.execute("""INSERT INTO tasks
                (id, name, repository, worktree, branch, base_ref, base_sha, prompt, status, created_at, exit_code)
                VALUES (?, ?, ?, ?, 'main', 'main', 'base', 'old task', ?, '2026-01-01T00:00:00Z', ?)""",
                (task_id, task_id.upper(), str(git_repo), str(git_repo), status, 0 if status == "completed" else None))
        conn.executemany("""INSERT INTO task_dependencies (task_id, depends_on_task_id, created_at)
                            VALUES (?, 'a', '2026-01-01T00:00:00Z')""", [("b",), ("c",)])
    return settings.db_path


def test_legacy_completed_and_existing_dag_remain_successful(legacy_db_path):
    db = Database(legacy_db_path)
    try:
        parent = db.get_task("a")
        assert parent["status"] == "completed" and parent["task_outcome"] == "success"
        assert parent["outcome_source"] == "legacy" and not parent["manual_override"]
        assert db.get_task("b")["status"] == "waiting_dependencies"
        assert db.dependencies_ready("b") and db.queue_if_ready("b")
        assert db.get_task("c")["status"] == "completed" and db.get_task("c")["task_outcome"] == "success"
        assert db.dependencies_ready("c")
        assert db.get_task("f")["status"] == "failed" and db.get_task("s")["status"] == "stopped"
    finally:
        db.close()


def test_legacy_migration_and_startup_do_not_retroactively_run_checks(legacy_db_path, settings, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("migration/startup must not execute a historical completed task or its validation")
    monkeypatch.setattr(completion, "check", forbidden)
    monkeypatch.setattr(completion, "validate_command", forbidden)
    runner = FakeRunner()
    monkeypatch.setattr(runner, "spawn", forbidden)
    db = Database(legacy_db_path)
    try:
        manager = TaskManager(settings, db, runner)
        manager.recover()
        parent = db.get_task("a")
        assert parent["status"] == "completed" and parent["task_outcome"] == "success"
        assert json.loads(parent["completion_contract"]) == {}
        assert json.loads(parent["completion_checks"]) == [] and parent["completion_checked_at"] is None
        assert db.list_attempts("a") == [] and db.completion_overrides("a") == []
        assert db.dependencies_ready("b") and db.get_task("b")["status"] == "waiting_dependencies"
    finally:
        db.close()


def test_legacy_migration_is_idempotent_and_preserves_subsequent_gate_outcomes(legacy_db_path):
    db = Database(legacy_db_path)
    before = db.list_tasks()
    db._migrate()
    assert db.list_tasks() == before
    db.close()
    db = Database(legacy_db_path)
    assert db.list_tasks() == before
    # Explicit later re-evaluation uses the new semantics; another startup must not grandfather it again.
    db.update_task("a", task_outcome="needs_review", outcome_source="gate", outcome_reason="Explicit re-evaluation.")
    reevaluated = db.get_task("a")
    db.close()
    db = Database(legacy_db_path)
    try:
        assert db.get_task("a") == reevaluated and not db.dependencies_ready("b")
        assert db.get_task("c") == next(t for t in before if t["id"] == "c")
    finally:
        db.close()


@pytest.mark.parametrize("path,available", [("/usr/bin/bwrap", True), (None, False)])
def test_bwrap_capability_uses_safe_lookup(monkeypatch, path, available):
    lookups = []
    def which(name):
        lookups.append(name)
        return path
    monkeypatch.setattr(completion.shutil, "which", which)
    result = completion.validation_capability()
    assert result["bwrap_available"] is available and lookups == ["bwrap"]
    assert bool(result["message"]) is not available


def test_bwrap_missing_never_launches_validation_or_unsandboxed_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(completion.shutil, "which", lambda _: None)
    async def forbidden(*args, **kwargs):
        pytest.fail("no subprocess may be launched when the validation sandbox is missing")
    monkeypatch.setattr(completion.asyncio, "create_subprocess_exec", forbidden)
    with pytest.raises(completion.ValidationUnavailable, match="bwrap.*not installed"):
        asyncio.run(completion.validate_command(tmp_path, ["/usr/bin/true"], tmp_path))


def test_bwrap_disappearing_after_preflight_requires_review_without_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(completion.shutil, "which", lambda _: "/missing/bwrap")
    attempts = []
    async def vanished(*argv, **kwargs):
        attempts.append(argv)
        raise FileNotFoundError("bwrap disappeared")
    monkeypatch.setattr(completion.asyncio, "create_subprocess_exec", vanished)
    with pytest.raises(completion.ValidationUnavailable):
        asyncio.run(completion.validate_command(tmp_path, ["/usr/bin/true"], tmp_path))
    assert len(attempts) == 1 and attempts[0][0] == "/missing/bwrap"


@pytest.mark.parametrize("backend", ["exec", "app-server"])
def test_missing_bwrap_requires_review_without_retry_or_releasing_dependents(
        backend, git_repo, make_manager, fake_codex_state, tmp_path, monkeypatch):
    monkeypatch.setattr(completion.shutil, "which", lambda _: None)
    marker = tmp_path / "unsafe-validation-ran"
    m = make_manager(backend=backend, retry_backoff_seconds=(0.01,))
    async def scenario():
        m.scheduler.start(0.02)
        try:
            parent = await m.create_task(repository=str(git_repo), prompt="ok", completion_contract={
                "required_paths": ["README.md"], "validation_commands": [["/usr/bin/touch", str(marker)], ["/usr/bin/true"]]})
            child = await m.create_task(repository=str(git_repo), prompt="ok", depends_on=[parent["id"]])
            result = await done(m, parent)
            assert result["status"] == "completed" and result["exit_code"] == (0 if backend == "exec" else None)
            assert result["task_outcome"] == "needs_review" and result["outcome_reason"] == completion.BWRAP_MISSING_REASON
            checks = json.loads(result["completion_checks"])
            assert next(c for c in checks if c["name"] == "required artifact: README.md")["status"] == "pass"
            assert any(c["status"] == "unavailable" for c in checks)
            assert sum(c["status"] == "skipped" for c in checks) == 2
            await asyncio.sleep(0.1)
            m.tick()
            assert m.get(parent["id"])["retry_count"] == 0 and m.get(parent["id"])["next_retry_at"] is None
            assert m.get(child["id"])["status"] == "waiting_dependencies" and m.db.list_attempts(child["id"]) == []
            assert len(m.db.list_attempts(parent["id"])) == 1 and not marker.exists()
            if backend == "exec":
                assert len(calls(fake_codex_state)) == 1
            else:
                assert len([c for c in calls(fake_codex_state) if c["method"] == "turn/start"]) == 1
        finally:
            await m.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("rules,outcome", [({}, "success"), ({"required_paths": ["out.txt"]}, "success"),
    ({"require_any_change": True}, "success"), ({"required_paths": ["missing.bin"]}, "incomplete"),
    ({"require_commit": True}, "incomplete")])
def test_without_validation_commands_bwrap_missing_does_not_change_decision(
        rules, outcome, git_repo, make_manager, monkeypatch):
    def forbidden_lookup(_):
        pytest.fail("deterministic-only contracts do not require bwrap preflight")
    monkeypatch.setattr(completion.shutil, "which", forbidden_lookup)
    m = make_manager()
    async def scenario():
        try:
            parent = await m.create_task(repository=str(git_repo), prompt="ok", completion_contract=rules)
            assert (await done(m, parent))["task_outcome"] == outcome
        finally:
            await m.shutdown()
    asyncio.run(scenario())


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="requires the real read-only bwrap sandbox")
def test_bwrap_restored_rerun_checks_without_reexecuting_codex(git_repo, make_manager, fake_codex_state, monkeypatch):
    installed_bwrap = shutil.which("bwrap")
    monkeypatch.setattr(completion.shutil, "which", lambda _: None)
    m = make_manager()
    async def scenario():
        try:
            parent = await m.create_task(repository=str(git_repo), prompt="ok", completion_contract={
                "required_paths": ["out.txt"], "validation_commands": [["/usr/bin/test", "-f", "out.txt"]]})
            child = await m.create_task(repository=str(git_repo), prompt="ok", depends_on=[parent["id"]])
            first = await done(m, parent)
            assert first["task_outcome"] == "needs_review" and m.get(child["id"])["status"] == "waiting_dependencies"
            monkeypatch.setattr(completion.shutil, "which", lambda _: installed_bwrap)
            result = await m.rerun_completion(parent["id"])
            assert result["task_outcome"] == "success", result["completion_checks"]
            assert result["codex_thread_id"] == first["codex_thread_id"]
            assert len(m.db.list_attempts(parent["id"])) == 1
            await done(m, child)
            assert len(calls(fake_codex_state)) == 2  # initial parent and released child only
        finally:
            await m.shutdown()
    asyncio.run(scenario())


def test_startup_and_options_report_current_bwrap_capability(settings, monkeypatch):
    monkeypatch.setattr(completion.shutil, "which", lambda _: None)
    app = create_app(settings, FakeRunner())
    assert app.state.completion_validation["bwrap_available"] is False
    with TestClient(app) as client:
        info = client.get("/api/options").json()["completion_validation"]
        assert not info["bwrap_available"] and "manual review" in info["message"]
        monkeypatch.setattr(completion.shutil, "which", lambda _: "/usr/bin/bwrap")
        assert client.get("/api/options").json()["completion_validation"]["bwrap_available"] is True
