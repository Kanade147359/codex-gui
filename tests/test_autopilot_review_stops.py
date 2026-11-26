"""Reduced, sanitized saved failures; no live DB, worktree, CLI, or model calls."""

import asyncio
import copy
import json
from pathlib import Path

import pytest

from app.autopilot import goals
from app.autopilot.git import git, sha, scope, snapshot, submit_head
from app.autopilot.providers import ClaudeProvider, CodexProvider, ProviderFailure, review_schema, validate_result
from app.autopilot.review_context import partitions, prompt, review_all
from app.autopilot.review_summary import summarize


FIXTURES = json.loads((Path(__file__).parent / "fixtures/autopilot/review_stops.json").read_text())


@pytest.mark.parametrize("path", ["成果.md", "audit/report.md "])
def test_required_artifact_names_preserved_in_fixed_review_snapshot(git_repo, tmp_path, path):
    async def run():
        await git(git_repo, "config", "core.quotePath", "true")
        base = await sha(git_repo)
        target = git_repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("Pinned companion evidence.\n")
        await git(git_repo, "add", "--", path)
        await git(git_repo, "commit", "-qm", "required companion")
        head = await sha(git_repo)
        candidate = dict(payload=dict(changed_paths=[path], scope=await scope(git_repo, base, head),
                                      contract=dict(required_paths=[path])))
        source = await snapshot(str(git_repo), head, base, tmp_path / "fixed")
        _, related = await partitions(candidate, source)
        assert [(item["path"], item["text"]) for item in related] == [(path, target.read_text())]

    asyncio.run(run())


def approved(candidate):
    return dict(candidate_id=candidate["id"], run_generation=candidate["run_generation"],
                scope=copy.deepcopy(candidate["payload"]["scope"]), verdict="APPROVE",
                blocking=[], nonblocking=[], resolved=[], coverage=candidate["payload"]["changed_paths"],
                unverified=[], requested_reads=[], goal_review=dict(version=2, requirements_digest="requirements-fixed",
                    assessments={axis: dict(status="ALIGNED", goal_ids=["G-EVIDENCE", "I-FAIL-CLOSED", "I-NO-MERGE", "I-AUTHORITY", "G-INTEGRATION"],
                                           evidence="Inspected fixed audit artifacts.") for axis in goals.AXES}, root_causes=[]))


@pytest.mark.parametrize("name", ["R149", "R157", "R160"])
def test_saved_incomplete_approvals_still_rejected_with_specific_gaps(name):
    f = FIXTURES[name]
    with pytest.raises(ValueError, match="APPROVE has incomplete evidence") as error:
        validate_result(f["result"], f["candidate"])
    for gap in f["result"]["unverified"]:
        assert gap in str(error.value)


def test_r158_acceptance_id_is_not_a_goal_id_in_schema_or_validator():
    f = FIXTURES["R158"]
    with pytest.raises(ValueError, match="source-backed IDs") as error:
        validate_result(f["result"], f["candidate"])
    assert "task_correctness" in str(error.value)
    assert "AC-PR158-INDEPENDENT-AUDIT" in str(error.value)
    schema = review_schema(f["candidate"])
    field = schema["properties"]["goal_review"]["properties"]["assessments"]["properties"]["task_correctness"]
    assert set(field["properties"]["goal_ids"]["items"]["enum"]) == {g["id"] for g in f["candidate"]["payload"]["requirements"]["goals"]}
    assert "enum" not in goals.ASSESSMENT_SCHEMA["properties"]["goal_ids"]["items"]


def test_r161_schema_pins_identity_and_validator_names_mismatched_field():
    f = FIXTURES["R161"]
    with pytest.raises(ValueError, match="candidate/scope/generation mismatch") as error:
        validate_result(f["result"], f["candidate"])
    assert "scope.head_sha" in str(error.value)
    schema = review_schema(f["candidate"])["properties"]
    assert schema["candidate_id"]["enum"] == [f["candidate"]["id"]]
    assert schema["run_generation"]["enum"] == [1]
    for key, value in f["candidate"]["payload"]["scope"].items():
        assert schema["scope"]["properties"][key]["enum"] == [value]


@pytest.mark.parametrize("mutation", ["candidate", "generation", "scope", "missing_evidence", "source_id"])
def test_stale_or_invalid_evidence_never_becomes_approval(mutation):
    candidate = copy.deepcopy(FIXTURES["R161"]["candidate"])
    value = approved(candidate)
    assert validate_result(value, candidate)["verdict"] == "APPROVE"
    if mutation == "candidate": value["candidate_id"] = "old-candidate"
    elif mutation == "generation": value["run_generation"] = 0
    elif mutation == "scope": value["scope"]["head_sha"] = "0" * 40
    elif mutation == "missing_evidence": value["unverified"] = ["required test log"]
    else: value["goal_review"]["assessments"]["task_correctness"]["goal_ids"] = ["invented"]
    envelope = dict(type="result", subtype="success", is_error=False, session_id="new-session", structured_output=value)
    with pytest.raises(ProviderFailure):
        ClaudeProvider().consume_events(json.dumps(envelope), "2.1.185", candidate)


def test_r157_subject_finding_is_not_an_audit_task_root_cause():
    candidate = FIXTURES["R157"]["candidate"]
    value = approved(candidate)
    value["goal_review"]["root_causes"] = [FIXTURES["R157"]["orphan_root_cause"]]
    with pytest.raises(ValueError, match="each blocking finding"):
        validate_result(value, candidate)


@pytest.mark.parametrize("name", ["R149", "R157", "R160"])
def test_required_companion_artifacts_arrive_in_every_partition_and_overall(git_repo, tmp_path, name):
    async def run():
        candidate = copy.deepcopy(FIXTURES[name]["candidate"])
        base = await sha(git_repo)
        artifact = FIXTURES["R160"]["subject_artifact"] if name == "R160" else dict(verdict="NO_NEW_BLOCKER_FOUND", findings=[])
        bodies = {"REPORT.md": "Audit evidence; subject code acceptance is separate.\n",
                  "HANDOFF.md": "Subject verdict: " + artifact["verdict"] + "\n",
                  "RESULTS.json": json.dumps(artifact) + "\n"}
        for path in candidate["payload"]["changed_paths"]:
            target = git_repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(bodies[target.name])
        await git(git_repo, "add", "audit")
        await git(git_repo, "commit", "-qm", "sanitized audit artifacts")
        head = await sha(git_repo)
        candidate["payload"].update(scope=await scope(git_repo, base, head),
            contract=dict(required_paths=candidate["payload"]["changed_paths"]), acceptance="Submit the independent audit.",
            recorded_completion=dict(parsed_result=dict(status="SUCCESS", reason="Audit submitted."), origin="strict_final_assistant_json"))
        source = await snapshot(str(git_repo), head, base, tmp_path / "fixed")
        calls = []

        class EvidenceProvider:
            async def start_review(self, part, policy, root, attempt, on_identity):
                data = json.loads(await prompt(part, root))
                related = {item["path"]: item["text"] for item in data["context"]["related"]}
                # Existence/size checks do not substitute for document contents.
                assert related == {path: bodies[Path(path).name] for path in candidate["payload"]["changed_paths"]}
                assert all(item["eof"] for item in data["context"]["related"])
                assert data["recorded_completion"]["parsed_result"]["status"] == "SUCCESS"
                assert json.loads(related[f"audit/{name}/RESULTS.json"]) == artifact
                calls.append(data)
                return approved(part)

        result = await review_all(EvidenceProvider(), candidate, {}, source,
            dict(id="attempt", event_log=str(tmp_path / "events.jsonl")), lambda _: None)
        assert validate_result(goals.terminal_fields(result, candidate), candidate)["verdict"] == "APPROVE"
        assert len(calls) == 4  # Three partitions plus whole-scope review.
        if name == "R160":
            saved = json.loads((source / "audit/R160/RESULTS.json").read_text())
            assert saved["verdict"] == "CHANGES_REQUIRED"
            assert [f["id"] for f in saved["findings"]] == ["R160-01", "R160-02"]
            assert result["blocking"] == []  # These are subject-code findings, not defective audit delivery.
    asyncio.run(run())


def test_r150_ownership_rejection_names_base_head_and_unowned_paths(git_repo):
    async def run():
        base = await sha(git_repo)
        # Reproduce a docs-only tip whose parent includes changes outside frozen ownership.
        (git_repo / "README.md").write_text("unowned parent change\n")
        await git(git_repo, "add", "README.md")
        await git(git_repo, "commit", "-qm", "different branch base")
        folder = git_repo / "audit/R150"
        folder.mkdir(parents=True)
        (folder / "REPORT.md").write_text("audit\n")
        await git(git_repo, "add", "audit")
        await git(git_repo, "commit", "-qm", "owned audit tip")
        head = await sha(git_repo)
        with pytest.raises(ValueError, match="outside approved edit ownership") as error:
            await submit_head(dict(worktree=str(git_repo), base_sha=base), dict(paths=FIXTURES["R150"]["owned_paths"]), True)
        assert all(x in str(error.value) for x in (base, head, "README.md"))
        assert await sha(git_repo) == head
        assert not (await git(git_repo, "status", "--porcelain"))[1]
    asyncio.run(run())


def test_r150_summary_does_not_diagnose_an_ownership_failure_as_model_settings():
    node = dict(id="w:R150", name="R150", state="blocked_configuration", candidate_id=None,
                spec=dict(manual_gates=[]), reason="candidate contains changes outside approved edit ownership")
    action = summarize(dict(nodes=[node]))["items"][0]["action"]
    assert "モデル" not in action
    assert "担当範囲" in action and "比較基点" in action


def test_old_turn_response_cannot_overwrite_current_review_text():
    async def run():
        candidate = FIXTURES["R161"]["candidate"]
        current = approved(candidate)
        queue = asyncio.Queue()
        for turn, value in [("current", current), ("old", FIXTURES["R161"]["result"])]:
            await queue.put(("item/completed", dict(turnId=turn, item=dict(type="agentMessage", text=json.dumps(value)))))
        await queue.put(("turn/completed", dict(turn=dict(id="old", status="completed"))))
        await queue.put(("turn/completed", dict(turn=dict(id="current", status="completed"))))
        result = await CodexProvider().consume_events(queue, candidate, "current")
        assert result == current
    asyncio.run(run())
