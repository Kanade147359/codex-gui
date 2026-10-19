"""Regressions for owner input preservation and private validator runtime staging."""

import asyncio
import json
import os
from pathlib import Path
import stat
import subprocess

import pytest

from app.autopilot import environment, planning
from app.autopilot.git import git, sha
from app.autopilot.inputs import materialize, requirements, verify
from app.autopilot.policy import canonical, digest, profile
from app.autopilot.runner import InfrastructureUnavailable, Validator
from app.models import now_iso
from autopilot_helpers import install, plan


async def accepted_planning_artifact(manager, repository, change, original=None):
    """Provide an already accepted, committed planner artifact to the real revision ledger."""
    validation = dict(commands=[["python3", "-c", "assert True"]], artifacts=["A.txt"])
    revision = dict(id="A", paths=["A.txt"], validation=validation, **change)
    (repository / "plan.json").write_text(json.dumps(dict(nodes=[revision])))
    await git(repository, "add", "plan.json")
    await git(repository, "commit", "-qm", "synthetic accepted planning output")
    raw = plan(repository, ("00", "A"))
    raw["inputs"] = {"COMMON.md": "Keep the common requirements.", "A.md": "Original owner requirement A.",
                     "B.md": "Additional owner reference B."}
    raw["nodes"][0].update(kind="report", paths=["plan.json"], plan_artifact="plan.json",
                           validation=dict(artifacts=["plan.json"]))
    raw["nodes"][1].update(draft=True, depends_on=["00"], paths=[], validation={},
                           input_files=["COMMON.md", "A.md"],
                           envelope=dict(paths=["A.txt"], validation_profiles=[validation]))
    raw["nodes"][1].update(original or {})
    ap = install(manager)
    preview = await ap.dry_run(raw)
    workflow = await ap.register(raw, preview["input_digest"])
    node_id = workflow["id"] + ":00"
    candidate_id = "accepted-planning-candidate"
    payload = dict(scope=dict(head_sha=await sha(repository)))
    with ap.ledger.transaction() as connection:
        connection.execute("UPDATE ap_workflows SET state='running' WHERE id=?", (workflow["id"],))
        connection.execute(
            "INSERT INTO ap_candidates(id,node_id,generation,run_generation,payload,state,created_at) "
            "VALUES(?,?,1,1,?,'accepted',?)", (candidate_id, node_id, canonical(payload), now_iso()))
        connection.execute("UPDATE ap_nodes SET state='accepted',candidate_id=?,generation=1 WHERE id=?",
                           (candidate_id, node_id))
    await planning.propose(ap, ap.ledger.get("nodes", node_id))
    return ap, workflow["id"], ap.ledger.rows("plan_revisions")[0]


@pytest.mark.parametrize("change,original", [
    ({"input_files": []}, {}),
    ({"input_files": ["COMMON.md", "B.md"]}, {}),
    ({"manual_gates": []}, {"manual_gates": ["owner design approval"]}),
    ({"contract": {"manual_approval": False}}, {"contract": {"manual_approval": True}}),
    ({"handoff": []}, {"handoff": ["A.txt"]}),
    ({"review_files": []}, {"review_files": ["A.txt"]}),
])
def test_planner_cannot_reduce_owner_requirements_without_exact_decision(make_manager, git_repo, change, original):
    async def scenario():
        manager = make_manager()
        ap, wid, revision = await accepted_planning_artifact(manager, git_repo, change, original)
        before = ap.ledger.get("nodes", wid + ":A")
        assert revision["state"] == "awaiting_owner"
        assert revision["payload"]["expansions"]
        with pytest.raises(ValueError, match="owner"):
            planning.apply(ap, revision)
        with pytest.raises(ValueError, match="exact"):
            planning.apply(ap, revision, approve=True, expected="stale-decision")
        assert ap.ledger.get("nodes", before["id"]) == before
        planning.apply(ap, revision, approve=True, expected=digest(revision["payload"]))
        after = ap.ledger.get("nodes", before["id"])
        assert after["state"] == "pending"
        applied = ap.ledger.get("plan_revisions", revision["id"])
        assert applied["state"] == "applied" and applied["payload"]["owner_approved"] is True
        await manager.shutdown()

    asyncio.run(scenario())


def test_saved_unsafe_ready_revision_waits_for_owner_after_upgrade(make_manager, git_repo):
    async def scenario():
        manager = make_manager()
        ap, wid, revision = await accepted_planning_artifact(manager, git_repo, {"input_files": []})
        old_payload = dict(revision["payload"], expansions=[])
        with ap.ledger.transaction() as connection:
            connection.execute("UPDATE ap_plan_revisions SET state='ready',payload=? WHERE id=?",
                               (canonical(old_payload), revision["id"]))
        before = ap.ledger.get("nodes", wid + ":A")
        planning.apply(ap, ap.ledger.get("plan_revisions", revision["id"]))
        updated = ap.ledger.get("plan_revisions", revision["id"])
        assert updated["state"] == "awaiting_owner" and updated["payload"]["expansions"]
        assert ap.ledger.get("nodes", before["id"]) == before
        with pytest.raises(ValueError, match="exact"):
            planning.apply(ap, updated, approve=True, expected=digest(old_payload))
        await manager.shutdown()

    asyncio.run(scenario())


def test_planning_refinement_preserves_original_prompt_without_requesting_approval(make_manager, git_repo):
    async def scenario():
        manager = make_manager()
        refined = "Concrete implementation steps for the assigned task."
        ap, wid, revision = await accepted_planning_artifact(manager, git_repo, {"prompt": refined})
        assert revision["state"] == "ready"
        planning.apply(ap, revision)
        child = ap.ledger.get("nodes", wid + ":A")
        bundle = await requirements(ap, child, ap.ledger.get("workflows", wid))
        assert "fixture A" in bundle["instruction"] and refined in bundle["instruction"]
        assert child["spec"]["input_files"] == ["COMMON.md", "A.md"]
        assert ap.ledger.get("plan_revisions", revision["id"])["payload"]["owner_approved"] is False
        await manager.shutdown()
    asyncio.run(scenario())


def test_additive_refinement_preserves_actual_fixed_documents(make_manager, git_repo, tmp_path):
    async def scenario():
        manager = make_manager()
        ap, wid, revision = await accepted_planning_artifact(
            manager, git_repo, {"input_files": ["COMMON.md", "A.md", "B.md"],
                               "prompt": "fixture A\nAdditional implementation details."})
        assert revision["state"] == "ready"
        planning.apply(ap, revision)
        bundle = await requirements(ap, ap.ledger.get("nodes", wid + ":A"), ap.ledger.get("workflows", wid))
        documents = {doc["name"]: doc["body"] for doc in bundle["documents"]}
        assert documents["documents/A.md"] == "Original owner requirement A."
        assert documents["documents/COMMON.md"] == "Keep the common requirements."
        assert documents["documents/B.md"] == "Additional owner reference B."
        root = tmp_path / "implementation-inputs"
        root.mkdir()
        materialize(root, bundle)
        verify(root, bundle)
        assert (root / ".autopilot-inputs/documents/A.md").read_text() == documents["documents/A.md"]
        await manager.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("name", [
    ".aws/credentials", ".npmrc", ".env", ".env.production", "codex-gui/state.sqlite",
    "nested/.ssh/id_ed25519", "nested/state.sqlite-wal",
])
def test_registered_runtime_rejects_nested_private_files_before_execution(tmp_path, monkeypatch, name):
    runtime = tmp_path / "registered-runtime"
    private = runtime / name
    private.parent.mkdir(parents=True)
    private.write_text("public-synthetic-private-marker")
    source = tmp_path / "source"
    source.mkdir()
    registered = profile(dict(commands=[["python3", "-c", "raise AssertionError('must not execute')"]],
                              environment=dict(mounts=[dict(source=str(runtime), target="/opt/validation/cache")])),
                         "code")

    async def forbidden_execution(*args, **kwargs):
        raise AssertionError("private runtime must be rejected before any preflight or candidate process")

    monkeypatch.setattr("app.autopilot.runner.run_bounded", forbidden_execution)
    with pytest.raises(InfrastructureUnavailable, match="private|GUI"):
        asyncio.run(Validator().validate(dict(payload=dict(scope={})), source, registered, tmp_path / "logs"))
    assert private.read_text() == "public-synthetic-private-marker"


@pytest.mark.parametrize("kind", ["symlink", "fifo", "hardlink"])
def test_registered_runtime_rejects_links_and_special_files(tmp_path, kind):
    runtime = tmp_path / "registered-runtime"
    runtime.mkdir()
    target = runtime / "dependency"
    outside = tmp_path / "synthetic-private"
    outside.write_text("synthetic marker")
    if kind == "symlink":
        target.symlink_to(outside)
    elif kind == "fifo":
        os.mkfifo(target)
    else:
        os.link(outside, target)
    with pytest.raises(InfrastructureUnavailable, match="links|special"):
        with environment.mounts(dict(mounts=[dict(source=str(runtime), target="/opt/validation/cache")])):
            pytest.fail("unsafe runtime must not be mounted")


def test_runtime_snapshot_keeps_original_contents_and_executable_mode(tmp_path):
    runtime = tmp_path / "registered-runtime"
    (runtime / "bin").mkdir(parents=True)
    executable = runtime / "bin/tool"
    executable.write_text("#!/bin/sh\nprintf 'offline-tool-ok'\n")
    executable.chmod(0o755)
    (runtime / "dependency.txt").write_text("fixed offline dependency")
    specification = dict(mounts=[dict(source=str(runtime), target="/opt/validation/cache")])
    with environment.mounts(specification) as arguments:
        assert arguments[0] == "--ro-bind" and arguments[2] == "/opt/validation/cache"
        staged = Path(arguments[1])
        assert staged != runtime and staged.exists()
        assert (staged / "dependency.txt").read_text() == "fixed offline dependency"
        assert stat.S_IMODE((staged / "bin/tool").stat().st_mode) == 0o555
        assert subprocess.check_output([str(staged / "bin/tool")], text=True) == "offline-tool-ok"
        # Host mutation between preflight and execution cannot add a credential or replace the input.
        (runtime / "dependency.txt").write_text("changed after inspection")
        (runtime / ".env").write_text("new synthetic marker")
        assert (staged / "dependency.txt").read_text() == "fixed offline dependency"
        assert not (staged / ".env").exists()
    assert not staged.exists()
    assert (runtime / "dependency.txt").read_text() == "changed after inspection"


def test_runtime_copy_rejects_concurrent_file_replacement(tmp_path, monkeypatch):
    runtime = tmp_path / "registered-runtime"
    runtime.mkdir()
    (runtime / "dependency.txt").write_text("original dependency")
    original_open = environment.os.open

    def replace_before_open(path, flags, *args, **kwargs):
        if path == "dependency.txt" and kwargs.get("dir_fd") is not None:
            (runtime / "dependency.txt").unlink()
            (runtime / "dependency.txt").symlink_to(tmp_path / "synthetic-private")
        return original_open(path, flags, *args, **kwargs)

    (tmp_path / "synthetic-private").write_text("synthetic marker")
    monkeypatch.setattr(environment.os, "open", replace_before_open)
    with pytest.raises(InfrastructureUnavailable, match="safely inspected"):
        with environment.mounts(dict(mounts=[dict(source=str(runtime), target="/opt/validation/cache")])):
            pytest.fail("concurrent link replacement must be rejected")
