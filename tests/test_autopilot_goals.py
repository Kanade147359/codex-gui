"""Versioned goal review, root-cause proposals and transactional graph regressions."""
import asyncio
import copy
import json
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
from fastapi.testclient import TestClient
from app.main import create_app
from app.autopilot import goals, planning, replanning
from app.autopilot.controller import Autopilot
from app.autopilot.policy import canonical, digest, manifest
from app.autopilot.providers import validate_result, review_schema, ProviderFailure
from app.autopilot.review_context import prompt, review_all
from app.autopilot.runner import run_bounded
from app.autopilot.ledger import migrate
from autopilot_helpers import FixtureProvider, install, plan, register, drive
from conftest import FakeRunner
from test_autopilot_documents import api_register, api_wait


class Replanner(FixtureProvider):
    def __init__(self, **scenario):
        super().__init__("codex")
        self.scenario = scenario
        self.replans = []

    async def start_replan(self, candidate, policy, root, attempt, on_identity):
        fixed = candidate["replan_input"]
        self.replans.append(copy.deepcopy(fixed))
        session = "replanner-" + uuid.uuid4().hex
        def started(proc):
            on_identity(dict(pid=proc.pid, session=session, model="fake", version="synthetic fixture"))
        _, out, _ = await run_bounded(
            [sys.executable, str(Path(__file__).with_name("fake_autopilot_replanner.py"))],
            stdin=canonical(dict(fixed=fixed, root=str(root), session=session, **self.scenario)),
            timeout=15, on_start=started)
        return json.loads(out)


def workflow(repo, mode="auto_within_envelope"):
    (repo / "capability.txt").write_text("adapter-v2 only\n")
    subprocess.run(["git", "-C", str(repo), "add", "capability.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "Synthetic capability"], check=True)
    raw = plan(repo, ("A", "C", "X"))
    raw["nodes"][1]["depends_on"] = ["A"]
    raw["policy"]["replanning"] = mode
    raw["inputs"] = {"COMMON.md": "Every deliverable must contain COMMON-OK. Use adapter-v2.\n",
                     "A.md": "Task assumption: adapter-v1 is available. Deliver A.txt with COMMON-OK.\n"}
    raw["goals"] = [dict(id="G-adapter", kind="goal", text="Use adapter-v2.", source="COMMON.md")]
    for n in raw["nodes"]:
        n["goal_ids"] = ["G-adapter"]
        n["acceptance_ids"] = ["AC-" + n["id"]]
    raw["nodes"][0]["input_files"] = ["A.md"]
    return raw


def install_goal(manager, provider=None, **scenario):
    rp = provider or Replanner(**scenario)
    ap = install(manager, claude=FixtureProvider("claude", dict(premise=True)), codex=rp)
    return ap, rp


def node(view, name):
    return next(n for n in view["nodes"] if n["name"] == name)


def test_goal_review_rejects_common_only_violation_and_accepts_partial_goal(make_manager, git_repo):
    async def run():
        m = make_manager()
        ap = install(m)
        raw = plan(git_repo, ("A", "C"))
        raw["inputs"] = {"COMMON.md": "COMMON-OK is mandatory. Build an entire product eventually.\n"}
        wid = await register(ap, raw)
        await drive(m, lambda: ap.ledger.get("workflows", wid)["state"] == "completed")
        view = ap.ledger.view(wid)
        a = node(view, "A")
        first = a["candidates"][0]
        assert first["validation"][0]["state"] == "PASS"
        r = first["reviews"][0]["payload"]
        assert r["verdict"] == "CHANGES_REQUESTED"
        assert r["goal_review"]["assessments"]["goal_alignment"]["status"] == "VIOLATION"
        assert r["goal_review"]["root_causes"][0]["classification"] == "IMPLEMENTATION_DEFECT"
        assert a["fix_rounds"] == 1 and not view["replans"]
        # C implements one small part, with the rest of the workflow goal unfinished.
        c = node(view, "C")
        assert c["state"] == "accepted"
        assert c["candidates"][-1]["reviews"][-1]["payload"]["goal_review"]["assessments"]["goal_alignment"]["status"] == "ALIGNED"
        fixes = a["operations"]
        assert next(o for o in fixes if o["kind"] == "fix")["payload"]["thread"] == m.db.get_task(a["task_id"])["codex_thread_id"]
        await m.shutdown()
    asyncio.run(run())


def test_api_auto_replan_executes_reviewed_replacement_before_successor(settings, git_repo, tmp_path):
    raw = workflow(git_repo)
    app = create_app(settings, FakeRunner())
    with TestClient(app) as client:
        ap, rp = install_goal(app.state.manager)
        wid = api_register(client, raw)
        view = api_wait(client, wid, lambda v: v["state"] == "completed")
        a, r, c = (node(view, n) for n in ("A", "R_adapter", "C"))
        assert len(rp.replans) == len(view["graph_revisions"]) == len(view["replans"]) == 1
        assert a["state"] == "superseded" and a["fix_rounds"] == 1
        assert not any(x["acceptances"] for x in a["candidates"])
        original_task = app.state.manager.db.get_task(a["task_id"])
        assert original_task["task_outcome"] != "success"
        from app.task_manager import _Turn, TaskError
        with pytest.raises(TaskError, match="replaced"):
            ap.queue_owner_turn(original_task, _Turn("Resume obsolete source", resume_thread=original_task["codex_thread_id"]), {})
        assert r["state"] == c["state"] == "accepted"
        assert c["spec"]["depends_on"] == ["R_adapter"]
        accepted = r["candidates"][-1]
        assert c["inputs"][0]["candidate_id"] == accepted["id"]
        assert c["inputs"][0]["head_sha"] == accepted["payload"]["scope"]["head_sha"]
        assert accepted["validation"][0]["state"] == "PASS" and accepted["acceptances"]
        assert accepted["payload"]["requirements"]["goal_ids"] == ["G-adapter"]
        assert accepted["payload"]["requirements"]["acceptance_ids"] == ["AC-A"]
        assert r["spec"]["contract"] == a["spec"]["contract"]
        assert view["graph_revisions"][0]["state"] == "applied"
        assert rp.replans[0]["history"] and rp.replans[0]["prior_reviews"]
        for _ in range(3):
            client.portal.call(app.state.manager.tick)
        assert len(ap.ledger.view(wid)["nodes"]) == 4
        (tmp_path / "goal-replanning-e2e.json").write_text(canonical(view))


@pytest.mark.parametrize("mode,paths", [("propose_only", ["A.txt"]), ("auto_within_envelope", ["A.txt", "unauthorized.txt"])])
def test_owner_proposals_do_not_stop_independent_work(settings, git_repo, mode, paths):
    app = create_app(settings, FakeRunner())
    with TestClient(app) as client:
        ap, rp = install_goal(app.state.manager, paths=paths)
        wid = api_register(client, workflow(git_repo, mode))
        view = api_wait(client, wid, lambda v: bool(v["graph_revisions"]) and node(v, "X")["state"] == "accepted")
        graph = view["graph_revisions"][0]
        assert graph["state"] == "awaiting_owner"
        assert node(view, "A")["state"] == "blocked_replan"
        assert node(view, "C")["state"] in ("pending", "blocked_dependencies")
        assert len(view["nodes"]) == 3
        url = f'/api/autopilot/workflows/{wid}/graph-revisions/{graph["id"]}/decision'
        assert client.post(url, json=dict(owner_action=True, expected_target="stale", action="approve", note="test")).status_code == 400
        assert client.post(url, json=dict(owner_action=True, expected_target=graph["decision_target"], action="approve", note="Reviewed scope and exact proposed graph")).status_code == 200
        done = api_wait(client, wid, lambda v: v["state"] == "completed")
        assert done["graph_revisions"][0]["state"] == "applied"
        assert any(d["payload"].get("purpose") == "graph_revision" for d in node(done, "A")["decisions"])
        assert client.post(url, json=dict(owner_action=True, expected_target=graph["decision_target"], action="approve", note="duplicate")).status_code == 400


def review_value(candidate):
    return dict(candidate_id=candidate["id"], run_generation=candidate["run_generation"], scope=candidate["payload"]["scope"],
                verdict="APPROVE", blocking=[], nonblocking=[], resolved=[], coverage=["A.txt"], unverified=[],
                goal_review=dict(version=2, requirements_digest="fixed", assessments={k:dict(status="ALIGNED", goal_ids=[], evidence="COMMON.md line 1 checked") for k in goals.AXES}, root_causes=[]))


def review_candidate():
    return dict(id="candidate", run_generation=1, payload=dict(scope=dict(base_sha="base", head_sha="head", tree_sha="tree", diff_digest="diff"), changed_paths=["A.txt"],
        requirements=dict(version=2, digest="fixed", documents=[dict(name="documents/COMMON.md", body="No forbidden changes")], goals=[])))


def test_review_version_digest_axes_and_classifications_fail_closed():
    c = review_candidate()
    value = review_value(c)
    assert validate_result(value, c)
    assert "goal_review" in review_schema(c)["required"]
    mutations = [lambda v:v.pop("goal_review"), lambda v:v["goal_review"].update(version=1),
                 lambda v:v["goal_review"].update(requirements_digest="stale"),
                 lambda v:v["goal_review"]["assessments"]["architectural_consistency"].update(status="UNKNOWN"),
                 lambda v:v["goal_review"]["assessments"]["goal_alignment"].update(status="VIOLATION"),
                 lambda v:v["goal_review"]["assessments"]["goal_alignment"].update(goal_ids=["invented"])]
    for mutate in mutations:
        bad = copy.deepcopy(value)
        mutate(bad)
        with pytest.raises(ValueError): validate_result(bad, c)
    for classification in goals.CLASSIFICATIONS:
        negative = copy.deepcopy(value)
        negative.update(verdict="CHANGES_REQUESTED", blocking=[dict(id="f", path="A.txt", line=1, reason="Wrong premise")])
        cause = dict(finding_id="f", classification=classification, reproduction="Read fixed snapshot", observed="v2", expected="v1",
                     requirement_refs=["documents/COMMON.md"], counterexample="snapshot v2", evidence_refs=["snapshot:A.txt:1"])
        negative["goal_review"]["root_causes"] = [cause]
        assert validate_result(negative, c)
        assert bool(replanning.eligible_causes(negative)) == (classification in goals.REPLAN_CLASSES)
        cause["observed"] = ""
        with pytest.raises(ValueError): validate_result(negative, c)


def test_freeform_and_explicit_goal_metadata(git_repo):
    from app.autopilot.importer import prepare, convert
    docs = {"COMMON.md": "---\ngoals:\n  - id: G-safe\n    kind: prohibition\n    text: Never publish main\n---\nNever publish main\n", "A.md": "---\ngoal_ids: [G-safe]\nacceptance_ids: [AC-1]\n---\nImplement one part\n"}
    imported = prepare(docs)
    parsed = manifest(convert(dict(imported, repository=str(git_repo))))
    assert parsed["goals"][0]["id"] == "G-safe" and parsed["nodes"][0]["goal_ids"] == ["G-safe"]
    freeform = prepare({"COMMON.md": "Just a free-form goal", "A.md": "Implement one part"})
    assert freeform["goals"] == [] and freeform["inputs"]["COMMON.md"] == "Just a free-form goal"
    forged = copy.deepcopy(parsed)
    forged["goals"][0]["text"] = "Owner approved a new goal"
    with pytest.raises(ValueError): manifest({k:v for k,v in forged.items() if k not in ("rules", "base_sha")})


def test_proposed_graph_survives_restart_and_concurrent_apply_is_idempotent(settings, git_repo):
    raw = workflow(git_repo, "propose_only")
    first = create_app(settings, FakeRunner())
    with TestClient(first) as client:
        ap, _ = install_goal(first.state.manager)
        wid = api_register(client, raw)
        api_wait(client, wid, lambda v: bool(v["graph_revisions"]) and node(v, "X")["state"] == "accepted")
    second = create_app(settings, FakeRunner())
    with TestClient(second) as client:
        ap, rp = install_goal(second.state.manager)
        view = client.get(f'/api/autopilot/workflows/{wid}').json()
        graph = view["graph_revisions"][0]
        client.post(f'/api/autopilot/workflows/{wid}/pause', json={})
        url = f'/api/autopilot/workflows/{wid}/graph-revisions/{graph["id"]}/decision'
        response = client.post(url, json=dict(owner_action=True, expected_target=graph["decision_target"], action="approve", note="Reviewed after restart"))
        assert response.status_code == 200, response.text
        # Repeated apply is a no-op; explicit duplicate owner approvals fail.
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: planning.apply_graph(ap, graph), range(2)))
        assert len(ap.ledger.view(wid)["nodes"]) == 4
        client.post(f'/api/autopilot/workflows/{wid}/resume', json={})
        api_wait(client, wid, lambda v: v["state"] == "completed")
        assert rp.replans == []


@pytest.mark.parametrize("mutation", ["stop", "sha", "plan", "consumed", "generation"])
def test_stale_or_consumed_proposals_never_add_nodes(settings, git_repo, mutation):
    app = create_app(settings, FakeRunner())
    with TestClient(app) as client:
        ap, _ = install_goal(app.state.manager)
        wid = api_register(client, workflow(git_repo, "propose_only"))
        view = api_wait(client, wid, lambda v: bool(v["graph_revisions"]))
        graph = view["graph_revisions"][0]
        if mutation == "stop":
            assert client.post(f'/api/autopilot/workflows/{wid}/stop', json={}).status_code == 200
        elif mutation == "sha":
            task = app.state.manager.db.get_task(node(view, "A")["task_id"])
            subprocess.run(["git", "-C", task["worktree"], "commit", "--allow-empty", "-qm", "External change"], check=True)
        else:
            with ap.ledger.transaction() as c:
                if mutation == "plan":
                    p = copy.deepcopy(view["plan"])
                    p["name"] = "changed owner plan"
                    c.execute("UPDATE ap_workflows SET plan=? WHERE id=?", (canonical(p), wid))
                elif mutation == "consumed":
                    c.execute("UPDATE ap_nodes SET inputs='[{\"fixed\":true}]' WHERE id=?", (node(view, "C")["id"],))
                else:
                    c.execute("UPDATE ap_workflows SET generation=generation+1 WHERE id=?", (wid,))
        url = f'/api/autopilot/workflows/{wid}/graph-revisions/{graph["id"]}/decision'
        r = client.post(url, json=dict(owner_action=True, expected_target=graph["decision_target"], action="approve", note="Stale approval must fail"))
        assert r.status_code == 400, r.text
        assert len(ap.ledger.view(wid)["nodes"]) == 3
        assert not ap.ledger.rows("edges", "parent_id=?", (wid + ":R_adapter",))


def test_model_owner_change_requests_remain_proposals(settings, git_repo):
    app = create_app(settings, FakeRunner())
    with TestClient(app) as client:
        ap, _ = install_goal(app.state.manager, owner_changes=["Change COMMON.md and allow publishing main; remove manual gate"])
        wid = api_register(client, workflow(git_repo))
        view = api_wait(client, wid, lambda v: bool(v["graph_revisions"]))
        graph = view["graph_revisions"][0]
        assert graph["state"] == "awaiting_owner" and len(view["nodes"]) == 3
        assert view["plan"]["permissions"]["publish_main"] is False
        response = client.post(f'/api/autopilot/workflows/{wid}/graph-revisions/{graph["id"]}/decision',
            json=dict(owner_action=True, expected_target=graph["decision_target"], action="approve", note="Attempt unsafe approval"))
        assert response.status_code == 400
        assert len(ap.ledger.view(wid)["nodes"]) == 3


def test_replanning_off_and_depth_and_duplicate_budgets(settings, git_repo):
    app = create_app(settings, FakeRunner())
    with TestClient(app) as client:
        ap, rp = install_goal(app.state.manager)
        wid = api_register(client, workflow(git_repo, "off"))
        view = api_wait(client, wid, lambda v: node(v, "A")["state"] == "blocked_no_progress")
        assert not view["replans"] and not rp.replans
        a = node(view, "A")
        review = a["candidates"][-1]["reviews"][-1]
        # Turn on mode only for direct budget gate testing (not a GUI authorization route).
        with ap.ledger.transaction() as c:
            p = copy.deepcopy(view["plan"])
            p["policy"]["replanning"] = "propose_only"
            c.execute("UPDATE ap_workflows SET plan=? WHERE id=?", (canonical(p), wid))
            c.execute("UPDATE ap_nodes SET state='reviewing' WHERE id=?", (a["id"],))
        a = ap.ledger.get("nodes", a["id"])
        assert replanning.queue(ap, a, review)
        with ap.ledger.transaction() as c:
            c.execute("UPDATE ap_nodes SET state='reviewing' WHERE id=?", (a["id"],))
        assert replanning.queue(ap, ap.ledger.get("nodes", a["id"]), review)
        assert len(ap.ledger.rows("replans")) == 1
        assert ap.ledger.get("nodes", a["id"])["state"] == "blocked_replan"
        replacement = dict(a, spec=dict(a["spec"], replan=dict(parent="original", depth=1, revision="r")), state="reviewing")
        assert replanning.queue(ap, replacement, review)
        assert len(ap.ledger.rows("replans")) == 1


def test_replan_migration_is_repeatable(db):
    migrate(db)
    migrate(db)
    with db._lock:
        assert db._conn.execute("SELECT count(*) FROM ap_replans").fetchone()[0] == 0
        assert db._conn.execute("SELECT count(*) FROM ap_graph_revisions").fetchone()[0] == 0


def test_atomic_graph_commit_race_and_rollback(settings, git_repo, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace
    from app.database import Database
    from app.autopilot.ledger import Ledger
    import sqlite3
    real_apply = planning.apply_graph
    monkeypatch.setattr(planning, "apply_graph", lambda *a, **k: None)
    app = create_app(settings, FakeRunner())
    with TestClient(app) as client:
        ap, _ = install_goal(app.state.manager)
        wid = api_register(client, workflow(git_repo))
        view = api_wait(client, wid, lambda v: bool(v["graph_revisions"]))
        client.post(f'/api/autopilot/workflows/{wid}/pause', json={})
        graph = view["graph_revisions"][0]
        assert graph["state"] == "ready"
        before_edges = ap.ledger.rows("edges")
        with ap.ledger.transaction() as c:
            c.execute("CREATE TRIGGER fail_new_node BEFORE INSERT ON ap_nodes WHEN NEW.name='R_adapter' BEGIN SELECT RAISE(ABORT,'injected crash'); END")
        with pytest.raises(sqlite3.IntegrityError): real_apply(ap, graph)
        assert len(ap.ledger.view(wid)["nodes"]) == 3
        assert ap.ledger.rows("edges") == before_edges
        assert ap.ledger.get("workflows", wid)["plan"] == view["plan"]
        assert ap.ledger.get("graph_revisions", graph["id"])["state"] == "ready"
        with ap.ledger.transaction() as c:
            c.execute("DROP TRIGGER fail_new_node")
        db2 = Database(settings.db_path)
        try:
            other = SimpleNamespace(ledger=Ledger(db2), manager=ap.manager)
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda ctrl: real_apply(ctrl, graph), [ap, other]))
            assert len(ap.ledger.view(wid)["nodes"]) == 4
            assert ap.ledger.get("nodes", wid + ":A")["state"] == "awaiting_replacement"
            assert not ap.ledger.rows("acceptances", "candidate_id=?", (node(view, "A")["candidate_id"],))
        finally:
            db2.close()
        monkeypatch.setattr(planning, "apply_graph", real_apply)
        client.post(f'/api/autopilot/workflows/{wid}/resume', json={})
        api_wait(client, wid, lambda v: v["state"] == "completed")


def test_proposal_schema_cannot_expand_permissions_or_budgets():
    fixed = dict(digest="fixed", scope=dict(head_sha="a" * 40), source_node="A",
                 causes=[dict(finding_id="cause", evidence_refs=["snapshot:x:1"])], evidence={"snapshot:x:1": {}},
                 original_task=dict(goal_ids=["G"]), validation_profiles=[{}])
    value = dict(version=1, input_digest="fixed", candidate_sha="a" * 40, confirmed_causes=["cause"],
                 evidence_refs=["snapshot:x:1"], analysis="Observed counterexample", owner_changes=[], tasks=[dict(
                     task_id="R", title="Fix", purpose="Fix cause", parent_task="A", finding_id="cause", root_cause="Wrong assumption",
                     goal_ids=["G"], implementation="Use actual supported input", paths=["x"], depends_on=[], validation_profile=0,
                     completion_conditions=["Original contract"], relation="replace", rationale="Counterexample")])
    assert replanning.validate_proposal(value, fixed)
    mutations = [lambda v:v.update(permissions=dict(publish_main=True)), lambda v:v.update(tasks=v["tasks"]*4),
                 lambda v:v["tasks"][0].update(manual_gates=[]), lambda v:v.update(candidate_sha="stale"),
                 lambda v:v.update(evidence_refs=["invented"]), lambda v:v.update(confirmed_causes=["invented"]),
                 lambda v:v["tasks"][0].update(validation_profile=99), lambda v:v["tasks"][0].update(goal_ids=["invented"])]
    for mutate in mutations:
        bad = copy.deepcopy(value)
        mutate(bad)
        with pytest.raises(ValueError): replanning.validate_proposal(bad, fixed)


def test_first_review_can_request_root_cause_analysis(settings, git_repo):
    app = create_app(settings, FakeRunner())
    with TestClient(app) as client:
        ap, rp = install_goal(app.state.manager)
        ap.providers["claude"].scenario["premise_generation"] = 1
        wid = api_register(client, workflow(git_repo, "propose_only"))
        view = api_wait(client, wid, lambda v: bool(v["graph_revisions"]))
        assert node(view, "A")["fix_rounds"] == 0
        assert len(rp.replans) == 1


def test_large_required_context_never_silently_approves():
    c = review_candidate()
    c["payload"].update(contract={}, acceptance="One part", review_context={})
    c["payload"]["requirements"]["documents"] = [dict(name=f"doc{i}", body="x"*262144) for i in range(5)]
    with pytest.raises(ValueError, match="exceeds 1 MiB"):
        asyncio.run(prompt(c, Path("/tmp")))


def test_overall_goal_approval_cannot_erase_inconclusive_partition(git_repo, tmp_path):
    from app.autopilot.git import git, sha, scope
    async def run():
        base = await sha(git_repo)
        for name in ("A.txt", "B.txt"):
            (git_repo / name).write_text("good\n")
        await git(git_repo, "add", ".")
        await git(git_repo, "commit", "-qm", "partition fixture")
        head = await sha(git_repo)
        c = review_candidate()
        c["payload"].update(scope=await scope(git_repo, base, head), changed_paths=["A.txt", "B.txt"], contract={}, acceptance="One part")
        class PartitionProvider:
            async def start_review(self, part, policy, root, attempt, on_identity):
                result = review_value(part)
                result["coverage"] = part["payload"]["changed_paths"]
                context = part["payload"]["review_context"]
                if context["phase"] == "partition" and context["index"] == 1:
                    result["verdict"] = "INCONCLUSIVE"
                    result["goal_review"]["assessments"]["goal_alignment"].update(status="UNKNOWN", evidence="Missing goal evidence in partition")
                return result
        result = await review_all(PartitionProvider(), c, {}, git_repo, dict(id="attempt", event_log=str(tmp_path/"events.jsonl")), lambda _:None)
        assert result["verdict"] == "INCONCLUSIVE"
        assert result["goal_review"]["assessments"]["goal_alignment"]["status"] == "UNKNOWN"
        assert len(result["partition_evidence"]) == 2
    asyncio.run(run())


def test_post_fix_validation_failure_is_diagnosed_before_repair(settings, git_repo):
    raw = workflow(git_repo, "propose_only")
    raw["nodes"][0]["validation"]["commands"] = [["python3", "-c", "from pathlib import Path; assert 'adapter-v1' in Path('capability.txt').read_text()"]]
    app = create_app(settings, FakeRunner())
    with TestClient(app) as client:
        ap, rp = install_goal(app.state.manager)
        wid = api_register(client, raw)
        view = api_wait(client, wid, lambda v: bool(v["graph_revisions"]))
        a = node(view, "A")
        assert a["fix_rounds"] == 1 and a["state"] == "blocked_replan"
        assert len(a["candidates"]) == 2
        assert all(c["validation"][0]["state"] == "FAIL" for c in a["candidates"])
        assert len(rp.replans) == 1 and rp.replans[0]["correction_instructions"]
        assert any(e.get("state") == "FAIL" for e in rp.replans[0]["evidence"].values())
        # The new plan still contains the original mandatory check, rather than silently weakening it.
        new = next(n for n in view["graph_revisions"][0]["payload"]["plan"]["nodes"] if n["id"] == "R_adapter")
        assert new["validation"] == a["spec"]["validation"]


@pytest.mark.parametrize("outcome", ["stop", "restart", "quota"])
def test_interrupted_replanner_does_not_restart_or_create_tasks(make_manager, git_repo, outcome):
    class Interrupted(Replanner):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.calls_count = 0
        async def start_replan(self, candidate, policy, root, attempt, on_identity):
            self.calls_count += 1
            self.started.set()
            if outcome == "quota":
                raise ProviderFailure("included_quota", "Synthetic confirmed included quota exhaustion")
            await asyncio.Event().wait()
    async def run():
        m = make_manager()
        provider = Interrupted()
        ap, _ = install_goal(m, provider=provider)
        wid = await register(ap, workflow(git_repo))
        await drive(m, lambda: provider.started.is_set())
        if outcome == "quota":
            await drive(m, lambda: ap.ledger.get("nodes", wid + ":A")["state"] == "blocked_replan")
        elif outcome == "stop":
            await ap.control(wid, "stop")
            assert ap.ledger.get("nodes", wid + ":A")["state"] == "stopped"
        else:
            await ap.shutdown()
            m.autopilot = Autopilot(m, providers=ap.providers)
            ap = m.autopilot
            ap.reconcile(wid)
            assert ap.ledger.get("nodes", wid + ":A")["state"] == "blocked_replan"
        for _ in range(3): m.tick()
        assert provider.calls_count == 1
        assert len(ap.ledger.view(wid)["nodes"]) == 3 and not ap.ledger.rows("graph_revisions")
        assert not ap.ledger.rows("acceptances", "candidate_id IN (SELECT id FROM ap_candidates WHERE node_id=?)", (wid + ":A",))
        await m.shutdown()
    asyncio.run(run())
