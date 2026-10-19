import asyncio
import json
from pathlib import Path
import sys
import time

import pytest

from app.autopilot import mentoring
from app.autopilot.execution import markdown_settings, resolve_execution
from app.autopilot.importer import prepare
from app.autopilot.policy import digest, manifest
from app.autopilot.providers import ProviderFailure, reviewer_policy
from autopilot_helpers import FixtureProvider, ImplementationRunner, drive, install, plan, register


@pytest.fixture
def controlled_install(monkeypatch):
    """State-machine unit dependency injection; real sandbox coverage stays in test_autopilot.py."""
    from app.autopilot import environment

    async def fake_preflight(root, profile, runtime_mounts=None):
        return [dict(environment="synthetic controller fixture; no toolchain probe")]

    class FakeValidator:
        async def validate(self, candidate, root, profile, log_dir, on_start=None):
            checks = [dict(artifact=name, exit_status=0 if (Path(root) / name).is_file() else 1)
                      for name in profile["artifacts"]]
            checks.extend(dict(command=command, command_digest=digest(command), exit_status=0,
                               fixture="synthetic command outcome; no command executed")
                          for command in profile["commands"])
            return dict(scope=candidate["payload"]["scope"], profile_digest=digest(profile), checks=checks,
                        state="PASS" if all(check["exit_status"] == 0 for check in checks) else "FAIL",
                        environment="synthetic controller fixture; not OS isolation evidence")

    monkeypatch.setattr(environment, "preflight", fake_preflight)

    class ChangingImplementationRunner(ImplementationRunner):
        def build_command(self, task, resume_thread=None):
            return [sys.executable, str(Path(__file__).with_name("fake_autopilot_mentor_implementation.py"))] + (
                ["resume", resume_thread] if resume_thread else [])

    def create(manager, *args, changing=False, **kwargs):
        ap = install(manager, *args, **kwargs)
        ap.validator = FakeValidator()
        if changing:
            manager.runner = ChangingImplementationRunner()
        return ap

    return create


def test_implementation_defaults_are_high_and_separate_from_reviewers(git_repo):
    raw = plan(git_repo)
    raw["policy"].update(codex_model="review-codex", claude_model="review-claude")
    fixed = manifest(raw)
    effective = resolve_execution(fixed["nodes"][0], fixed)
    assert effective["model"] == ""
    assert effective["model_source"] == "CLI configuration"
    assert effective["reasoning_effort"] == "high"
    assert fixed["policy"]["mentor_enabled"] is True
    assert fixed["policy"]["mentor_max_rounds"] == 2


def test_markdown_task_common_and_workflow_precedence(git_repo):
    inputs = {
        "COMMON.md": "---\nmodel: common-model\nreasoning_effort: medium\n---\nFixed requirements\n",
        "C.md": "---\nmodel: task-md-model\n---\n- **推論強度**: high\nImplement C.\n",
    }
    imported = prepare(inputs)
    raw = plan(git_repo)
    raw.update(inputs=inputs)
    raw["policy"].update(implementation_model="workflow-model", reasoning_effort="low")
    node = raw["nodes"][0]
    node.update(model="explicit-task", reasoning_effort="xhigh", input_files=imported["nodes"][0]["input_files"])
    effective = resolve_execution(manifest(raw)["nodes"][0], manifest(raw))
    assert effective == dict(model="task-md-model", reasoning_effort="high",
                             model_source="markdown: C.md", reasoning_source="markdown: C.md")
    raw["inputs"]["C.md"] = "Implement C."
    assert resolve_execution(node, raw)["model"] == "explicit-task"
    node.pop("model")
    node.pop("reasoning_effort")
    assert resolve_execution(node, raw)["model"] == "common-model"
    assert resolve_execution(node, raw)["reasoning_effort"] == "medium"
    node["input_files"] = ["C.md"]
    assert resolve_execution(node, raw)["model"] == "common-model"
    del raw["inputs"]["COMMON.md"]
    assert resolve_execution(node, raw)["model"] == "workflow-model"


def test_only_explicit_markdown_directives_select_a_model():
    text = """# Example
The old model: ignored-old is described here.
```yaml
model: ignored-example
```
> model: ignored-quotation
    model: ignored-indented-code
- **Model**: `chosen-model`
Reasoning effort: High
"""
    assert markdown_settings(text, "task.md") == dict(model="chosen-model", reasoning_effort="high")
    with pytest.raises(ValueError, match="model"):
        markdown_settings("Model: model with invalid arguments", "task.md")


@pytest.mark.parametrize("field,value", [
    ("implementation_model", "--unsafe"), ("implementation_model", "model with args"),
    ("implementation_model", None), ("reasoning_effort", "high;echo"),
    ("mentor_enabled", "yes"), ("mentor_max_rounds", True), ("mentor_max_rounds", 6),
])
def test_invalid_execution_policy_is_rejected(git_repo, field, value):
    raw = plan(git_repo)
    raw["policy"][field] = value
    with pytest.raises(ValueError):
        manifest(raw)


def test_conflicting_markdown_models_fail_dry_run_instead_of_guessing(git_repo):
    raw = plan(git_repo)
    raw["inputs"] = {"C.md": "Model: one\nModel: two\n"}
    raw["nodes"][0]["input_files"] = ["C.md"]
    with pytest.raises(ValueError, match="conflicting model"):
        manifest(raw)
    raw["inputs"] = {"C.md": "Model: one\n", "extra.md": "Model: two\n"}
    raw["nodes"][0]["input_files"] = ["C.md", "extra.md"]
    with pytest.raises(ValueError, match="conflicting model"):
        manifest(raw)


def test_model_and_high_reach_initial_fix_and_owner_turns(make_manager, git_repo, fake_codex_state, controlled_install):
    async def scenario():
        manager = make_manager()
        ap = controlled_install(manager)
        raw = plan(git_repo, ("A",))
        raw["policy"]["implementation_model"] = "base-model"
        raw["inputs"] = {"A.md": "Model: assigned-model\nReasoning effort: high\n"}
        raw["nodes"][0]["input_files"] = ["A.md"]
        preview = await ap.dry_run(raw)
        assert preview["execution_settings"]["A"]["model_source"] == "markdown: A.md"
        identity = await register(ap, raw)
        await drive(manager, lambda: ap.ledger.view(identity)["state"] == "completed")
        node = ap.ledger.view(identity)["nodes"][0]
        task = manager.get(node["task_id"])
        assert (task["model"], task["reasoning_effort"]) == ("assigned-model", "high")
        fix = ap.ledger.rows("operations", "node_id=? AND kind='fix'", (node["id"],))[0]
        assert fix["payload"]["execution_settings"]["model"] == "assigned-model"
        assert fix["payload"]["execution_settings"]["reasoning_effort"] == "high"
        thread = task["codex_thread_id"]
        # The immutable task instruction is also applied to an owner follow-up.
        manager.db.update_task(task["id"], model="stale", reasoning_effort="low")
        await manager.send_instruction(task["id"], "Continue the fixed acceptance work.")
        await drive(manager, lambda: ap.ledger.view(identity)["state"] == "completed")
        task = manager.get(task["id"])
        assert (task["model"], task["reasoning_effort"], task["codex_thread_id"]) == ("assigned-model", "high", thread)
        manager.db.update_task(task["id"], reasoning_effort="low")
        await manager.schedule_instruction(task["id"], "Confirm the original acceptance scope again.")
        await drive(manager, lambda: manager.scheduled_instructions(task["id"])[0]["status"] == "completed")
        assert manager.get(task["id"])["reasoning_effort"] == "high"
        assert manager.get(task["id"])["codex_thread_id"] == thread
        turns = [json.loads(line) for line in (fake_codex_state / "autopilot-implementations.jsonl").read_text().splitlines()]
        assert len({turn["thread"] for turn in turns}) == 1
        await manager.shutdown()

    asyncio.run(scenario())


class MentorFixture(FixtureProvider):
    def __init__(self, *, mentor_verdict="CHANGES_REQUESTED", mentor_error=None, repeated=False):
        super().__init__("claude")
        self.mentor_verdict = mentor_verdict
        self.mentor_error = mentor_error
        self.repeated = repeated

    async def start_review(self, candidate, policy, root, attempt, on_identity):
        is_mentor = bool(candidate.get("mentor_context"))
        self.scenario = dict(always_changes=self.repeated or candidate["generation"] <= 2)
        result = await super().start_review(candidate, policy, root, attempt, on_identity)
        if is_mentor and self.mentor_error:
            raise ProviderFailure(self.mentor_error, "synthetic mentor failure")
        if result["blocking"]:
            result["blocking"][0]["reason"] = ("Repeated issue" if self.repeated else f"Required correction {candidate['generation']}")
        if is_mentor:
            assert "MENTOR CONSULTATION" in reviewer_policy(candidate)
            result["verdict"] = self.mentor_verdict
            if self.mentor_verdict == "APPROVE":
                result["blocking"] = []
                result["goal_review"]["root_causes"] = []
                for assessment in result["goal_review"]["assessments"].values():
                    assessment["status"] = "ALIGNED"
            else:
                result["blocking"][0]["reason"] += "; edit C.txt within the fixed contract and rerun the required checks"
        return result


@pytest.mark.parametrize("verdict", ["CHANGES_REQUESTED", "APPROVE"])
def test_mentor_advice_repairs_same_thread_and_never_accepts_candidate(make_manager, git_repo, fake_codex_state, verdict, controlled_install):
    async def scenario():
        manager = make_manager()
        ap = controlled_install(manager, MentorFixture(mentor_verdict=verdict), changing=True)
        raw = plan(git_repo)
        raw["policy"]["implementation_model"] = "base-model"
        identity = await register(ap, raw)
        await drive(manager, lambda: ap.ledger.view(identity)["state"] == "completed", timeout=30)
        node = ap.ledger.view(identity)["nodes"][0]
        mentors = [r for r in ap.ledger.rows("reviews") if r["payload"].get("purpose") == "mentor"]
        assert len(mentors) == 1 and mentors[0]["state"] == "mentored"
        assert node["fix_rounds"] == 2
        assert node["candidate_id"] != mentors[0]["candidate_id"]
        assert len(ap.ledger.rows("acceptances")) == 1
        fixes = ap.ledger.rows("operations", "node_id=? AND kind='fix'", (node["id"],))
        assert all(fix["payload"]["execution_settings"]["reasoning_effort"] == "high" for fix in fixes)
        turns = [json.loads(line) for line in (fake_codex_state / "autopilot-implementations.jsonl").read_text().splitlines()]
        assert len(turns) == 3 and len({turn["thread"] for turn in turns}) == 1
        assert "Required correction 2" in turns[-1]["prompt"]
        if verdict == "CHANGES_REQUESTED":
            assert "Mentor guidance:" in turns[-1]["prompt"]
        await manager.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("mentor_error,expected", [("auth", "blocked_auth"), ("configuration", "blocked_configuration")])
def test_mentor_auth_and_configuration_do_not_switch_provider(make_manager, git_repo, mentor_error, expected, controlled_install):
    async def scenario():
        manager = make_manager()
        other = FixtureProvider("codex")
        ap = controlled_install(manager, MentorFixture(mentor_error=mentor_error), other, changing=True)
        identity = await register(ap, plan(git_repo))
        await drive(manager, lambda: ap.ledger.view(identity)["nodes"][0]["state"] == expected, timeout=30)
        assert not other.calls and not ap.ledger.rows("acceptances")
        assert ap.ledger.view(identity)["nodes"][0]["fix_rounds"] == 1
        await manager.shutdown()

    asyncio.run(scenario())


def test_mentor_cannot_replenish_no_progress_or_fix_budget(make_manager, git_repo, controlled_install):
    async def scenario():
        manager = make_manager()
        ap = controlled_install(manager, MentorFixture(repeated=True), changing=True)
        raw = plan(git_repo)
        raw["policy"].update(no_progress_limit=2, max_fix_rounds=3)
        identity = await register(ap, raw)
        await drive(manager, lambda: ap.ledger.view(identity)["nodes"][0]["state"] == "blocked_no_progress", timeout=30)
        node = ap.ledger.view(identity)["nodes"][0]
        assert node["fix_rounds"] <= 3 and node["no_progress"] == 2
        assert not ap.ledger.rows("acceptances")
        before = len(ap.ledger.rows("reviews"))
        for _ in range(5):
            manager.tick()
        assert len(ap.ledger.rows("reviews")) == before
        await manager.shutdown()

    asyncio.run(scenario())


def test_mentor_keeps_original_findings_and_owned_paths():
    node = dict(spec=dict(paths=["src"]))
    source = dict(payload=dict(blocking=[dict(id="required", path="src/a.py", line=1, reason="Original requirement")]))
    findings = mentoring.repair_findings(node, source, dict(blocking=[], resolved=["required"], verdict="APPROVE"))
    assert findings == source["payload"]["blocking"]
    with pytest.raises(ValueError, match="ownership"):
        mentoring.repair_findings(node, source, dict(blocking=[dict(id="escape", path="other/a.py", line=1, reason="Edit outside ownership")]))


@pytest.mark.parametrize("policy", [dict(mentor_enabled=False), dict(mentor_max_rounds=0)])
def test_mentor_can_be_disabled_without_changing_required_review(make_manager, git_repo, controlled_install, policy):
    async def scenario():
        manager = make_manager()
        ap = controlled_install(manager, MentorFixture(), changing=True)
        raw = plan(git_repo)
        raw["policy"].update(policy)
        identity = await register(ap, raw)
        await drive(manager, lambda: ap.ledger.view(identity)["state"] == "completed")
        assert not any(r["payload"].get("purpose") == "mentor" for r in ap.ledger.rows("reviews"))
        assert len(ap.ledger.rows("reviews")) == 3 and len(ap.ledger.rows("acceptances")) == 1
        await manager.shutdown()

    asyncio.run(scenario())


def test_mentor_owner_decision_never_turns_into_a_repair(make_manager, git_repo, controlled_install):
    async def scenario():
        manager = make_manager()
        ap = controlled_install(manager, MentorFixture(mentor_verdict="NEEDS_HUMAN"), changing=True)
        identity = await register(ap, plan(git_repo))
        await drive(manager, lambda: ap.ledger.view(identity)["nodes"][0]["state"] == "blocked_decision")
        assert ap.ledger.view(identity)["nodes"][0]["fix_rounds"] == 1
        assert not ap.ledger.rows("acceptances")
        await manager.shutdown()

    asyncio.run(scenario())


def test_completed_mentor_recovers_once_without_a_second_consultation(make_manager, git_repo, controlled_install, monkeypatch):
    async def scenario():
        manager = make_manager()
        ap = controlled_install(manager, MentorFixture(), changing=True)
        # Simulate a crash after durable advice and before its state transition.
        monkeypatch.setattr(ap, "_finish_mentor", lambda *args: None)
        identity = await register(ap, plan(git_repo))
        await drive(manager, lambda: bool(ap.ledger.rows("reviews", "state='mentored'")))
        await ap.shutdown()
        replacement = controlled_install(manager, MentorFixture(), changing=True)
        await drive(manager, lambda: replacement.ledger.view(identity)["state"] == "completed")
        assert len(replacement.ledger.rows("reviews", "state='mentored'")) == 1
        assert replacement.ledger.view(identity)["nodes"][0]["fix_rounds"] == 2
        await manager.shutdown()

    asyncio.run(scenario())


def test_stop_during_mentor_discards_late_advice(make_manager, git_repo, controlled_install):
    class WaitingMentor(MentorFixture):
        async def start_review(self, candidate, policy, root, attempt, on_identity):
            result = await super().start_review(candidate, policy, root, attempt, on_identity)
            if candidate.get("mentor_context"):
                self.ready = True
                await asyncio.Event().wait()
            return result

    async def scenario():
        manager = make_manager()
        provider = WaitingMentor()
        ap = controlled_install(manager, provider, changing=True)
        identity = await register(ap, plan(git_repo))
        await drive(manager, lambda: getattr(provider, "ready", False))
        node = ap.ledger.view(identity)["nodes"][0]
        record = next(r for r in ap.ledger.rows("reviews") if r["payload"].get("purpose") == "mentor")
        await ap.control(identity, "stop")
        record["payload"]["verdict"] = "NEEDS_HUMAN"
        ap._finish_mentor(node, record)
        ap._request_fix(node, "late-advice", [dict(id="late", path="C.txt", line=1, reason="Late advice")])
        for _ in range(3):
            manager.tick()
        assert ap.ledger.view(identity)["nodes"][0]["state"] == "stopped"
        assert not ap.ledger.rows("acceptances")
        assert len(ap.ledger.rows("operations", "kind='fix'")) == 1
        await manager.shutdown()

    asyncio.run(scenario())


def test_integration_uses_its_explicit_model_and_high(make_manager, git_repo, controlled_install):
    async def scenario():
        manager = make_manager()
        ap = controlled_install(manager)
        raw = plan(git_repo, ("C", "I1"))
        raw["policy"]["implementation_model"] = "base-model"
        raw["nodes"][1].update(kind="integration", depends_on=["C"], paths=["C.txt"],
                               validation=raw["nodes"][0]["validation"], model="integration-model",
                               integration_mode="merge-then-implement")
        identity = await register(ap, raw)
        await drive(manager, lambda: ap.ledger.view(identity)["state"] == "completed")
        tasks = {node["name"]: manager.get(node["task_id"]) for node in ap.ledger.view(identity)["nodes"]}
        assert tasks["C"]["model"] == "base-model"
        assert tasks["I1"]["model"] == "integration-model" and tasks["I1"]["reasoning_effort"] == "high"
        assert tasks["I1"]["codex_thread_id"]
        await manager.shutdown()

    asyncio.run(scenario())


def test_unsupported_high_is_configuration_error_without_downgrade(make_manager, git_repo, controlled_install, monkeypatch):
    from unittest.mock import AsyncMock

    async def scenario():
        manager = make_manager()
        ap = controlled_install(manager)
        raw = plan(git_repo)
        preview = await ap.dry_run(raw)
        workflow = await ap.register(raw, preview["input_digest"])
        task = await manager.create_task(repository=str(git_repo), name="capability fixture", prompt="fixture",
                                         model="limited-model", reasoning_effort="high", _defer_dispatch=True)
        node = ap.ledger.rows("nodes", "workflow_id=?", (workflow["id"],))[0]
        await ap.control(workflow["id"], "start")
        await ap.control(workflow["id"], "pause")
        assert ap.ledger.transition(node["id"], "pending", "pending", task_id=task["id"])
        client = type("Client", (), {"request": AsyncMock(return_value=dict(config=dict(model="limited-model")))})()
        monkeypatch.setattr(manager, "client", AsyncMock(return_value=client))
        monkeypatch.setattr(manager, "_require_subscription", AsyncMock())
        monkeypatch.setattr(ap.providers["codex"], "probe_capabilities_and_auth", AsyncMock(return_value={}))
        monkeypatch.setattr(manager.ctx, "model_info", AsyncMock(return_value=dict(efforts=["low", "medium"])))
        with pytest.raises(ProviderFailure, match="no automatic downgrade") as error:
            await ap.preflight_execution(task)
        assert error.value.kind == "configuration"
        assert manager.get(task["id"])["reasoning_effort"] == "high"
        await manager.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("legacy_row", [False, True])
def test_mentor_preserves_primary_reason_in_history_and_next_review(make_manager, git_repo, controlled_install, monkeypatch, legacy_row):
    class RewordingMentor(MentorFixture):
        async def start_review(self, candidate, policy, root, attempt, on_identity):
            result = await super().start_review(candidate, policy, root, attempt, on_identity)
            if candidate.get("mentor_context"):
                result["blocking"][0]["reason"] = "Independent mentor advice without the original wording"
            return result

    async def scenario():
        manager = make_manager()
        provider = RewordingMentor()
        ap = controlled_install(manager, provider, changing=True)
        finish = ap._finish_mentor
        inspected = []

        def inspect_record(node, record, source=None):
            payload = record["payload"]
            assert payload["blocking"][0]["reason"] == "Independent mentor advice without the original wording"
            assert payload["original_findings"][0]["reason"] == "Required correction 2"
            assert "Required correction 2\nMentor guidance: Independent mentor advice" in payload["repair_findings"][0]["reason"]
            inspected.append(record["id"])
            if legacy_row:
                # Existing versions saved only raw advice and a source_review_id.
                payload = {key: value for key, value in payload.items()
                           if key not in ("original_findings", "repair_findings")}
                ap.ledger.finish("reviews", record["id"], "mentored", payload)
                record = ap.ledger.get("reviews", record["id"])
            return finish(node, record, source)

        monkeypatch.setattr(ap, "_finish_mentor", inspect_record)
        identity = await register(ap, plan(git_repo))
        await drive(manager, lambda: ap.ledger.view(identity)["state"] == "completed")
        assert len(inspected) == 1
        final_input = provider.calls[-1]["received"]
        carried = next(f for f in final_input["prior_findings"] if f["id"] == "persistent")
        assert carried["reason"].startswith("Required correction 2\nMentor guidance: ")
        assert "Independent mentor advice without the original wording" in carried["reason"]
        mentor = ap.ledger.get("reviews", inspected[0])
        source = ap.ledger.get("reviews", mentor["payload"]["source_review_id"])
        assert source["payload"]["blocking"][0]["reason"] == "Required correction 2"
        await manager.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("error_kind,infra_retries,failed_attempts,expected", [
    ("auth", 2, 1, "blocked_auth"),
    ("configuration", 2, 1, "blocked_configuration"),
    ("timeout", 0, 1, "blocked_infrastructure"),
    ("timeout", 1, 2, "blocked_infrastructure"),
    ("generic_exception", 2, 1, "blocked_infrastructure"),
])
def test_incomplete_mentor_crash_preserves_fatal_and_retry_limits(make_manager, git_repo, controlled_install, monkeypatch,
                                                                error_kind, infra_retries, failed_attempts, expected):
    class FailingMentor(MentorFixture):
        async def start_review(self, candidate, policy, root, attempt, on_identity):
            result = await super().start_review(candidate, policy, root, attempt, on_identity)
            if candidate.get("mentor_context") and error_kind == "generic_exception":
                raise RuntimeError("Synthetic mentor infrastructure failure")
            return result

    async def scenario():
        manager = make_manager()
        provider = FailingMentor(mentor_error=None if error_kind == "generic_exception" else error_kind)
        ap = controlled_install(manager, provider, changing=True)
        finish = ap.ledger.finish
        crash = []

        def save_then_crash(table, identity, state, payload):
            finish(table, identity, state, payload)
            if table == "reviews" and state == "incomplete" and payload.get("purpose") == "mentor":
                failures = ap.ledger.rows("reviews", "candidate_id=? AND state='incomplete'",
                                          (ap.ledger.get("reviews", identity)["candidate_id"],))
                if len(failures) == failed_attempts:
                    crash.append(identity)
                    raise asyncio.CancelledError()

        monkeypatch.setattr(ap.ledger, "finish", save_then_crash)
        raw = plan(git_repo)
        raw["policy"].update(infra_retries=infra_retries, provider_backoff=1)
        identity = await register(ap, raw)
        await drive(manager, lambda: bool(crash))
        node = ap.ledger.view(identity)["nodes"][0]
        assert node["state"] == "reviewing"
        latest = ap.ledger.get("reviews", crash[-1])
        assert latest["state"] == "incomplete" and "failure_state" not in latest["payload"]
        before = len(ap.ledger.rows("reviews"))
        await ap.shutdown()
        replacement_provider = MentorFixture()
        fallback = FixtureProvider("codex")
        replacement = controlled_install(manager, replacement_provider, fallback, changing=True)
        replacement.reconcile(identity)
        assert replacement.ledger.view(identity)["nodes"][0]["state"] == expected
        for _ in range(3):
            manager.tick()
        assert len(replacement.ledger.rows("reviews")) == before
        assert not replacement_provider.calls and not fallback.calls
        saved = replacement.ledger.get("reviews", crash[-1])["payload"]
        assert saved["failure_state"] == expected
        if error_kind == "timeout":
            assert saved["retry_failures"] == failed_attempts
        assert replacement.ledger.view(identity)["nodes"][0]["fix_rounds"] == 1
        assert not replacement.ledger.rows("acceptances")
        await manager.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("error_kind", ["timeout", "included_quota"])
def test_incomplete_mentor_crash_restores_backoff_or_quota_wait(make_manager, git_repo, controlled_install, monkeypatch, error_kind):
    async def scenario():
        manager = make_manager()
        ap = controlled_install(manager, MentorFixture(mentor_error=error_kind), changing=True)
        finish = ap.ledger.finish
        crash = []

        def save_then_crash(table, identity, state, payload):
            finish(table, identity, state, payload)
            if table == "reviews" and state == "incomplete" and payload.get("purpose") == "mentor":
                crash.append(identity)
                raise asyncio.CancelledError()

        monkeypatch.setattr(ap.ledger, "finish", save_then_crash)
        raw = plan(git_repo)
        raw["policy"].update(infra_retries=1, provider_backoff=60)
        identity = await register(ap, raw)
        await drive(manager, lambda: bool(crash))
        assert not ap.ledger.rows("provider_availability")
        latest = ap.ledger.get("reviews", crash[0])
        assert "retry_at" not in latest["payload"] and "recheck_at" not in latest["payload"]
        before = len(ap.ledger.rows("reviews"))
        await ap.shutdown()
        replacement = controlled_install(manager, MentorFixture(), changing=True)
        replacement.reconcile(identity)
        node = replacement.ledger.view(identity)["nodes"][0]
        assert node["state"] == "review_queued"
        saved = replacement.ledger.get("reviews", crash[0])["payload"]
        if error_kind == "timeout":
            assert saved["retry_failures"] == 1
            assert saved["retry_at"] == latest["payload"]["failed_at"] + 60 > time.time()
            workflow = replacement.ledger.get("workflows", identity)
            assert replacement._select_provider(node, workflow) is None
            # Another restart cannot erase or extend the already saved backoff.
            assert replacement.ledger.transition(node["id"], "review_queued", "reviewing")
            replacement.reconcile(identity)
            assert replacement.ledger.get("reviews", crash[0])["payload"]["retry_at"] == saved["retry_at"]
        else:
            rows = replacement.ledger.rows("provider_availability")
            assert len(rows) == 1 and rows[0]["state"] == "included_quota"
            assert rows[0]["provider"] == "claude" and rows[0]["recheck_at"] == saved["recheck_at"]
            assert replacement._select_provider(node, replacement.ledger.get("workflows", identity)) == "codex"
        assert len(replacement.ledger.rows("reviews")) == before
        assert not replacement.providers["claude"].calls and not replacement.providers["codex"].calls
        await manager.shutdown()

    asyncio.run(scenario())


def test_existing_finding_evidence_path_does_not_expand_edit_ownership():
    node = dict(spec=dict(paths=["A.txt"]))
    original = dict(id="false-adapter", path="capability.txt", line=1, reason="Pinned evidence contradicts the task assumption")
    source = dict(payload=dict(blocking=[original]))
    advice = dict(blocking=[dict(original, reason="Use the same immutable capability evidence while correcting the owned deliverable")])
    findings = mentoring.repair_findings(node, source, advice)
    assert findings[0]["path"] == "capability.txt"
    assert findings[0]["reason"].startswith(original["reason"] + "\nMentor guidance: ")
    assert node["spec"]["paths"] == ["A.txt"]
    assert source["payload"]["blocking"] == [original]
    with pytest.raises(ValueError, match="ownership"):
        mentoring.repair_findings(node, source, dict(blocking=[dict(original, path="other-source.txt")]))
    with pytest.raises(ValueError, match="ownership"):
        mentoring.repair_findings(node, source, dict(blocking=[dict(original, id="new-outside-finding")]))
