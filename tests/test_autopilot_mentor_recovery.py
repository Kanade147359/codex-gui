"""Bounded semantic diagnosis with durable review lineage and unchanged acceptance gates."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from types import SimpleNamespace

import pytest

from app.autopilot import decisions, mentoring
from app.autopilot.controller import Autopilot
from app.autopilot.ledger import Ledger, migrate
from app.autopilot.policy import canonical, digest, manifest
from autopilot_helpers import FixtureProvider, drive, plan, register
from test_autopilot_execution_mentor import MentorFixture, controlled_install  # noqa: F401


def review_stop(db, git_repo, *, verdict="NEEDS_HUMAN", state="reviewing", policy=None, fix_rounds=0, spec=None):
    migrate(db)
    ap = SimpleNamespace(ledger=Ledger(db))
    raw = plan(git_repo)
    raw["policy"].update(policy or {})
    raw["nodes"][0].update(spec or {})
    workflow = ap.ledger.register(manifest(raw))
    with ap.ledger.transaction() as connection:
        connection.execute("UPDATE ap_workflows SET state='running' WHERE id=?", (workflow["id"],))
    workflow = ap.ledger.get("workflows", workflow["id"])
    node = ap.ledger.rows("nodes")[0]
    candidate = ap.ledger.submit(node, workflow, dict(contract=node["spec"]["contract"]))
    assert ap.ledger.transition(node["id"], "submitted", state, fix_rounds=fix_rounds)
    source = ap.ledger.record(
        "reviews", candidate_id=candidate["id"], provider="claude", generation=1, state="completed",
        payload=dict(verdict=verdict, purpose="review", run_generation=workflow["generation"],
                     blocking=[dict(id="required", path="C.txt", line=1, reason="Original unresolved evidence")],
                     unverified=["Required artifact contents need inspection"]),
    )
    return ap, workflow, ap.ledger.get("nodes", node["id"]), source


@pytest.mark.parametrize("verdict", ["NEEDS_HUMAN", "INCONCLUSIVE"])
def test_first_review_stop_queues_one_independent_diagnosis(db, git_repo, verdict):
    ap, _, node, source = review_stop(db, git_repo, verdict=verdict)
    assert mentoring.queue(ap, node, source)
    current = ap.ledger.get("nodes", node["id"])
    assert current["state"] == "review_queued"
    assert (current["fix_rounds"], current["no_progress"], current["last_failure"]) == (0, 0, "")
    saved = ap.ledger.get("reviews", source["id"])
    assert saved["payload"] == dict(source["payload"], mentor_requested=True)
    assert mentoring.pending(ap, current)["id"] == source["id"]
    assert not mentoring.queue(ap, node, source)
    assert not ap.ledger.rows("acceptances")


@pytest.mark.parametrize("verdict,state", [
    ("NEEDS_HUMAN", "blocked_decision"), ("INCONCLUSIVE", "blocked_infrastructure"),
])
def test_saved_review_stop_recovers_once_across_ledger_restart(db, git_repo, verdict, state):
    ap, _, node, source = review_stop(db, git_repo, verdict=verdict, state=state)
    replacement = SimpleNamespace(ledger=Ledger(db))
    assert mentoring.recover(replacement, node)
    current = replacement.ledger.get("nodes", node["id"])
    assert current["state"] == "review_queued" and current["candidate_id"] == node["candidate_id"]
    assert mentoring.pending(replacement, current)["id"] == source["id"]
    assert not mentoring.recover(ap, node)
    assert current["fix_rounds"] == node["fix_rounds"]


def test_two_callers_reserve_only_one_mentor(db, git_repo):
    ap, _, node, source = review_stop(db, git_repo)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result(timeout=10) for future in
                   [pool.submit(mentoring.queue, ap, node, source) for _ in range(2)]]
    assert sorted(results) == [False, True]
    assert len(ap.ledger.rows("reviews")) == 1


@pytest.mark.parametrize("verdict", ["NEEDS_HUMAN", "INCONCLUSIVE"])
def test_reconcile_resumes_saved_mentor_request_before_new_attempt_was_recorded(db, git_repo, verdict):
    ap, workflow, node, source = review_stop(db, git_repo, verdict=verdict)
    assert mentoring.queue(ap, node, source)
    # A crash after the review claim but before launching/recording its mentor process.
    assert ap.ledger.transition(node["id"], "review_queued", "reviewing")
    ap.jobs = {}
    Autopilot.reconcile(ap, workflow["id"])
    current = ap.ledger.get("nodes", node["id"])
    assert current["state"] == "review_queued"
    assert mentoring.pending(ap, current)["id"] == source["id"]
    assert len(ap.ledger.rows("reviews")) == 1 and current["fix_rounds"] == 0


@pytest.mark.parametrize("verdict,state", [
    ("NEEDS_HUMAN", "reviewing"), ("INCONCLUSIVE", "reviewing"),
    ("NEEDS_HUMAN", "blocked_decision"), ("INCONCLUSIVE", "blocked_infrastructure"),
])
def test_semantic_recovery_can_be_disabled(db, git_repo, verdict, state):
    ap, _, node, source = review_stop(db, git_repo, verdict=verdict, state=state,
                                     policy=dict(semantic_recovery=False))
    assert not mentoring.queue(ap, node, source)
    assert not mentoring.recover(ap, node)
    assert ap.ledger.get("nodes", node["id"])["state"] == state


@pytest.mark.parametrize("fix_rounds,expected", [(0, False), (1, True)])
def test_disabled_semantic_recovery_preserves_existing_change_advice(db, git_repo, fix_rounds, expected):
    ap, _, node, source = review_stop(db, git_repo, verdict="CHANGES_REQUESTED", fix_rounds=fix_rounds,
                                     policy=dict(semantic_recovery=False))
    assert mentoring.queue(ap, node, source) is expected


@pytest.mark.parametrize("guard", [
    "manual_gate", "source_auth", "stopped", "run_generation",
    "candidate_generation", "current_generation", "candidate_id", "mentor_disabled", "zero_rounds", "fix_budget",
])
def test_diagnosis_preserves_manual_auth_candidate_and_budget_guards(db, git_repo, guard):
    ap, workflow, node, source = review_stop(db, git_repo)
    with ap.ledger.transaction() as connection:
        if guard == "manual_gate":
            changed = dict(node["spec"], manual_gates=["Owner approval"])
            connection.execute("UPDATE ap_nodes SET spec=? WHERE id=?", (canonical(changed), node["id"]))
        elif guard == "source_auth":
            connection.execute("UPDATE ap_reviews SET payload=? WHERE id=?",
                               (canonical(dict(source["payload"], error_kind="auth")), source["id"]))
        elif guard == "stopped":
            connection.execute("UPDATE ap_workflows SET state='stopped' WHERE id=?", (workflow["id"],))
        elif guard == "run_generation":
            connection.execute("UPDATE ap_workflows SET generation=generation+1 WHERE id=?", (workflow["id"],))
        elif guard == "candidate_generation":
            connection.execute("UPDATE ap_candidates SET generation=generation+1 WHERE id=?", (node["candidate_id"],))
        elif guard == "current_generation":
            connection.execute("UPDATE ap_nodes SET generation=generation+1 WHERE id=?", (node["id"],))
        elif guard == "candidate_id":
            connection.execute("UPDATE ap_nodes SET candidate_id=NULL WHERE id=?", (node["id"],))
        elif guard == "fix_budget":
            connection.execute("UPDATE ap_nodes SET fix_rounds=? WHERE id=?",
                               (workflow["plan"]["policy"]["max_fix_rounds"], node["id"]))
        else:
            fixed = workflow["plan"]
            fixed["policy"].update({"mentor_enabled": False} if guard == "mentor_disabled" else {"mentor_max_rounds": 0})
            connection.execute("UPDATE ap_workflows SET plan=? WHERE id=?", (canonical(fixed), workflow["id"]))
    assert not mentoring.queue(ap, node, source)
    assert not ap.ledger.get("reviews", source["id"])["payload"].get("mentor_requested")
    assert not ap.ledger.rows("acceptances")


@pytest.mark.parametrize("approval", ["current", "stale_spec", "stale_authorization", "stale_run"])
def test_only_current_owner_start_approval_permits_diagnosis(db, git_repo, approval):
    ap, workflow, node, source = review_stop(db, git_repo, spec=dict(manual_gates=["Owner design approval"]))
    payload = dict(purpose="manual_gates", spec_digest=digest(node["spec"]),
                   run_generation=workflow["generation"], authorization=decisions.authorization(workflow))
    if approval == "stale_spec":
        payload["spec_digest"] = "previous-spec"
    elif approval == "stale_authorization":
        payload["authorization"] = "previous-authorization"
    elif approval == "stale_run":
        payload["run_generation"] -= 1
    with ap.ledger.transaction() as connection:
        connection.execute("INSERT INTO ap_decisions VALUES(?,?,?,?,?,?)",
                           ("start-approval", node["id"], "start-target", "approved", canonical(payload), "fixture"))
    assert mentoring.queue(ap, node, source) is (approval == "current")
    assert not ap.ledger.rows("acceptances")


def test_final_manual_contract_allows_diagnosis_without_changing_contract(db, git_repo):
    ap, _, node, source = review_stop(db, git_repo, spec=dict(contract=dict(manual_approval=True)))
    assert mentoring.queue(ap, node, source)
    assert ap.ledger.get("nodes", node["id"])["spec"]["contract"]["manual_approval"] is True
    assert ap.ledger.get("candidates", node["candidate_id"])["payload"]["contract"]["manual_approval"] is True
    assert not ap.ledger.rows("acceptances")


@pytest.mark.parametrize("latest_state,payload", [
    ("mentored", dict(verdict="NEEDS_HUMAN", purpose="mentor")),
    ("incomplete", dict(error_kind="auth", purpose="mentor")),
    ("incomplete", dict(error_kind="timeout", purpose="mentor")),
    ("claimed", dict(purpose="mentor")),
    ("uncertain", dict(purpose="review")),
])
def test_saved_diagnosis_or_later_failure_never_reopens_original_review(db, git_repo, latest_state, payload):
    ap, workflow, node, source = review_stop(db, git_repo, state="blocked_decision")
    ap.ledger.record("reviews", candidate_id=node["candidate_id"], provider="claude", generation=2,
                     state=latest_state, payload=dict(payload, source_review_id=source["id"],
                                                     run_generation=workflow["generation"]))
    assert not mentoring.recover(ap, node)
    assert ap.ledger.get("nodes", node["id"])["state"] == "blocked_decision"


@pytest.mark.parametrize("verdict,state", [
    ("APPROVE", "blocked_decision"), ("CHANGES_REQUESTED", "blocked_decision"),
    ("NEEDS_HUMAN", "blocked_auth"), ("INCONCLUSIVE", "blocked_configuration"),
    ("NEEDS_HUMAN", "blocked_infrastructure"),
])
def test_recover_only_reopens_corresponding_semantic_review_stop(db, git_repo, verdict, state):
    ap, _, node, _ = review_stop(db, git_repo, verdict=verdict, state=state)
    assert not mentoring.recover(ap, node)


@pytest.mark.parametrize("semantic,expected", [(True, True), (False, False)])
def test_first_diagnosis_at_no_progress_boundary_does_not_replenish_budget(db, git_repo, semantic, expected):
    ap, _, node, source = review_stop(db, git_repo, verdict="CHANGES_REQUESTED", fix_rounds=1,
                                     policy=dict(semantic_recovery=semantic))
    fingerprint = digest(source["payload"]["blocking"])
    assert ap.ledger.transition(node["id"], "reviewing", "reviewing", no_progress=1, last_failure=fingerprint)
    node = ap.ledger.get("nodes", node["id"])
    assert mentoring.queue(ap, node, source) is expected
    current = ap.ledger.get("nodes", node["id"])
    assert (current["fix_rounds"], current["no_progress"], current["last_failure"]) == (1, 1, fingerprint)


@pytest.mark.parametrize("mentor_max_rounds,no_progress", [(1, 0), (2, 1)])
def test_prior_candidate_diagnosis_retains_round_and_no_progress_limits(db, git_repo, mentor_max_rounds, no_progress):
    ap, workflow, node, source = review_stop(db, git_repo, policy=dict(mentor_max_rounds=mentor_max_rounds))
    assert mentoring.queue(ap, node, source)
    assert ap.ledger.transition(node["id"], "review_queued", "implementing", fix_rounds=1)
    node = ap.ledger.get("nodes", node["id"])
    candidate = ap.ledger.submit(node, workflow, dict(contract=node["spec"]["contract"]))
    fingerprint = digest(source["payload"]["blocking"])
    assert ap.ledger.transition(node["id"], "submitted", "reviewing", no_progress=no_progress, last_failure=fingerprint)
    latest = ap.ledger.record("reviews", candidate_id=candidate["id"], provider="claude", generation=1,
                              state="completed", payload=dict(source["payload"], verdict="CHANGES_REQUESTED"))
    current = ap.ledger.get("nodes", node["id"])
    assert not mentoring.queue(ap, current, latest)
    assert ap.ledger.get("nodes", node["id"])["no_progress"] == no_progress


def test_diagnosis_keeps_unverified_review_evidence_and_primary_findings(db, git_repo):
    _, _, node, source = review_stop(db, git_repo, verdict="INCONCLUSIVE")
    context = mentoring.context(source)
    assert context["prior_unverified"] == source["payload"]["unverified"]
    assert context["prior_verdict"] == "INCONCLUSIVE"
    assert mentoring.repair_findings(node, source, dict(verdict="APPROVE", blocking=[])) == source["payload"]["blocking"]


class RecoverableReview(FixtureProvider):
    def __init__(self, verdict, mentor_verdict="CHANGES_REQUESTED"):
        super().__init__("claude")
        self.verdict, self.mentor_verdict = verdict, mentor_verdict

    async def start_review(self, candidate, policy, root, attempt, on_identity):
        self.scenario = dict(always_changes=candidate["generation"] == 1)
        result = await super().start_review(candidate, policy, root, attempt, on_identity)
        if candidate["generation"] == 1:
            result["verdict"] = self.mentor_verdict if candidate.get("mentor_context") else self.verdict
        return result


@pytest.mark.parametrize("verdict", ["NEEDS_HUMAN", "INCONCLUSIVE"])
def test_semantic_review_diagnosis_repairs_same_thread_and_requires_new_acceptance(
    make_manager, git_repo, fake_codex_state, controlled_install, verdict,
):
    async def scenario():
        manager = make_manager()
        provider = RecoverableReview(verdict)
        ap = controlled_install(manager, provider, changing=True)
        identity = await register(ap, plan(git_repo))
        await drive(manager, lambda: ap.ledger.view(identity)["state"] == "completed")
        node = ap.ledger.view(identity)["nodes"][0]
        mentors = ap.ledger.rows("reviews", "state='mentored'")
        assert len(mentors) == 1 and node["fix_rounds"] == 1
        assert node["candidate_id"] != mentors[0]["candidate_id"]
        acceptance = ap.ledger.rows("acceptances")
        assert len(acceptance) == 1 and acceptance[0]["candidate_id"] == node["candidate_id"]
        turns = [json.loads(line) for line in (fake_codex_state / "autopilot-implementations.jsonl").read_text().splitlines()]
        assert len(turns) == 2 and len({turn["thread"] for turn in turns}) == 1
        assert "Persistent fixture finding" in turns[-1]["prompt"]
        await manager.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("verdict,state", [
    ("NEEDS_HUMAN", "blocked_decision"), ("INCONCLUSIVE", "blocked_infrastructure"),
])
def test_preexisting_review_stop_resumes_after_restart(
    make_manager, git_repo, controlled_install, monkeypatch, verdict, state,
):
    async def scenario():
        manager = make_manager()
        queue, recover = mentoring.queue, mentoring.recover
        # Emulate a prior release that saved the review stop without consulting a mentor.
        monkeypatch.setattr(mentoring, "queue", lambda *args: False)
        monkeypatch.setattr(mentoring, "recover", lambda *args: False)
        ap = controlled_install(manager, RecoverableReview(verdict), changing=True)
        identity = await register(ap, plan(git_repo))
        await drive(manager, lambda: ap.ledger.view(identity)["nodes"][0]["state"] == state)
        old_node = ap.ledger.view(identity)["nodes"][0]
        thread = manager.get(old_node["task_id"])["codex_thread_id"]
        await ap.shutdown()
        monkeypatch.setattr(mentoring, "queue", queue)
        monkeypatch.setattr(mentoring, "recover", recover)
        replacement = controlled_install(manager, RecoverableReview(verdict), changing=True)
        await drive(manager, lambda: replacement.ledger.view(identity)["state"] == "completed")
        node = replacement.ledger.view(identity)["nodes"][0]
        assert node["task_id"] == old_node["task_id"]
        assert manager.get(node["task_id"])["codex_thread_id"] == thread
        assert len(replacement.ledger.rows("reviews", "state='mentored'")) == 1
        assert node["fix_rounds"] == 1
        await manager.shutdown()

    asyncio.run(scenario())


def test_diagnosis_confirming_owner_requirement_stops_without_reconsulting(make_manager, git_repo, controlled_install):
    async def scenario():
        manager = make_manager()
        ap = controlled_install(manager, RecoverableReview("NEEDS_HUMAN", "NEEDS_HUMAN"), changing=True)
        identity = await register(ap, plan(git_repo))
        await drive(manager, lambda: ap.ledger.view(identity)["nodes"][0]["state"] == "blocked_decision")
        node = ap.ledger.view(identity)["nodes"][0]
        assert len(ap.ledger.rows("reviews", "state='mentored'")) == 1
        assert node["fix_rounds"] == 0 and not mentoring.recover(ap, node)
        before = len(ap.ledger.rows("reviews"))
        for _ in range(3):
            manager.tick()
        assert len(ap.ledger.rows("reviews")) == before
        assert not ap.ledger.rows("acceptances")
        await manager.shutdown()

    asyncio.run(scenario())


def test_manual_contract_keeps_final_acceptance_blocked_after_successful_mentor_repair(
    make_manager, git_repo, controlled_install,
):
    async def scenario():
        manager = make_manager()
        ap = controlled_install(manager, RecoverableReview("NEEDS_HUMAN"), changing=True)
        raw = plan(git_repo)
        raw["nodes"][0]["contract"] = dict(manual_approval=True)
        identity = await register(ap, raw)
        await drive(manager, lambda: ap.ledger.view(identity)["nodes"][0]["state"] == "blocked_decision")
        node = ap.ledger.view(identity)["nodes"][0]
        assert node["reason"].startswith("Candidate owner acceptance required")
        assert len(ap.ledger.rows("reviews", "state='mentored'")) == 1 and node["fix_rounds"] == 1
        assert node["spec"]["contract"]["manual_approval"] is True
        assert not mentoring.recover(ap, node) and not ap.ledger.rows("acceptances")
        before = len(ap.ledger.rows("reviews"))
        for _ in range(3):
            manager.tick()
        assert len(ap.ledger.rows("reviews")) == before
        await manager.shutdown()

    asyncio.run(scenario())


def test_no_progress_diagnosis_still_stops_before_another_implementation(make_manager, git_repo, controlled_install):
    async def scenario():
        manager = make_manager()
        ap = controlled_install(manager, MentorFixture(repeated=True), changing=True)
        identity = await register(ap, plan(git_repo))
        await drive(manager, lambda: ap.ledger.view(identity)["nodes"][0]["state"] == "blocked_no_progress")
        node = ap.ledger.view(identity)["nodes"][0]
        assert len(ap.ledger.rows("reviews", "state='mentored'")) == 1
        assert (node["fix_rounds"], node["no_progress"]) == (1, 2)
        assert len(ap.ledger.rows("operations", "kind='fix'")) == 1
        assert not ap.ledger.rows("acceptances")
        await manager.shutdown()

    asyncio.run(scenario())
