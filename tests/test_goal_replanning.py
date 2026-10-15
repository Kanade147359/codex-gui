"""Versioned goal review and independent dynamic replanning through real GUI layers."""

import asyncio
import copy
import json

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.autopilot.git import git, sha
from app.autopilot.goals import validate_review
from app.autopilot.policy import canonical
from app.autopilot.providers import validate_result
from autopilot_helpers import plan, register, drive
from conftest import FakeRunner
from goal_helpers import install_goals, root_plan
from test_autopilot_documents import api_register, api_wait


def test_goal_schema_rejects_legacy_unknown_and_mismatched_digest(make_manager, git_repo):
    async def scenario():
        m = make_manager()
        ap = install_goals(m)
        raw = plan(git_repo, ("P",))
        raw["inputs"] = {"COMMON.md": "Implement locally; another task can finish the later export goal."}
        wid = await register(ap, raw)
        await drive(m, lambda: ap.ledger.get("workflows", wid)["state"] == "completed")
        node = ap.ledger.view(wid)["nodes"][0]
        c = node["candidates"][0]
        result = c["reviews"][-1]["payload"]
        assert c["payload"]["requirements"]["goal_index"]["declared"] == []
        assert result["goal_assessment"]["goal_alignment"]["status"] == "MATCH"
        assert "partially" in result["goal_assessment"]["goal_alignment"]["evidence"][0]["reason"]
        keys = (
            "candidate_id",
            "run_generation",
            "scope",
            "verdict",
            "blocking",
            "nonblocking",
            "resolved",
            "coverage",
            "unverified",
        )
        with pytest.raises(ValueError):
            validate_result({k: result[k] for k in keys}, c)
        for field in ("requirements_digest", "goal_index_digest"):
            bad = copy.deepcopy(result)
            bad["goal_assessment"][field] = "0" * 64
            with pytest.raises(ValueError):
                validate_review(bad, c)
        bad = copy.deepcopy(result)
        bad["goal_assessment"]["evidence_completeness"]["status"] = "UNKNOWN"
        with pytest.raises(ValueError):
            validate_review(bad, c)
        await m.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("path", ["input.csv", "../outside.db"])
def test_replanner_bounded_reads_use_fixed_snapshot_or_reject_escape(settings, counterexample_repo, path):
    app = create_app(settings, FakeRunner())
    ap = install_goals(app.state.manager, request_file=path)
    with TestClient(app) as client:
        raw = root_plan(counterexample_repo)
        raw["nodes"][0]["review_files"] = []
        wid = api_register(client, raw)
        if path == "input.csv":
            final = api_wait(client, wid, lambda v: v["state"] == "completed")
            record = next(n for n in final["nodes"] if n["name"] == "C")["replans"][0]
            packet = record["payload"]["packet"]
            assert packet["source_context"]["requested_code"][0]["text"] == "good,data\n"
            assert len([call for call in ap.providers["codex"].calls if "replan" in call]) == 2
        else:
            final = api_wait(
                client,
                wid,
                lambda v: next(n for n in v["nodes"] if n["name"] == "C")["state"] == "blocked_configuration",
            )
            assert not final["graph_revisions"] and len(final["nodes"]) == 3


def test_replanner_quota_wait_releases_shared_slots_and_stop_is_final(make_manager, counterexample_repo):
    async def scenario():
        m = make_manager(max_concurrent=3)
        ap = install_goals(m, replan_error="included_quota")
        wid = await register(ap, root_plan(counterexample_repo))
        await drive(m, lambda: ap.ledger.get("nodes", wid + ":C")["state"] == "waiting_replan_provider")
        record = ap.ledger.rows("replans")[0]
        assert record["state"] == "waiting_provider" and m._slots._value == 3
        before = len(ap.providers["codex"].calls)
        with ap.ledger.transaction() as db:
            import time

            db.execute("UPDATE ap_provider_availability SET recheck_at=?", (time.time() + 3600,))
        for _ in range(5):
            m.tick()
        assert len(ap.providers["codex"].calls) == before
        await ap.control(wid, "stop")
        with ap.ledger.transaction() as db:
            db.execute("UPDATE ap_provider_availability SET recheck_at=0")
        for _ in range(5):
            m.tick()
        assert len(ap.providers["codex"].calls) == before and not ap.ledger.rows("graph_revisions")
        assert ap.ledger.get("replans", record["id"])["state"] == "cancelled"
        await m.shutdown()

    asyncio.run(scenario())


def test_environment_diagnosis_does_not_consume_fix_or_replanning_budget(make_manager, git_repo):
    async def scenario():
        from goal_helpers import GoalReviewer

        class EnvironmentReview(GoalReviewer):
            async def start_review(self, *args):
                result = await super().start_review(*args)
                result.update(
                    verdict="CHANGES_REQUESTED",
                    blocking=[dict(id="env", path="", line=0, reason="Required observation unavailable")],
                )
                result["diagnosis"].update(classification="ENVIRONMENT_FAILURE", finding_ids=["env"])
                result["goal_assessment"]["evidence_completeness"]["status"] = "UNKNOWN"
                return result

        m = make_manager()
        ap = install_goals(m)
        ap.providers["claude"] = EnvironmentReview("claude")
        wid = await register(ap, plan(git_repo, ("P",)))
        await drive(m, lambda: ap.ledger.get("nodes", wid + ":P")["state"] == "blocked_infrastructure")
        n = ap.ledger.get("nodes", wid + ":P")
        assert n["fix_rounds"] == 0 and not ap.ledger.rows("replans") and not ap.ledger.rows("replacements")
        await m.shutdown()

    asyncio.run(scenario())


def test_common_only_prohibition_rejects_locally_passing_code_and_uses_original_thread(
    make_manager, git_repo, fake_codex_state
):
    async def scenario():
        m = make_manager()
        ap = install_goals(m)
        raw = plan(git_repo, ("G",))
        raw["inputs"] = {"COMMON.md": "No remote access. Later tasks may complete the broader export goal."}
        raw["goals"] = [
            dict(
                id="NO_REMOTE",
                kind="prohibition",
                text="No remote access",
                source="COMMON.md",
                quote="No remote access",
            )
        ]
        raw["policy"]["replanning"] = dict(mode="off")
        raw["nodes"][0].update(
            paths=["G.py"],
            goal_ids=["NO_REMOTE"],
            validation=dict(
                commands=[
                    [
                        "python3",
                        "-c",
                        "from pathlib import Path; assert 'MODE' in Path('G.py').read_text()",
                    ]
                ],
                artifacts=["G.py"],
            ),
        )
        wid = await register(ap, raw)
        await drive(m, lambda: ap.ledger.get("workflows", wid)["state"] == "completed")
        node = ap.ledger.view(wid)["nodes"][0]
        assert len(node["candidates"]) == 2 and node["fix_rounds"] == 1
        rejected = node["candidates"][0]
        assert rejected["validation"][-1]["state"] == "PASS" and not rejected["acceptances"]
        result = rejected["reviews"][-1]["payload"]
        assert result["goal_assessment"]["task_correctness"]["status"] == "MATCH"
        assert result["goal_assessment"]["goal_alignment"]["status"] == "VIOLATION"
        assert not ap.ledger.rows("replans")
        history = [
            json.loads(s) for s in (fake_codex_state / "goal-implementations.jsonl").read_text().splitlines()
        ]
        assert history[0]["thread"] == history[1]["thread"] == history[1]["resume"]
        assert history[0]["requirements"] == history[1]["requirements"]
        await m.shutdown()

    asyncio.run(scenario())


def test_root_cause_dynamic_graph_real_api_sqlite_git_and_restart(settings, git_repo, tmp_path, monkeypatch):
    async def input_commit():
        (git_repo / "input.csv").write_text("good,data\n")
        await git(git_repo, "add", "input.csv")
        await git(git_repo, "commit", "-qm", "Public immutable CSV counterexample")
        return await sha(git_repo, "main")

    initial = asyncio.run(input_commit())
    from app.autopilot import graph_revision

    original = graph_revision.checked_apply

    async def restart_before_graph(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(graph_revision, "checked_apply", restart_before_graph)
    app = create_app(settings, FakeRunner())
    ap = install_goals(app.state.manager)
    with TestClient(app) as client:
        from pathlib import Path

        files = Path(__file__).parent / "fixtures/autopilot-goals"
        imported = client.post(
            "/api/autopilot/import",
            files=[("files", (p.name, p.read_bytes(), "text/markdown")) for p in sorted(files.glob("*.md"))],
        )
        assert imported.status_code == 200, imported.text
        converted = client.post(
            "/api/autopilot/import/convert",
            json=dict(
                imported.json(),
                repository=str(git_repo),
                permissions=dict(commit_owned_changes=True),
                policy=dict(
                    extra_usage_disabled_confirmed=True, replanning=dict(mode="auto_within_envelope")
                ),
            ),
        )
        assert converted.status_code == 200, converted.text
        wid = api_register(client, converted.json()["manifest"])
        before = api_wait(client, wid, lambda v: bool(v["graph_revisions"]))
        c = next(n for n in before["nodes"] if n["name"] == "C")
        assert c["fix_rounds"] == 1 and len(c["candidates"]) == 2
        assert c["candidates"][-1]["validation"][-1]["state"] == "FAIL"
        assert app.state.manager.db.get_task(c["task_id"])["task_outcome"] != "success"
        assert not next(n for n in before["nodes"] if n["name"] == "D")["task_id"]
        client.portal.call(app.state.manager.shutdown)

    async def duplicate_apply(*args, **kwargs):
        await asyncio.gather(original(*args, **kwargs), original(*args, **kwargs))

    monkeypatch.setattr(graph_revision, "checked_apply", duplicate_apply)
    app2 = create_app(settings, FakeRunner())
    ap = install_goals(app2.state.manager)
    with TestClient(app2) as client:
        final = api_wait(client, wid, lambda v: v["state"] == "completed")
        nodes = {n["name"]: n for n in final["nodes"]}
        assert nodes["C"]["state"] == "superseded" and nodes["R"]["state"] == "accepted"
        assert (
            nodes["R"]["candidates"][-1]["payload"]["scope"]["base_sha"]
            == nodes["C"]["candidates"][-1]["payload"]["scope"]["base_sha"]
        )
        assert nodes["D"]["spec"]["depends_on"] == ["R"]
        assert (
            nodes["D"]["inputs"][0]["head_sha"]
            == nodes["R"]["candidates"][-1]["payload"]["scope"]["head_sha"]
        )
        assert app2.state.manager.db.get_task(nodes["C"]["task_id"])["task_outcome"] != "success"
        analysis = nodes["C"]["replans"][0]
        assert analysis["payload"]["evidence_supported"] is True
        assert analysis["payload"]["proposal"]["diagnosis"]["classification"] == "TASK_ASSUMPTION_FALSE"
        assert (
            analysis["payload"]["session"]
            != app2.state.manager.db.get_task(nodes["C"]["task_id"])["codex_thread_id"]
        )
        assert len(ap.ledger.rows("replacements")) == 1 and len(final["graph_revisions"]) == 1
        for _ in range(3):
            client.portal.call(ap.tick)
        assert len(ap.ledger.view(wid)["nodes"]) == 4
        assert asyncio.run(sha(git_repo, "main")) == initial
        (tmp_path / "goal-e2e.json").write_text(json.dumps(final, indent=2))


@pytest.fixture
def counterexample_repo(git_repo):
    async def create():
        (git_repo / "input.csv").write_text("good,data\n")
        await git(git_repo, "add", "input.csv")
        await git(git_repo, "commit", "-qm", "Public CSV counterexample")

    asyncio.run(create())
    return git_repo


def decision(client, wid, revision, **changes):
    payload = dict(
        owner_action=True,
        expected_target=revision["decision_target"],
        action="approve",
        note="Owner reviewed the exact graph and scope change",
    )
    payload.update(changes)
    return client.post(
        f"/api/autopilot/workflows/{wid}/graph-revisions/{revision['id']}/decision", json=payload
    )


@pytest.mark.parametrize(
    "mode,scenario", [("propose_only", {}), ("auto_within_envelope", {"outside_envelope": True})]
)
def test_replan_owner_decision_is_versioned_and_separate_from_resume(
    settings, counterexample_repo, mode, scenario
):
    app = create_app(settings, FakeRunner())
    ap = install_goals(app.state.manager, **scenario)
    with TestClient(app) as client:
        wid = api_register(client, root_plan(counterexample_repo, mode))
        view = api_wait(
            client,
            wid,
            lambda v: (
                bool(v["graph_revisions"])
                and next(n for n in v["nodes"] if n["name"] == "E")["state"] == "accepted"
            ),
        )
        revision = view["graph_revisions"][0]
        assert revision["state"] == "awaiting_owner"
        assert len(view["nodes"]) == 3 and not next(n for n in view["nodes"] if n["name"] == "D")["task_id"]
        if scenario:
            assert "unapproved edit paths" in str(revision["payload"]["expansions"])
        assert decision(client, wid, revision, expected_target="stale").status_code == 400
        assert decision(client, wid, revision, owner_action=False).status_code == 400
        assert client.post(f"/api/autopilot/workflows/{wid}/resume").status_code in (200, 404, 405)
        assert len(ap.ledger.rows("replacements")) == 0
        assert decision(client, wid, revision).status_code == 200
        assert decision(client, wid, revision).status_code == 400
        final = api_wait(client, wid, lambda v: v["state"] == "completed")
        assert len(final["nodes"]) == 4 and len(ap.ledger.rows("replacements")) == 1
        assert final["graph_revisions"][0]["payload"]["owner_decision"]["action"] == "approve"


@pytest.mark.parametrize("attack", ["weaken_validation", "drop_acceptance", "remove_goal"])
def test_model_cannot_weaken_global_goals_or_mandatory_acceptance(settings, counterexample_repo, attack):
    app = create_app(settings, FakeRunner())
    ap = install_goals(app.state.manager, **{attack: True})
    with TestClient(app) as client:
        wid = api_register(client, root_plan(counterexample_repo))
        before = api_wait(client, wid, lambda v: bool(v["graph_revisions"]))
        r = before["graph_revisions"][0]
        assert r["state"] == "awaiting_owner" and r["payload"]["hard_blocks"]
        assert decision(client, wid, r).status_code == 400
        assert len(ap.ledger.rows("replacements")) == 0
        after = api_wait(
            client, wid, lambda v: next(n for n in v["nodes"] if n["name"] == "E")["state"] == "accepted"
        )
        assert before["plan"] == after["plan"] and len(after["nodes"]) == 3
        assert decision(client, wid, r, action="reject").status_code == 200
        assert ap.ledger.get("graph_revisions", r["id"])["state"] == "rejected"


@pytest.mark.parametrize("change", ["sha", "plan", "source_inputs", "consumed", "stop"])
def test_stale_source_plan_consumed_inputs_and_stop_refuse_graph_apply(settings, counterexample_repo, change):
    app = create_app(settings, FakeRunner())
    ap = install_goals(app.state.manager)
    with TestClient(app) as client:
        wid = api_register(client, root_plan(counterexample_repo, "propose_only"))
        view = api_wait(client, wid, lambda v: bool(v["graph_revisions"]))
        r = view["graph_revisions"][0]
        c = next(n for n in view["nodes"] if n["name"] == "C")
        if change == "sha":
            task = app.state.manager.db.get_task(c["task_id"])

            async def mutate():
                from pathlib import Path

                (Path(task["worktree"]) / "C.py").write_text("# Changed after proposal\n")
                await git(task["worktree"], "add", "C.py")
                await git(task["worktree"], "commit", "-qm", "Change owner decision target")

            client.portal.call(mutate)
        elif change == "plan":
            with ap.ledger.transaction() as db:
                updated = copy.deepcopy(view["plan"])
                updated["name"] = "New plan revision"
                db.execute("UPDATE ap_workflows SET plan=? WHERE id=?", (canonical(updated), wid))
        elif change == "consumed":
            with ap.ledger.transaction() as db:
                db.execute(
                    "UPDATE ap_nodes SET inputs=? WHERE id=?",
                    (canonical([dict(immutable="already consumed")]), wid + ":D"),
                )
        elif change == "source_inputs":
            with ap.ledger.transaction() as db:
                db.execute(
                    "UPDATE ap_nodes SET inputs=? WHERE id=?",
                    (canonical([dict(changed="source accepted input")]), wid + ":C"),
                )
        else:
            client.portal.call(ap.control, wid, "stop")
        assert decision(client, wid, r).status_code == 400
        for _ in range(3):
            client.portal.call(ap.tick)
        assert len(ap.ledger.rows("replacements")) == 0 and len(ap.ledger.view(wid)["nodes"]) == 3
        assert app.state.manager.db.get_task(c["task_id"])["task_outcome"] != "success"


def test_unanchored_model_claim_never_creates_tasks(settings, counterexample_repo):
    app = create_app(settings, FakeRunner())
    install_goals(app.state.manager, fake_quote=True)
    with TestClient(app) as client:
        wid = api_register(client, root_plan(counterexample_repo))
        view = api_wait(
            client,
            wid,
            lambda v: next(n for n in v["nodes"] if n["name"] == "C")["state"] == "blocked_configuration",
        )
        assert not view["graph_revisions"] and len(view["nodes"]) == 3
        assert "nonexistent fixed requirement" in next(n for n in view["nodes"] if n["name"] == "C")["reason"]


def test_goal_changes_invalidate_owner_target_and_replanning_bounds(git_repo):
    from app.autopilot.decisions import authorization
    from app.autopilot.policy import manifest
    from app.autopilot.replanning import normalize_policy

    raw = root_plan(git_repo)
    p = manifest(raw)
    assert normalize_policy({})["mode"] == "propose_only"
    for key in ("max_tasks", "max_per_node", "max_depth"):
        with pytest.raises(ValueError):
            normalize_policy({key: 4})
    w = dict(plan=p)
    original = authorization(w)
    w["plan"]["goals"][0]["text"] = "new goal interpretation"
    assert authorization(w) != original
    raw["nodes"][0]["goal_ids"] = ["inferred-not-owner-approved"]
    with pytest.raises(ValueError):
        manifest(raw)


def test_stop_late_replanner_and_quota_reset_never_dispatch_new_tasks(make_manager, counterexample_repo):
    async def scenario():
        from goal_helpers import GoalReviewer

        class LateReplanner(GoalReviewer):
            async def start_replan(self, *args):
                result = await super().start_replan(*args)
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    return result  # Adversarial late result after human Stop.

        m = make_manager(max_concurrent=3)
        ap = install_goals(m)
        started = asyncio.Event()
        ap.providers["codex"] = LateReplanner("codex")
        wid = await register(ap, root_plan(counterexample_repo))
        await drive(m, started.is_set)
        await ap.control(wid, "stop")
        for _ in range(5):
            m.tick()
            await asyncio.sleep(0.02)
        v = ap.ledger.view(wid)
        assert v["state"] == "stopped" and len(v["nodes"]) == 3 and not v["graph_revisions"]
        assert ap.ledger.rows("replans")[0]["state"] in ("stale", "cancelled")
        assert not ap.ledger.rows("replacements")
        assert m._slots._value == 3
        with pytest.raises(ValueError):
            await ap.control(wid, "resume")
        await m.shutdown()

    asyncio.run(scenario())


def test_crashed_replanner_identity_is_reconciled_without_blind_launch(make_manager, counterexample_repo):
    async def scenario():
        from app.autopilot.controller import Autopilot
        from goal_helpers import GoalReviewer

        class Interrupted(GoalReviewer):
            async def start_replan(self, *args):
                await super().start_replan(*args)
                started.set()
                await asyncio.Event().wait()

        m = make_manager()
        ap = install_goals(m)
        started = asyncio.Event()
        ap.providers["codex"] = Interrupted("codex")
        wid = await register(ap, root_plan(counterexample_repo))
        await drive(m, started.is_set)
        await ap.shutdown()
        successor = Autopilot(m, providers=dict(claude=GoalReviewer("claude"), codex=GoalReviewer("codex")))
        m.autopilot = successor
        for _ in range(3):
            m.tick()
            await asyncio.sleep(0.02)
        record = successor.ledger.rows("replans")[0]
        assert record["state"] == "uncertain"
        assert record["payload"]["reconciliation"] in ("process_alive", "dead_or_unknown")
        assert successor.ledger.get("nodes", wid + ":C")["state"] == "reconciling"
        assert len(successor.ledger.rows("replans")) == 1 and not successor.providers["codex"].calls
        assert not successor.ledger.rows("graph_revisions")
        await m.shutdown()

    asyncio.run(scenario())


def test_partition_goal_violation_survives_overall_approve(make_manager, git_repo, tmp_path):
    async def scenario():
        from app.autopilot.review_context import review_all
        from goal_helpers import GoalReviewer

        m = make_manager()
        ap = install_goals(m)
        wid = await register(ap, plan(git_repo, ("P",)))
        await drive(m, lambda: ap.ledger.get("workflows", wid)["state"] == "completed")
        candidate = ap.ledger.view(wid)["nodes"][0]["candidates"][0]
        root = await ap._snapshot(candidate)

        class PartitionViolation(GoalReviewer):
            async def start_review(self, cand, *args):
                r = await super().start_review(cand, *args)
                if cand["payload"]["review_context"].get("phase") != "overall":
                    r["verdict"] = "INCONCLUSIVE"
                    r["goal_assessment"]["goal_alignment"]["status"] = "VIOLATION"
                return r

        # Force multiple partitions without manufacturing a different scope.
        import app.autopilot.review_context as context

        original = context.partitions

        async def doubled(c, r):
            pages, related = await original(c, r)
            return pages + copy.deepcopy(pages), related

        context.partitions = doubled
        try:
            result = await review_all(
                PartitionViolation("claude"),
                candidate,
                {},
                root,
                dict(id="goal-partitions", event_log=str(tmp_path / "partition.jsonl")),
                lambda _: None,
            )
        finally:
            context.partitions = original
        assert result["verdict"] == "INCONCLUSIVE"
        assert result["goal_assessment"]["goal_alignment"]["status"] == "VIOLATION"
        assert len(result["partition_evidence"]) == 2
        await m.shutdown()

    asyncio.run(scenario())


def test_codex_replanner_adapter_preserves_schema_version_and_cli_metadata(tmp_path, monkeypatch):
    from pathlib import Path
    from app.autopilot.providers import CodexProvider
    from app.autopilot.replanning import proposal_schema

    auth_home = tmp_path / "private-auth"
    (auth_home / ".codex").mkdir(parents=True)
    (auth_home / ".codex/auth.json").write_text("{}")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: auth_home))
    scope = dict(base_sha="a" * 40, head_sha="b" * 40, tree_sha="c" * 40, diff_digest="d" * 64)
    packet = dict(
        digest="e" * 64, candidate=dict(id="pinned", scope=scope), authority=dict(policy=dict(max_tasks=3))
    )
    result = dict(
        version=1,
        packet_digest=packet["digest"],
        source_candidate="pinned",
        scope=scope,
        diagnosis=dict(
            version=1,
            classification="INCONCLUSIVE",
            finding_ids=[],
            reproduction="Inspect pinned source",
            observed="Missing evidence",
            expected="Anchored counterexample",
            rationale="Do not change a plan without evidence",
            anchors=[],
        ),
        tasks=[],
        replacement_task="",
        requested_changes=[],
        requested_reads=[],
    )
    methods = []

    class Client:
        pid = None

        def __init__(self, argv, env):
            assert "--ro-bind" in argv and env["CODEX_HOME"] == "/scratch/.codex"

        async def start(self):
            pass

        async def request(self, method, params, **kwargs):
            methods.append(method)
            if method == "account/read":
                return dict(account=dict(type="chatgpt"))
            if method in ("account/rateLimits/read", "model/list"):
                return {}
            if method == "thread/start":
                assert params["sandbox"] == "read-only" and params["approvalPolicy"] == "never"
                assert params["config"]["features"]["shell_tool"] is False
                return dict(thread=dict(id="independent-analysis"), model="fixture-model")
            if method == "turn/start":
                assert params["outputSchema"] == proposal_schema()
                assert json.loads(params["input"][0]["text"]) == packet
                return dict(turn=dict(id="turn-analysis"))
            return {}

        def subscribe(self, tid):
            queue = asyncio.Queue()
            queue.put_nowait(
                (
                    "item/completed",
                    dict(turnId="turn-analysis", item=dict(type="agentMessage", text=json.dumps(result))),
                )
            )
            queue.put_nowait(("turn/completed", dict(turn=dict(id="turn-analysis", status="completed"))))
            return queue

        async def close(self):
            pass

    async def scenario():
        provider = CodexProvider(client_factory=Client)

        async def probe(_):
            return dict(binary="/usr/bin/true", version="CLI-fixture-version")

        provider.probe_capabilities_and_auth = probe
        source = tmp_path / "source"
        source.mkdir()
        value = await provider.start_replan(
            packet,
            dict(codex_model="", review_timeout=3),
            source,
            dict(id="analysis-fixture"),
            lambda _: None,
        )
        assert value["version"] == 1 and value["provider_version"] == "CLI-fixture-version"
        assert value["session"] == "independent-analysis"
        assert "thread/resume" not in methods and "thread/fork" not in methods
        assert methods.count("thread/start") == 1 and "turn/interrupt" in methods

    asyncio.run(scenario())


def test_initial_review_can_trigger_anchored_independent_replanning(make_manager, counterexample_repo):
    async def scenario():
        from goal_helpers import GoalReviewer

        class InitialPremise(GoalReviewer):
            async def start_review(self, candidate, *args):
                result = await super().start_review(candidate, *args)
                if candidate["payload"]["requirements"]["node"] == "C" and result["blocking"]:
                    result["diagnosis"].update(
                        classification="TASK_ASSUMPTION_FALSE",
                        reproduction="Inspect tracked input.csv and explicit task assumption",
                        observed="Accepted input is CSV",
                        expected="Input is JSON according to A-json",
                        rationale="The original premise contradicts the pinned accepted input",
                        anchors=[
                            dict(kind="source", ref="input.csv", quote="good,data"),
                            dict(kind="requirement", ref="assumption:A-json", quote="Input is JSON"),
                        ],
                    )
                return result

        m = make_manager()
        ap = install_goals(m, initial_premise=True)
        ap.providers["claude"] = InitialPremise("claude")
        wid = await register(ap, root_plan(counterexample_repo, "propose_only"))
        await drive(m, lambda: bool(ap.ledger.rows("graph_revisions")))
        source = ap.ledger.get("nodes", wid + ":C")
        assert source["state"] == "awaiting_replan_decision" and source["fix_rounds"] == 0
        assert len(ap.ledger.rows("replans")) == 1 and len(ap.ledger.rows("nodes")) == 3
        assert m.db.get_task(source["task_id"])["task_outcome"] != "success"
        await m.shutdown()

    asyncio.run(scenario())


def test_root_counterexample_can_reference_later_fixed_source_lines(git_repo):
    async def scenario():
        from app.autopilot.replanning import verify_evidence

        (git_repo / "counter.py").write_text("# padding\n" * 300 + "ACTUAL_FORMAT = 'CSV'\n")
        await git(git_repo, "add", "counter.py")
        await git(git_repo, "commit", "-qm", "Pinned later-line counterexample")
        packet = dict(
            source_node=dict(
                spec=dict(prompt="Parse the input", assumptions=[dict(id="A-json", text="Input is JSON")])
            ),
            requirements=dict(documents=[], goal_index=dict(declared=[])),
            validation=[],
            findings=[dict(id="premise")],
        )
        diagnosis = dict(
            version=1,
            classification="TASK_ASSUMPTION_FALSE",
            finding_ids=["premise"],
            reproduction="Inspect fixed source line 301",
            observed="CSV source format",
            expected="JSON in the task assumption",
            rationale="A concrete counterexample contradicts the explicit premise",
            anchors=[
                dict(kind="source", ref="counter.py#L301-L301", quote="ACTUAL_FORMAT = 'CSV'"),
                dict(kind="requirement", ref="assumption:A-json", quote="Input is JSON"),
            ],
        )
        assert await verify_evidence(diagnosis, packet, git_repo)
        diagnosis["anchors"][0]["ref"] = "counter.py"
        with pytest.raises(ValueError):
            await verify_evidence(diagnosis, packet, git_repo)

    asyncio.run(scenario())


def test_completed_ordinary_diagnosis_replays_repair_without_another_model_call(make_manager, git_repo):
    async def scenario():
        from app.autopilot.controller import Autopilot
        from goal_helpers import GoalReviewer

        m = make_manager()
        ap = install_goals(m, always_changes=True, changes_only=["P"])
        original = ap._request_fix
        completed = asyncio.Event()

        def crash_boundary(node, fingerprint, findings, _skip_replan=False):
            if _skip_replan:
                completed.set()  # Persisted diagnosis, before deterministic repair transition.
            else:
                original(node, fingerprint, findings)

        ap._request_fix = crash_boundary
        wid = await register(ap, plan(git_repo, ("P",)))
        await drive(m, completed.is_set)
        assert ap.ledger.rows("replans")[0]["state"] == "completed"
        await ap.shutdown()
        replacement = Autopilot(m, providers=dict(claude=GoalReviewer("claude"), codex=GoalReviewer("codex")))
        m.autopilot = replacement
        m.tick()
        assert replacement.ledger.get("nodes", wid + ":P")["state"] in (
            "fix_queued",
            "reserving_fix",
            "blocked_no_progress",
            "blocked_budget",
        )
        assert len(replacement.ledger.rows("replans")) == 1 and not replacement.providers["codex"].calls
        assert not replacement.ledger.rows("graph_revisions")
        await m.shutdown()

    asyncio.run(scenario())


def test_accepted_prerequisite_is_readable_to_replanner_and_managed_sandbox(
    make_manager, counterexample_repo
):
    async def scenario():
        m = make_manager()
        ap = install_goals(m, accepted_prerequisite=True)
        raw = root_plan(counterexample_repo)
        raw["nodes"].insert(0, plan(counterexample_repo, ("P",))["nodes"][0])
        c = next(n for n in raw["nodes"] if n["id"] == "C")
        c["depends_on"] = ["P"]
        c["validation"]["commands"].insert(
            0,
            [
                "python3",
                "-c",
                "from pathlib import Path; assert Path('.autopilot-inputs/handoff/P/P.txt').read_text() == 'good\\n'",
            ],
        )
        d = next(n for n in raw["nodes"] if n["id"] == "D")
        d["validation"]["commands"].append(
            [
                "python3",
                "-c",
                "from pathlib import Path; refs=list(Path('.autopilot-inputs/handoff').rglob('C.txt')); assert len(refs)==1 and refs[0].read_text()=='good\\n'",
            ]
        )
        wid = await register(ap, raw)
        await drive(m, lambda: ap.ledger.get("workflows", wid)["state"] == "completed")
        nodes = {n["name"]: n for n in ap.ledger.view(wid)["nodes"]}
        assert nodes["R"]["inputs"][0]["candidate_id"] == nodes["P"]["candidate_id"]
        for name in ("R", "D"):
            evidence = nodes[name]["candidates"][-1]["validation"][-1]["payload"]
            assert evidence["state"] == "PASS" and all(
                check["exit_status"] == 0 for check in evidence["checks"]
            )
        analysis = nodes["C"]["replans"][0]["payload"]["packet"]
        assert (
            analysis["accepted_inputs"][0]["head_sha"]
            == nodes["P"]["candidates"][-1]["payload"]["scope"]["head_sha"]
        )
        await m.shutdown()

    asyncio.run(scenario())
