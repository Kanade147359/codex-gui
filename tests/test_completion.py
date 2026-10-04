"""Completion gate regression and integration tests: real subprocesses, isolated Git and DB."""
import asyncio
import json
from pathlib import Path
import shutil
import subprocess
import threading

import pytest
from fastapi.testclient import TestClient

from app import completion
from app.database import Database
from app.main import create_app
from app.task_manager import TaskError
from conftest import FakeRunner, wait_for


async def done(m, task):
    return await wait_for(lambda: m.get(task["id"])["status"] == "completed" and m.get(task["id"]))


def calls(state):
    path = state / "invocations.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.mark.parametrize("backend", ["exec", "app-server"])
@pytest.mark.parametrize("status,outcome", [("SUCCESS", "success"), ("BLOCKED", "blocked"),
    ("NEEDS_INPUT", "needs_input"), ("PARTIAL", "incomplete"), ("MISSING", "needs_review")])
def test_structured_outcomes_never_retry_or_release_non_success(backend, status, outcome, git_repo, make_manager):
    m = make_manager(backend=backend, scheduler_interval_seconds=0.02, retry_backoff_seconds=(0.01,))
    async def scenario():
        m.scheduler.start(0.02)
        try:
            a = await m.create_task(repository=str(git_repo), prompt=f"semantic {status} Required target ABI is unspecified.", name="P17")
            b = await m.create_task(repository=str(git_repo), prompt="ok", name="P18", depends_on=[a["id"]])
            result = await done(m, a)
            assert result["task_outcome"] == outcome
            assert result["status"] == "completed"
            assert result["exit_code"] == (0 if backend == "exec" else None)
            assert result["retry_count"] == 0 and result["next_retry_at"] is None
            if status == "SUCCESS":
                await done(m, b)
            else:
                for _ in range(20):
                    m.tick()
                    await asyncio.sleep(0.01)
                child = m.present_task(m.get(b["id"]))
                assert child["status"] == "waiting_dependencies" and child["deps_done"] == 0
                assert child["dependencies"][0]["task_outcome"] == outcome
                assert outcome.replace("_", " ") in child["status_detail"]
                assert m.db.list_attempts(b["id"]) == []
        finally:
            await m.shutdown()
    asyncio.run(scenario())


def test_exit_zero_missing_artifact_integration(git_repo, settings, fake_codex_state):
    """A real CLI-shaped subprocess exits 0 and claims SUCCESS; the HTTP/scheduler must keep B paused."""
    from test_ui_recovery import wait_status
    with TestClient(create_app(settings, FakeRunner())) as client:
        a = client.post("/api/tasks", json={"repository": str(git_repo), "prompt": "semantic SUCCESS Done.",
            "name": "P17", "completion_contract": {"required_paths": ["required.bin"],
            "validation_commands": [["/usr/bin/true"]]}}).json()
        b = client.post("/api/tasks", json={"repository": str(git_repo), "prompt": "ok", "name": "P18", "depends_on": [a["id"]]}).json()
        result = wait_status(client, a["id"], {"completed"})
        child = client.get(f"/api/tasks/{b['id']}").json()
        assert result["execution_status"] == "completed" and result["exit_code"] == 0
        assert result["task_outcome"] == "incomplete"
        assert "required.bin" in result["outcome_reason"]
        assert any(c["status"] == "skipped" and c["name"].startswith("validation:") for c in result["completion_checks"])
        assert child["status"] == "waiting_dependencies" and child["deps_done"] == 0
        assert result["retry_count"] == 0 and result["next_retry_at"] is None
        assert len(calls(fake_codex_state)) == 1
        assert not Path(b["worktree"]).exists()


@pytest.mark.parametrize("backend", ["exec", "app-server"])
def test_additional_instruction_success_releases_once_in_same_thread(backend, git_repo, make_manager, fake_codex_state):
    m = make_manager(backend=backend)
    async def scenario():
        m.scheduler.start(0.02)
        try:
            a = await m.create_task(repository=str(git_repo), prompt="semantic NEEDS_INPUT ABI unspecified.")
            b = await m.create_task(repository=str(git_repo), prompt="ok", depends_on=[a["id"]])
            first = await done(m, a)
            assert m.get(b["id"])["status"] == "waiting_dependencies"
            await m.send_instruction(a["id"], "semantic SUCCESS ABI specified; work completed.")
            last = await done(m, a)
            for _ in range(40):
                m.tick()
                m._evaluate_dependents(a["id"])
            await done(m, b)
            assert last["task_outcome"] == "success"
            assert last["codex_thread_id"] == first["codex_thread_id"]
            assert last["worktree"] == first["worktree"] and last["branch"] == first["branch"]
            assert len(m.db.list_attempts(b["id"])) == 1
            if backend == "exec":
                assert len(calls(fake_codex_state)) == 3
                assert calls(fake_codex_state)[1]["argv"] == ["resume", first["codex_thread_id"]]
            else:
                starts = [c for c in calls(fake_codex_state) if c["method"] == "turn/start"]
                assert len(starts) == 3 and all(c["params"]["outputSchema"] == completion.SCHEMA for c in starts)
        finally:
            await m.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("rules", [
    {"required_changed_paths": ["README.md"]}, {"require_commit": True},
])
def test_success_claim_cannot_override_contract_failure(rules, git_repo, make_manager):
    m = make_manager()
    async def scenario():
        try:
            a = await m.create_task(repository=str(git_repo), prompt="ok", completion_contract=rules)
            result = await done(m, a)
            assert result["task_outcome"] == "incomplete" and result["outcome_source"] == "contract"
        finally:
            await m.shutdown()
    asyncio.run(scenario())


def test_no_change_and_missing_result_are_conservative(git_repo, make_manager):
    m = make_manager(backend="app-server")  # fake app-server produces SUCCESS without modifying files
    async def scenario():
        try:
            a = await m.create_task(repository=str(git_repo), prompt="ok", completion_contract={"require_any_change": True})
            assert (await done(m, a))["task_outcome"] == "incomplete"
            b = await m.create_task(repository=str(git_repo), prompt="semantic MISSING Unknown.",
                                    completion_contract={"required_paths": ["README.md"]})
            assert (await done(m, b))["task_outcome"] == "needs_review"
        finally:
            await m.shutdown()
    asyncio.run(scenario())


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="read-only validation requires bwrap")
def test_all_checks_pass_commit_and_rerun(git_repo, make_manager):
    m = make_manager()
    async def scenario():
        try:
            a = await m.create_task(repository=str(git_repo), prompt="ok", completion_contract={
                "required_paths": ["out.txt"], "required_changed_paths": ["out.txt"], "require_any_change": True,
                "require_commit": True, "validation_commands": [["/usr/bin/test", "-f", "out.txt"]]})
            assert (await done(m, a))["task_outcome"] == "incomplete"
            await m.commit(a["id"], "required commit")
            result = await m.rerun_completion(a["id"])
            assert result["task_outcome"] == "success", result["completion_checks"]
            assert result["evidence_result"] == "PASS" and result["approval_source"] == "auto_evidence"
            assert len(m.db.completion_approvals(a["id"])) == 1
            assert all(c["status"] == "pass" for c in result["completion_checks"])
            assert len(m.db.list_attempts(a["id"])) == 1  # no extra Codex run
        finally:
            await m.shutdown()
    asyncio.run(scenario())


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="read-only validation requires bwrap")
@pytest.mark.parametrize("command", [["/usr/bin/false"], ["git", "reset", "--hard", "HEAD"]])
def test_validation_failure_and_git_is_not_destroyed(command, git_repo, make_manager):
    m = make_manager()
    async def scenario():
        try:
            a = await m.create_task(repository=str(git_repo), prompt="ok", completion_contract={"validation_commands": [command]})
            result = await done(m, a)
            assert result["task_outcome"] == "incomplete"
            assert (Path(a["worktree"]) / "out.txt").read_text() == "hello\n"
            assert subprocess.check_output(["git", "-C", a["worktree"], "status", "--porcelain"]).decode() == "?? out.txt\n"
        finally:
            await m.shutdown()
    asyncio.run(scenario())


def test_manual_success_confirmation_audit_and_rerun_revoke(git_repo, make_manager, db):
    m = make_manager()
    async def scenario():
        try:
            a = await m.create_task(repository=str(git_repo), prompt="semantic BLOCKED Required spec not found.",
                                    completion_contract={"manual_approval": True})
            await done(m, a)
            with pytest.raises(TaskError) as exc:
                m.override_completion(a["id"], "success", "Reviewed the work.", False)
            assert exc.value.code == "confirmation_required" and db.completion_overrides(a["id"]) == []
            result = m.override_completion(a["id"], "success", "Reviewed the work.", True)
            assert result["manual_override"] and result["task_outcome"] == "success"
            audit = db.completion_overrides(a["id"])
            assert len(audit) == 1 and audit[0]["previous_outcome"] == "blocked"
            assert audit[0]["actor"] == result["manual_override_by"] and audit[0]["created_at"] == result["manual_override_at"]
            result = await m.rerun_completion(a["id"])
            assert result["task_outcome"] == "blocked" and not result["manual_override"]
            assert db.completion_overrides(a["id"]) == audit
        finally:
            await m.shutdown()
    asyncio.run(scenario())


def test_manual_approval_pauses_dependents_and_override_releases(git_repo, make_manager):
    m = make_manager()
    async def scenario():
        try:
            a = await m.create_task(repository=str(git_repo), prompt="ok", completion_contract={"manual_approval": True})
            b = await m.create_task(repository=str(git_repo), prompt="ok", depends_on=[a["id"]])
            assert (await done(m, a))["task_outcome"] == "needs_review"
            assert m.get(b["id"])["status"] == "waiting_dependencies"
            m.override_completion(a["id"], "success", "User reviewed artifacts.", True)
            await done(m, b)
        finally:
            await m.shutdown()
    asyncio.run(scenario())


def test_rerun_serializes_with_instruction_and_manual_override(git_repo, make_manager, monkeypatch):
    m = make_manager()
    async def scenario():
        try:
            a = await m.create_task(repository=str(git_repo), prompt="ok")
            await done(m, a)
            entered, release = asyncio.Event(), asyncio.Event()
            original = completion.check
            async def slow(*args):
                entered.set()
                await release.wait()
                return await original(*args)
            monkeypatch.setattr(completion, "check", slow)
            check = asyncio.create_task(m.rerun_completion(a["id"]))
            await entered.wait()
            assert m.get(a["id"])["task_outcome"] == "needs_review"
            for op in (lambda: m.override_completion(a["id"], "success", "reviewed", True),
                       lambda: m.set_completion_contract(a["id"], {})):
                with pytest.raises(TaskError):
                    op()
            with pytest.raises(TaskError):
                await m.send_instruction(a["id"], "ok")
            with pytest.raises(TaskError):
                await m.rerun_completion(a["id"])
            release.set()
            assert (await check)["task_outcome"] == "success"
        finally:
            await m.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("value", ['{"status":[],"reason":"x"}', '{"status":"SUCCESS"}',
    'prefix {"status":"SUCCESS","reason":"x"}', '{"status":"SUCCESS","reason":""}',
    '{"status":"SUCCESS","reason":"x","extra":1}',
    '{"status":"BLOCKED","status":"SUCCESS","reason":"x"}'])
def test_invalid_result_never_becomes_success(value):
    assert completion.parse_result(value) is None


@pytest.mark.parametrize("rules", [{"required_paths": ["../outside"]}, {"required_paths": ["/absolute"]},
    {"required_changed_paths": [".git/config"]}, {"require_commit": "yes"}, {"validation_commands": [[]]}])
def test_invalid_contract(rules):
    with pytest.raises(ValueError):
        completion.contract(rules)


def test_outcome_and_override_survive_restart_with_atomic_dependency_claim(tmp_path):
    path = tmp_path / "gate.db"
    db = Database(path)
    fields = dict(repository="/isolated", worktree="/isolated/wt", branch="b", base_ref="main", base_sha="a",
                  prompt="p", created_at="2026-01-01T00:00:00Z")
    db.create_task(id="a", name="P17", status="completed", task_outcome="needs_input", **fields)
    db.create_task(id="b", name="P18", status="waiting_dependencies", depends_on=["a"], **fields)
    db.close()
    db = Database(path)
    assert db.get_task("a")["task_outcome"] == "needs_input" and not db.queue_if_ready("b")
    db.override_completion("a", "success", "ABI supplied and checked", "local-user")
    db.close()
    databases = [Database(path) for _ in range(6)]
    barrier, wins = threading.Barrier(6), []
    def race(d):
        barrier.wait()
        wins.append((d.queue_if_ready("b"), d.claim_queued("b", "scheduler")))
    threads = [threading.Thread(target=race, args=(d,)) for d in databases]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(q for q, _ in wins) == 1 and sum(c for _, c in wins) == 1
    assert databases[0].completion_overrides("a")[0]["actor"] == "local-user"
    for d in databases:
        d.close()


def test_symlink_artifact_cannot_escape_worktree(git_repo, make_manager, tmp_path):
    m = make_manager()
    async def scenario():
        try:
            a = await m.create_task(repository=str(git_repo), prompt="ok")
            await done(m, a)
            outside = tmp_path / "outside.txt"
            outside.write_text("not an artifact")
            (Path(a["worktree"]) / "artifact").symlink_to(outside)
            m.set_completion_contract(a["id"], {"required_paths": ["artifact"]})
            result = await m.rerun_completion(a["id"])
            assert result["task_outcome"] == "incomplete"
        finally:
            await m.shutdown()
    asyncio.run(scenario())


def test_dependency_revoked_while_child_waits_for_runner_slot(git_repo, make_manager, fake_codex_state):
    m = make_manager()
    async def scenario():
        try:
            parent = await m.create_task(repository=str(git_repo), prompt="ok")
            await done(m, parent)
            m._slots = asyncio.Semaphore(0)
            child = await m.create_task(repository=str(git_repo), prompt="ok", depends_on=[parent["id"]])
            await asyncio.sleep(0.03)  # child claimed, held in the execution queue
            m.override_completion(parent["id"], "blocked", "Required review is missing.", False)
            m._slots.release()
            await wait_for(lambda: m.get(child["id"])["status"] == "waiting_dependencies")
            assert len(calls(fake_codex_state)) == 1
            m.override_completion(parent["id"], "success", "Review finished.", True)
            await done(m, child)
            assert len(calls(fake_codex_state)) == 2
        finally:
            await m.shutdown()
    asyncio.run(scenario())


def test_gate_infrastructure_error_is_review_not_automatic_retry(git_repo, make_manager, monkeypatch):
    m = make_manager()
    async def broken(*args):
        raise OSError("checker unavailable")
    monkeypatch.setattr(completion, "check", broken)
    async def scenario():
        try:
            parent = await m.create_task(repository=str(git_repo), prompt="ok")
            result = await done(m, parent)
            assert result["task_outcome"] == "needs_review" and "checker unavailable" in result["outcome_reason"]
            assert result["retry_count"] == 0 and result["next_retry_at"] is None
        finally:
            await m.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["running", "completed"])
def test_restart_during_completion_checks_never_retries(status, git_repo, make_manager):
    m = make_manager()
    head = subprocess.check_output(["git", "-C", str(git_repo), "rev-parse", "HEAD"]).decode().strip()
    m.db.create_task(id="a", name="parent", repository=str(git_repo), worktree=str(git_repo), branch="main",
        base_ref="main", base_sha=head, prompt="ok", created_at="2026-01-01T00:00:00Z", status=status,
        completion_pending=1, exit_code=0, semantic_result=json.dumps({"status": "SUCCESS", "reason": "Done."}))
    m.db.create_task(id="b", name="child", repository=str(git_repo), worktree=str(git_repo), branch="main",
        base_ref="main", base_sha=head, prompt="ok", created_at="2026-01-01T00:00:00Z", status="waiting_dependencies",
        depends_on=["a"])
    assert "a" in m.recover()
    result = m.get("a")
    assert result["status"] == "completed" and result["task_outcome"] == "needs_review"
    assert result["retry_count"] == 0 and result["next_retry_at"] is None and not result["completion_pending"]
    assert not m.db.queue_if_ready("b")


def test_compaction_preserves_semantic_pause(git_repo, make_manager):
    m = make_manager(backend="app-server")
    async def scenario():
        try:
            parent = await m.create_task(repository=str(git_repo), prompt="semantic NEEDS_INPUT Missing ABI.")
            child = await m.create_task(repository=str(git_repo), prompt="ok", depends_on=[parent["id"]])
            first = await done(m, parent)
            await m.compact(parent["id"])
            last = await done(m, parent)
            assert last["task_outcome"] == "needs_input" and last["semantic_result"] == first["semantic_result"]
            assert m.get(child["id"])["status"] == "waiting_dependencies"
        finally:
            await m.shutdown()
    asyncio.run(scenario())


def test_manual_block_and_cancel_descendants_api(git_repo, settings):
    from test_ui_recovery import wait_status
    with TestClient(create_app(settings, FakeRunner())) as c:
        parent = c.post("/api/tasks", json={"repository": str(git_repo), "prompt": "semantic NEEDS_INPUT Missing ABI."}).json()
        child = c.post("/api/tasks", json={"repository": str(git_repo), "prompt": "ok", "depends_on": [parent["id"]]}).json()
        grandchild = c.post("/api/tasks", json={"repository": str(git_repo), "prompt": "ok", "depends_on": [child["id"]]}).json()
        wait_status(c, parent["id"], {"completed"})
        base = f"/api/tasks/{parent['id']}"
        assert c.post(base + "/completion/override", json={"outcome": "success", "reason": "Reviewed"}).status_code == 409
        blocked = c.post(base + "/completion/override", json={"outcome": "blocked", "reason": "Missing requirements."}).json()
        assert blocked["task_outcome"] == "blocked" and blocked["manual_override_by"]
        result = c.post(base + "/dependents/cancel").json()
        assert set(result["cancelled"]) == {child["id"], grandchild["id"]}
        assert c.get(f"/api/tasks/{child['id']}").json()["status"] == "stopped"
        assert c.get(f"/api/tasks/{grandchild['id']}").json()["status"] == "stopped"


def test_lost_app_server_settles_without_querying_a_new_server(git_repo, make_manager, monkeypatch):
    m = make_manager(backend="app-server")
    original = m.read_limits
    async def limits(reason="poll", *args, **kwargs):
        assert reason != "turn_end", "a lost server cannot supply this turn's quota snapshot"
        return await original(reason, *args, **kwargs)
    monkeypatch.setattr(m, "read_limits", limits)
    async def scenario():
        try:
            parent = await m.create_task(repository=str(git_repo), prompt="die", auto_retry=False)
            result = await wait_for(lambda: m.get(parent["id"])["status"] == "failed" and m.get(parent["id"]))
            assert result["retry_count"] == 0 and "exited" in result["status_detail"]
        finally:
            await m.shutdown()
    asyncio.run(scenario())
