"""Semantic continuation uses isolated fake CLI processes, never real model usage."""

import asyncio
import json
from pathlib import Path
import sys

import pytest

from app.autopilot import environment
from app.autopilot.ledger import Ledger, migrate
from app.autopilot.policy import canonical, manifest
from autopilot_helpers import FixtureProvider, ImplementationRunner, drive, plan, register
from conftest import FakeRunner
from test_autopilot_execution_mentor import controlled_install


class SemanticRunner(ImplementationRunner):
    """Only C emits the requested semantic outcome; independent leaves use normal fixtures."""

    def build_command(self, task, resume_thread=None):
        if task["name"].endswith(": C"):
            command = FakeRunner().build_command(task, resume_thread)
            if resume_thread and task["last_prompt"].startswith("Autopilot correction"):
                shim = ("import os, runpy, sys; os.environ['FAKE_CODEX_FORCE_MODE'] = 'ok'; "
                        "sys.argv = sys.argv[1:]; runpy.run_path(sys.argv[0], run_name='__main__')")
                return [sys.executable, "-c", shim, *command[1:]]
            return command
        return super().build_command(task, resume_thread)


class HoldingReview(FixtureProvider):
    def __init__(self):
        super().__init__("claude")
        self.ready = asyncio.Event()
        self.release = asyncio.Event()

    async def start_review(self, candidate, policy, root, attempt, on_identity):
        result = await super().start_review(candidate, policy, root, attempt, on_identity)
        if candidate["payload"]["requirements"]["node"] == "C":
            self.ready.set()
            await self.release.wait()
        return result


def semantic_plan(repo, status="PARTIAL", names=("C",)):
    raw = plan(repo, names)
    node = next(node for node in raw["nodes"] if node["id"] == "C")
    node.update(
        prompt=f"semantic {status} Finish the remaining artifact using the fixed task requirements",
        paths=["out.txt"],
        validation=dict(
            commands=[["python3", "-c", "from pathlib import Path; assert Path('out.txt').exists()"]],
            artifacts=["out.txt"],
        ),
    )
    raw["policy"]["review_slots"] = 2
    return raw


def invocations(state):
    path = state / "invocations.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def fixes(ap, identity):
    return ap.ledger.rows("operations", "node_id=? AND kind='fix'", (identity + ":C",))


def hold_repair_preflight(monkeypatch, ap, *, ignore_cancel=False):
    ready, release = asyncio.Event(), asyncio.Event()
    previous = environment.preflight

    async def preflight(root, profile, runtime_mounts=None):
        nodes = ap.ledger.rows("nodes", "state='reserving_fix'")
        if any(ap.manager.db.get_task(node["task_id"])["worktree"] == str(root) for node in nodes):
            ready.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                if not ignore_cancel:
                    raise
                # A completed external preflight may arrive as the old owner turn is cancelled.
                await release.wait()
        return await previous(root, profile, runtime_mounts)

    monkeypatch.setattr(environment, "preflight", preflight)
    return ready, release


def test_semantic_recovery_policy_is_enabled_by_default_and_requires_boolean(git_repo):
    raw = semantic_plan(git_repo)
    assert manifest(raw)["policy"]["semantic_recovery"] is True
    raw["policy"]["semantic_recovery"] = False
    assert manifest(raw)["policy"]["semantic_recovery"] is False
    for invalid in ("true", 1, None):
        raw["policy"]["semantic_recovery"] = invalid
        with pytest.raises(ValueError, match="semantic_recovery"):
            manifest(raw)


def test_stale_scheduler_snapshot_cannot_claim_a_new_owner_generation(make_manager, git_repo, controlled_install):
    async def scenario():
        manager = make_manager()
        ap = controlled_install(manager)
        try:
            identity = await register(ap, semantic_plan(git_repo))
            stale = ap.ledger.get("nodes", identity + ":C")
            with ap.ledger.transaction() as connection:
                connection.execute("UPDATE ap_nodes SET generation=generation+1 WHERE id=?", (stale["id"],))
            ap._spawn(stale, "pending", "launching", ap._launch)
            assert not ap.jobs
            current = ap.ledger.get("nodes", stale["id"])
            assert current["state"] == "pending" and current["generation"] == stale["generation"] + 1
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("status,outcome", [
    ("PARTIAL", "incomplete"), ("BLOCKED", "blocked"),
    ("NEEDS_INPUT", "needs_input"), ("MISSING", "needs_review"),
])
def test_semantic_recovery_resumes_same_work_before_independent_acceptance(
    make_manager, git_repo, fake_codex_state, controlled_install, monkeypatch, status, outcome,
):
    async def scenario():
        manager = make_manager()
        provider = HoldingReview()
        ap = controlled_install(manager, provider)
        manager.runner = SemanticRunner()
        original_submit = ap._submit
        first_result, release = asyncio.Event(), asyncio.Event()
        recorded = []

        async def submit(node):
            if not recorded:
                recorded.append(manager.db.get_task(node["task_id"]))
                first_result.set()
                await release.wait()
            await original_submit(node)

        monkeypatch.setattr(ap, "_submit", submit)
        try:
            raw = semantic_plan(git_repo, status, ("C", "E"))
            raw["nodes"][1]["depends_on"] = ["C"]
            identity = await register(ap, raw)
            await drive(manager, first_result.is_set)
            original = recorded[0]
            assert original["status"] == "completed" and original["task_outcome"] == outcome
            assert not ap.ledger.rows("acceptances") and not ap.ledger.rows("reviews")
            # The next turn must preserve already completed, uncommitted work.
            (Path(original["worktree"]) / "out.txt").write_text("retained earlier work\n")
            release.set()
            await drive(manager, provider.ready.is_set)
            current = manager.db.get_task(original["id"])
            assert current["codex_thread_id"] == original["codex_thread_id"]
            assert current["worktree"] == original["worktree"]
            assert current["task_outcome"] == "needs_review"
            assert not ap.ledger.rows("acceptances")
            assert ap.ledger.get("nodes", identity + ":E")["task_id"] is None
            assert len(fixes(ap, identity)) == 1
            assert ap.ledger.rows("validation", "state='PASS'")
            assert (Path(current["worktree"]) / "out.txt").read_text().startswith("retained earlier work\n")
            provider.release.set()
            await drive(manager, lambda: ap.ledger.view(identity)["state"] == "completed")
            calls = invocations(fake_codex_state)
            assert len(calls) == 2
            assert calls[1]["argv"] == ["resume", original["codex_thread_id"]]
            assert all(call["cwd"] == original["worktree"] for call in calls)
            assert len(manager.db.list_turns(original["id"])) == 2
            assert len(ap.ledger.rows("acceptances")) == 2
            assert manager.db.get_task(original["id"])["task_outcome"] == "success"
        finally:
            release.set()
            provider.release.set()
            await manager.shutdown()

    asyncio.run(scenario())


def test_existing_blocked_result_recovers_after_upgrade_without_new_task(
    make_manager, git_repo, fake_codex_state, controlled_install,
):
    async def scenario():
        manager = make_manager()
        ap = controlled_install(manager)
        manager.runner = SemanticRunner()
        try:
            raw = semantic_plan(git_repo, names=("C", "E"))
            raw["nodes"][1]["depends_on"] = ["C"]
            raw["policy"]["semantic_recovery"] = False
            identity = await register(ap, raw)
            await drive(manager, lambda: ap.ledger.get("nodes", identity + ":C")["state"] == "blocked_result")
            old_node = ap.ledger.get("nodes", identity + ":C")
            old_task = manager.db.get_task(old_node["task_id"])
            workflow = ap.ledger.get("workflows", identity)
            del workflow["plan"]["policy"]["semantic_recovery"]
            with ap.ledger.transaction() as connection:
                connection.execute("UPDATE ap_workflows SET plan=? WHERE id=?", (canonical(workflow["plan"]), identity))
            await ap.shutdown()
            replacement = controlled_install(manager)
            manager.runner = SemanticRunner()
            await drive(manager, lambda: replacement.ledger.view(identity)["state"] == "completed")
            current = replacement.ledger.get("nodes", old_node["id"])
            assert current["task_id"] == old_task["id"]
            task = manager.db.get_task(current["task_id"])
            assert (task["worktree"], task["codex_thread_id"]) == (old_task["worktree"], old_task["codex_thread_id"])
            assert len(fixes(replacement, identity)) == 1
            assert len(invocations(fake_codex_state)) == 2
            assert len(manager.db.list_tasks()) == 2
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


_REPEATED_FAILURE = """
import os, pathlib, runpy, sys
progress = sys.argv[1] == '1'
if progress:
    path = pathlib.Path('out.txt')
    revision = int(path.read_text()) + 1 if path.exists() else 1
    path.write_text(str(revision))
    reason = 'Finish the remaining artifact requirement for revision ' + str(revision)
else:
    reason = 'The required artifact is still unfinished'
os.environ['FAKE_CODEX_FORCE_MODE'] = 'semantic PARTIAL ' + reason
sys.argv = sys.argv[2:]
runpy.run_path(sys.argv[0], run_name='__main__')
"""


class RepeatedFailureRunner(FakeRunner):
    def __init__(self, progress):
        super().__init__()
        self.progress = progress

    def build_command(self, task, resume_thread=None):
        return [sys.executable, "-c", _REPEATED_FAILURE, str(int(self.progress)),
                *super().build_command(task, resume_thread)[1:]]


@pytest.mark.parametrize("progress,no_progress_limit,expected,rounds", [
    (False, 2, "blocked_no_progress", 1), (True, 10, "blocked_budget", 3),
])
def test_semantic_recovery_limits_persist_after_restart(
    make_manager, git_repo, fake_codex_state, controlled_install, progress, no_progress_limit, expected, rounds,
):
    async def scenario():
        manager = make_manager()
        ap = controlled_install(manager)
        manager.runner = RepeatedFailureRunner(progress)
        try:
            raw = semantic_plan(git_repo)
            raw["policy"].update(no_progress_limit=no_progress_limit, max_fix_rounds=3)
            identity = await register(ap, raw)
            await drive(manager, lambda: ap.ledger.get("nodes", identity + ":C")["state"] == expected)
            stopped = ap.ledger.get("nodes", identity + ":C")
            assert stopped["fix_rounds"] == rounds
            assert len(fixes(ap, identity)) == rounds
            assert len(invocations(fake_codex_state)) == rounds + 1
            assert manager.db.get_task(stopped["task_id"])["task_outcome"] == "incomplete"
            latest = json.loads(manager.db.get_task(stopped["task_id"])["semantic_result"])
            assert latest["reason"] in ap.ledger.view(identity)["review_summary"]["text"]
            assert not ap.ledger.rows("reviews") and not ap.ledger.rows("acceptances")
            await ap.shutdown()
            replacement = controlled_install(manager)
            manager.runner = RepeatedFailureRunner(progress)
            for _ in range(5):
                manager.tick()
                await asyncio.sleep(0)
            restored = replacement.ledger.get("nodes", stopped["id"])
            assert (restored["state"], restored["fix_rounds"], restored["no_progress"]) == (
                expected, rounds, stopped["no_progress"],
            )
            assert len(invocations(fake_codex_state)) == rounds + 1
            assert len(fixes(replacement, identity)) == rounds
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_independent_leaf_continues_during_semantic_repair_and_dependent_waits_for_review(
    make_manager, git_repo, fake_codex_state, controlled_install, monkeypatch,
):
    async def scenario():
        manager = make_manager()
        provider = HoldingReview()
        ap = controlled_install(manager, provider)
        manager.runner = SemanticRunner()
        ready, release = hold_repair_preflight(monkeypatch, ap)
        try:
            raw = semantic_plan(git_repo, names=("C", "E", "U"))
            raw["nodes"][1]["depends_on"] = ["C"]
            identity = await register(ap, raw)
            await drive(manager, lambda: ready.is_set() and ap.ledger.get("nodes", identity + ":U")["state"] == "accepted")
            assert ap.ledger.get("nodes", identity + ":E")["task_id"] is None
            assert len(invocations(fake_codex_state)) == 1
            release.set()
            await drive(manager, provider.ready.is_set)
            assert ap.ledger.get("nodes", identity + ":E")["task_id"] is None
            provider.release.set()
            await drive(manager, lambda: ap.ledger.view(identity)["state"] == "completed")
            dependent = ap.ledger.get("nodes", identity + ":E")
            assert len(manager.db.list_turns(dependent["task_id"])) == 1
            assert len(ap.ledger.rows("acceptances")) == 3
        finally:
            release.set()
            provider.release.set()
            await manager.shutdown()

    asyncio.run(scenario())


def test_concurrent_semantic_fix_reservation_creates_one_turn(
    make_manager, git_repo, fake_codex_state, controlled_install, monkeypatch,
):
    async def scenario():
        manager = make_manager()
        ap = controlled_install(manager)
        manager.runner = SemanticRunner()
        ready, release = hold_repair_preflight(monkeypatch, ap)
        duplicate = None
        try:
            identity = await register(ap, semantic_plan(git_repo))
            await drive(manager, ready.is_set)
            node = ap.ledger.get("nodes", identity + ":C")
            duplicate = asyncio.create_task(ap._fix(node))
            await asyncio.sleep(0)
            release.set()
            await duplicate
            await drive(manager, lambda: ap.ledger.view(identity)["state"] == "completed")
            assert len(fixes(ap, identity)) == 1
            assert len(invocations(fake_codex_state)) == 2
            assert len(manager.db.list_turns(node["task_id"])) == 2
            assert ap.ledger.get("nodes", node["id"])["fix_rounds"] == 1
        finally:
            release.set()
            if duplicate:
                await asyncio.gather(duplicate, return_exceptions=True)
            await manager.shutdown()

    asyncio.run(scenario())


def test_reserving_semantic_fix_recovers_after_restart_without_duplicate_turn(
    make_manager, git_repo, fake_codex_state, controlled_install, monkeypatch,
):
    async def scenario():
        manager = make_manager()
        ap = controlled_install(manager)
        manager.runner = SemanticRunner()
        ready, release = hold_repair_preflight(monkeypatch, ap)
        try:
            identity = await register(ap, semantic_plan(git_repo))
            await drive(manager, ready.is_set)
            before = ap.ledger.get("nodes", identity + ":C")
            thread = manager.db.get_task(before["task_id"])["codex_thread_id"]
            assert before["state"] == "reserving_fix" and not fixes(ap, identity)
            await ap.shutdown()
            release.set()
            replacement = controlled_install(manager)
            manager.runner = SemanticRunner()
            await drive(manager, lambda: replacement.ledger.view(identity)["state"] == "completed")
            after = replacement.ledger.get("nodes", before["id"])
            assert after["task_id"] == before["task_id"] and after["fix_rounds"] == 1
            assert len(fixes(replacement, identity)) == 1
            assert len(invocations(fake_codex_state)) == 2
            assert manager.db.get_task(after["task_id"])["codex_thread_id"] == thread
        finally:
            release.set()
            await manager.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("state", ["fix_queued", "reserving_fix"])
def test_legacy_pending_repair_keeps_its_already_consumed_no_progress_count(
    make_manager, git_repo, fake_codex_state, controlled_install, monkeypatch, state,
):
    async def scenario():
        manager = make_manager()
        ap = controlled_install(manager)
        manager.runner = SemanticRunner()
        ready, release = hold_repair_preflight(monkeypatch, ap)
        try:
            # Missing-result corrections existed before semantic recovery was introduced.
            identity = await register(ap, semantic_plan(git_repo, "MISSING"))
            await drive(manager, ready.is_set)
            before = ap.ledger.get("nodes", identity + ":C")
            task = manager.db.get_task(before["task_id"])
            assert not fixes(ap, identity)
            await ap.shutdown()
            with ap.ledger.transaction() as connection:
                connection.execute("ALTER TABLE ap_nodes DROP COLUMN pending_no_progress")
                connection.execute("UPDATE ap_nodes SET state=?,no_progress=1 WHERE id=?", (state, before["id"]))
            migrate(manager.db)
            restored = ap.ledger.get("nodes", before["id"])
            assert restored["pending_no_progress"] == 0 and restored["no_progress"] == 1
            release.set()
            replacement = controlled_install(manager)
            manager.runner = SemanticRunner()
            await drive(manager, lambda: replacement.ledger.view(identity)["state"] == "completed")
            after = replacement.ledger.get("nodes", before["id"])
            assert after["no_progress"] == 1 and after["pending_no_progress"] == 0
            assert after["fix_rounds"] == 1 and len(fixes(replacement, identity)) == 1
            calls = invocations(fake_codex_state)
            assert len(calls) == 2 and calls[-1]["argv"] == ["resume", task["codex_thread_id"]]
            assert len(manager.db.list_turns(task["id"])) == 2
        finally:
            release.set()
            await manager.shutdown()

    asyncio.run(scenario())


def test_repair_counter_migration_defaults_legacy_rows_and_preserves_pending_values(db, git_repo):
    ledger = Ledger(db)
    workflow = ledger.register(manifest(semantic_plan(git_repo, names=("C", "E"))))
    legacy, pending = workflow["id"] + ":C", workflow["id"] + ":E"
    with ledger.transaction() as connection:
        connection.execute("ALTER TABLE ap_nodes DROP COLUMN pending_no_progress")
        connection.execute("UPDATE ap_nodes SET state='fix_queued',no_progress=1 WHERE id=?", (legacy,))
        connection.execute("UPDATE ap_nodes SET state='reserving_fix',no_progress=2 WHERE id=?", (pending,))
    migrate(db)
    assert ledger.get("nodes", legacy)["pending_no_progress"] == 0
    assert ledger.get("nodes", legacy)["no_progress"] == 1
    assert ledger.get("nodes", pending)["pending_no_progress"] == 0
    assert ledger.get("nodes", pending)["no_progress"] == 2
    with ledger.transaction() as connection:
        connection.execute("UPDATE ap_nodes SET pending_no_progress=1,no_progress=0 WHERE id=?", (pending,))
    expected = ledger.rows("nodes", "workflow_id=?", (workflow["id"],))
    migrate(db)
    migrate(db)
    assert ledger.rows("nodes", "workflow_id=?", (workflow["id"],)) == expected


def test_reserved_semantic_fix_runs_once_after_restart(
    make_manager, git_repo, fake_codex_state, controlled_install, monkeypatch,
):
    async def scenario():
        manager = make_manager()
        ap = controlled_install(manager)
        manager.runner = SemanticRunner()
        dispatch = manager._dispatch

        def before_dispatch(task_id):
            task = manager.db.get_task(task_id)
            if task and task["last_prompt"].startswith("Autopilot correction"):
                return False
            return dispatch(task_id)

        monkeypatch.setattr(manager, "_dispatch", before_dispatch)
        try:
            identity = await register(ap, semantic_plan(git_repo))
            await drive(manager, lambda: ap.ledger.get("nodes", identity + ":C")["state"] == "fixing")
            node = ap.ledger.get("nodes", identity + ":C")
            assert manager.db.get_task(node["task_id"])["status"] == "queued"
            assert len(fixes(ap, identity)) == 1 and len(invocations(fake_codex_state)) == 1
            await ap.shutdown()
            replacement = controlled_install(manager)
            manager.runner = SemanticRunner()
            monkeypatch.setattr(manager, "_dispatch", dispatch)
            await drive(manager, lambda: replacement.ledger.view(identity)["state"] == "completed")
            assert len(fixes(replacement, identity)) == 1
            assert len(invocations(fake_codex_state)) == 2
            assert len(manager.db.list_turns(node["task_id"])) == 2
            assert replacement.ledger.get("nodes", node["id"])["fix_rounds"] == 1
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_owner_instruction_during_repair_preflight_discards_stale_fix(
    make_manager, git_repo, fake_codex_state, controlled_install, monkeypatch,
):
    async def scenario():
        manager = make_manager()
        ap = controlled_install(manager)
        manager.runner = SemanticRunner()
        ready, release = hold_repair_preflight(monkeypatch, ap, ignore_cancel=True)
        try:
            identity = await register(ap, semantic_plan(git_repo))
            await drive(manager, ready.is_set)
            before = ap.ledger.get("nodes", identity + ":C")
            await manager.send_instruction(before["task_id"], "ok Owner clarified the remaining artifact requirement")
            assert ap.ledger.get("nodes", before["id"])["generation"] > before["generation"]
            release.set()
            await drive(manager, lambda: ap.ledger.view(identity)["state"] == "completed")
            assert not fixes(ap, identity)
            calls = invocations(fake_codex_state)
            assert len(calls) == 2 and "Owner clarified" in calls[-1]["prompt"]
            assert ap.ledger.get("nodes", before["id"])["fix_rounds"] == 0
            assert len(manager.db.list_turns(before["task_id"])) == 2
        finally:
            release.set()
            await manager.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["same_contract", "completion_recheck"])
def test_changed_completion_source_does_not_spend_an_unexecuted_repair_budget(
    make_manager, git_repo, fake_codex_state, controlled_install, monkeypatch, change,
):
    async def scenario():
        manager = make_manager()
        ap = controlled_install(manager)
        manager.runner = SemanticRunner()
        ready, release = hold_repair_preflight(monkeypatch, ap)
        try:
            identity = await register(ap, semantic_plan(git_repo))
            await drive(manager, ready.is_set)
            before = ap.ledger.get("nodes", identity + ":C")
            task = manager.db.get_task(before["task_id"])
            if change == "same_contract":
                manager.set_completion_contract(task["id"], json.loads(task["completion_contract"]))
            else:
                from app import completion

                # Make the recheck timestamp distinct without delaying a scheduler race test.
                monkeypatch.setattr(completion, "now_iso", lambda: "2040-01-01T00:00:00Z")
                await manager.rerun_completion(task["id"])
            release.set()
            await drive(manager, lambda: ap.ledger.view(identity)["state"] == "completed")
            after = ap.ledger.get("nodes", before["id"])
            assert after["fix_rounds"] == 1 and len(fixes(ap, identity)) == 1
            calls = invocations(fake_codex_state)
            assert len(calls) == 2 and calls[-1]["argv"] == ["resume", task["codex_thread_id"]]
            assert len(manager.db.list_turns(task["id"])) == 2
            assert len(ap.ledger.rows("acceptances")) == 1
        finally:
            release.set()
            await manager.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("reason,recoverable", [
    ("Rate limit tests are failing", True),
    ("The handler returns 401 instead of 200", True),
    ('The test expects "not logged in" but gets 500', True),
    ("Not logged in; run codex login", False),
    ("Usage limit reached for this account", False),
    ("Codex authentication failed; log in again", False),
])
def test_semantic_account_guards_distinguish_product_defects_from_execution_unavailability(
    make_manager, git_repo, fake_codex_state, controlled_install, reason, recoverable,
):
    async def scenario():
        manager = make_manager()
        ap = controlled_install(manager)
        manager.runner = SemanticRunner()
        try:
            raw = semantic_plan(git_repo)
            # The fixture echoes the appended controller instructions too; preserve the sentence boundary.
            raw["nodes"][0]["prompt"] = "semantic PARTIAL " + reason + "."
            identity = await register(ap, raw)
            if recoverable:
                await drive(manager, lambda: ap.ledger.view(identity)["state"] == "completed")
                assert len(fixes(ap, identity)) == 1 and len(invocations(fake_codex_state)) == 2
                assert len(ap.ledger.rows("acceptances")) == 1
            else:
                await drive(manager, lambda: ap.ledger.get("nodes", identity + ":C")["state"] == "blocked_result")
                for _ in range(3):
                    manager.tick()
                    await asyncio.sleep(0)
                assert not fixes(ap, identity) and len(invocations(fake_codex_state)) == 1
                assert not ap.ledger.rows("acceptances")
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_stop_before_semantic_fix_reservation_prevents_late_continuation(
    make_manager, git_repo, fake_codex_state, controlled_install, monkeypatch,
):
    async def scenario():
        manager = make_manager()
        ap = controlled_install(manager)
        manager.runner = SemanticRunner()
        ready, release = hold_repair_preflight(monkeypatch, ap)
        try:
            identity = await register(ap, semantic_plan(git_repo))
            await drive(manager, ready.is_set)
            stale = ap.ledger.get("nodes", identity + ":C")
            await ap.control(identity, "stop")
            release.set()
            await ap._fix(stale)
            for _ in range(3):
                manager.tick()
                await asyncio.sleep(0)
            assert ap.ledger.view(identity)["state"] == "stopped"
            assert ap.ledger.get("nodes", stale["id"])["state"] == "stopped"
            assert not fixes(ap, identity) and not ap.ledger.rows("acceptances")
            assert len(invocations(fake_codex_state)) == 1
        finally:
            release.set()
            await manager.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("message,kind", [("401 Unauthorized", "auth"), ("invalid configuration", "config")])
def test_execution_auth_and_configuration_failures_are_not_semantically_retried(
    make_manager, git_repo, fake_codex_state, controlled_install, message, kind,
):
    async def scenario():
        manager = make_manager()
        ap = controlled_install(manager)
        manager.runner = SemanticRunner()
        try:
            raw = semantic_plan(git_repo)
            raw["nodes"][0]["prompt"] = "err 1 " + message
            identity = await register(ap, raw)
            await drive(manager, lambda: ap.ledger.get("nodes", identity + ":C")["state"] in {
                "blocked_result", "blocked_auth", "blocked_configuration",
            })
            node = ap.ledger.get("nodes", identity + ":C")
            for _ in range(5):
                manager.tick()
                await asyncio.sleep(0)
            task = manager.db.get_task(node["task_id"])
            assert task["status"] == "failed" and task["last_failure_kind"] == kind
            assert task["retry_count"] == 0 and node["fix_rounds"] == 0
            assert len(invocations(fake_codex_state)) == 1
            assert not fixes(ap, identity) and not ap.ledger.rows("acceptances")
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_unapproved_manual_gate_remains_blocked_while_independent_leaf_runs(
    make_manager, git_repo, fake_codex_state, controlled_install,
):
    async def scenario():
        manager = make_manager()
        ap = controlled_install(manager)
        manager.runner = SemanticRunner()
        try:
            raw = semantic_plan(git_repo, names=("C", "E", "U"))
            raw["nodes"][0]["manual_gates"] = ["Owner must approve the design"]
            raw["nodes"][1]["depends_on"] = ["C"]
            identity = await register(ap, raw)
            await drive(manager, lambda: ap.ledger.get("nodes", identity + ":U")["state"] == "accepted")
            node = ap.ledger.get("nodes", identity + ":C")
            assert node["state"] == "blocked_decision" and node["task_id"] is None
            assert ap.ledger.get("nodes", identity + ":E")["task_id"] is None
            assert not invocations(fake_codex_state) and not fixes(ap, identity)
            assert not ap.ledger.rows("decisions")
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("gate", ["contract", "policy"])
def test_final_owner_approval_is_not_bypassed_by_semantic_recovery(
    make_manager, git_repo, fake_codex_state, controlled_install, gate,
):
    async def scenario():
        manager = make_manager()
        ap = controlled_install(manager)
        manager.runner = SemanticRunner()
        try:
            raw = semantic_plan(git_repo, names=("C", "E"))
            raw["nodes"][1]["depends_on"] = ["C"]
            if gate == "contract":
                raw["nodes"][0]["contract"] = dict(manual_approval=True)
            else:
                raw["policy"]["automatic_acceptance"] = False
            identity = await register(ap, raw)
            await drive(manager, lambda: ap.ledger.get("nodes", identity + ":C")["state"] == "blocked_decision")
            node = ap.ledger.get("nodes", identity + ":C")
            assert node["candidate_id"] and len(fixes(ap, identity)) == 1
            assert len(invocations(fake_codex_state)) == 2
            assert ap.ledger.rows("validation", "state='PASS'")
            assert any(review["payload"].get("verdict") == "APPROVE" for review in ap.ledger.rows("reviews"))
            assert manager.db.get_task(node["task_id"])["task_outcome"] == "needs_review"
            assert ap.ledger.get("nodes", identity + ":E")["task_id"] is None
            assert not ap.ledger.rows("decisions") and not ap.ledger.rows("acceptances")
            await ap.control(identity, "resume")
            for _ in range(3):
                manager.tick()
                await asyncio.sleep(0)
            assert ap.ledger.get("nodes", node["id"])["state"] == "blocked_decision"
            assert not ap.ledger.rows("acceptances")
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_quota_wait_does_not_become_a_semantic_retry(
    make_manager, git_repo, fake_codex_state, controlled_install, monkeypatch,
):
    async def scenario():
        manager = make_manager()
        ap = controlled_install(manager)
        manager.runner = SemanticRunner()

        async def limits(*args, **kwargs):
            return dict(ordinary_usage_allowed=False)

        monkeypatch.setattr(manager, "read_limits", limits)
        try:
            raw = semantic_plan(git_repo)
            raw["nodes"][0]["prompt"] = "err 1 usage limit exceeded"
            raw["policy"]["provider_backoff"] = 60
            identity = await register(ap, raw)
            await drive(manager, lambda: ap.ledger.get("nodes", identity + ":C")["state"] in {
                "waiting_implementation_provider", "probing_implementation",
            })
            for _ in range(5):
                manager.tick()
                await asyncio.sleep(0)
            node = ap.ledger.get("nodes", identity + ":C")
            task = manager.db.get_task(node["task_id"])
            assert task["status"] == "waiting-for-quota" and task["last_failure_kind"] == "quota"
            assert task["retry_count"] == 0 and node["fix_rounds"] == 0
            assert len(invocations(fake_codex_state)) == 1
            assert not fixes(ap, identity) and not ap.ledger.rows("acceptances")
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_scheduled_semantic_recovery_keeps_reservation_until_acceptance_and_runs_next_turn_serially(
    make_manager, git_repo, fake_codex_state, controlled_install, monkeypatch,
):
    async def scenario():
        manager = make_manager()
        provider = HoldingReview()
        provider.release.set()
        ap = controlled_install(manager, provider)
        manager.runner = SemanticRunner()
        release = None
        try:
            identity = await register(ap, semantic_plan(git_repo, "SUCCESS"))
            await drive(manager, lambda: ap.ledger.view(identity)["state"] == "completed")
            node = ap.ledger.get("nodes", identity + ":C")
            thread = manager.db.get_task(node["task_id"])["codex_thread_id"]
            provider.ready.clear()
            provider.release.clear()
            ready, release = hold_repair_preflight(monkeypatch, ap)
            first = await manager.schedule_instruction(
                node["task_id"], "semantic PARTIAL Scheduled artifact must finish the fixed requirement",
            )
            second = await manager.schedule_instruction(
                node["task_id"], "ok Confirm the repaired artifact on the same thread",
            )
            await drive(manager, ready.is_set)
            assert manager.db.get_scheduled(first["id"])["status"] == "running"
            waiting = manager.db.get_scheduled(second["id"])
            assert waiting["status"] in ("ready", "waiting_thread") and waiting["started_at"] is None
            assert second["id"] not in {row["id"] for row in manager.db.ready_scheduled()}
            assert len(invocations(fake_codex_state)) == 2
            release.set()
            await drive(manager, provider.ready.is_set)
            assert manager.db.get_scheduled(first["id"])["status"] == "running"
            waiting = manager.db.get_scheduled(second["id"])
            assert waiting["status"] in ("ready", "waiting_thread") and waiting["started_at"] is None
            assert second["id"] not in {row["id"] for row in manager.db.ready_scheduled()}
            assert len(invocations(fake_codex_state)) == 3
            assert manager.db.get_task(node["task_id"])["task_outcome"] == "needs_review"
            provider.release.set()
            await drive(manager, lambda: (
                manager.db.get_scheduled(first["id"])["status"] == "completed"
                and manager.db.get_scheduled(second["id"])["status"] == "completed"
                and ap.ledger.view(identity)["state"] == "completed"
            ))
            calls = invocations(fake_codex_state)
            assert len(calls) == 4
            assert all(call["argv"] == ["resume", thread] for call in calls[1:])
            assert calls[1]["prompt"].startswith("semantic PARTIAL Scheduled")
            assert calls[2]["prompt"].startswith("Autopilot correction")
            assert calls[3]["prompt"].startswith("ok Confirm")
            assert len(manager.db.list_turns(node["task_id"])) == 4
            assert len(fixes(ap, identity)) == 1
            assert len(ap.ledger.rows("acceptances")) == 3
        finally:
            if release:
                release.set()
            provider.release.set()
            await manager.shutdown()

    asyncio.run(scenario())
