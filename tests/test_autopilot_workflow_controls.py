import asyncio
import copy

import pytest

from app.autopilot.ledger import Ledger
from app.autopilot.policy import canonical
from app.autopilot.review_summary import progress, summarize
from app.autopilot.workflow_controls import add_instruction, add_node, control_target
from autopilot_helpers import install, plan


async def draft(make_manager, repo):
    manager = make_manager()
    ap = install(manager)
    raw = plan(repo)
    preview = await ap.dry_run(raw)
    workflow = await ap.register(raw, preview["input_digest"])
    return manager, ap, workflow["id"]


def request(ap, identity, **fields):
    return dict(owner_action=True, expected_target=control_target(ap.ledger.get("workflows", identity)),
                request_key="request-0001", **fields)


def test_append_child_is_atomic_replayable_and_keeps_existing_evidence(make_manager, git_repo):
    async def run():
        manager, ap, identity = await draft(make_manager, git_repo)
        before = ap.ledger.view(identity)
        node = dict(id="D", prompt="fixture D", paths=["D.txt"], depends_on=["C"],
                    validation=dict(commands=[["python3", "-c", "print('checked')"]]))
        body = request(ap, identity, node=node)
        result = add_node(ap, identity, body)
        assert result["state"] == "draft"
        assert [n["name"] for n in result["nodes"]] == ["C", "D"]
        assert result["nodes"][0]["spec"] == before["nodes"][0]["spec"]
        assert result["plan"]["base_sha"] == before["plan"]["base_sha"]
        assert result["control_target"] != before["control_target"]
        assert result["nodes"][1]["execution_settings"]["reasoning_effort"] == "high"
        assert len(result["events"]) == 1
        assert ap.ledger.rows("edges", "node_id=?", (identity + ":D",)) == [
            dict(node_id=identity + ":D", parent_id=identity + ":C")]
        assert add_node(ap, identity, body)["events"] == result["events"]
        with pytest.raises(ValueError, match="different content"):
            add_node(ap, identity, dict(body, node=dict(node, prompt="changed")))
        assert len(Ledger(manager.db).view(identity)["events"]) == 1
        await manager.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize("change", [
    dict(id="C"), dict(depends_on=["missing"]), dict(depends_on=["D"]),
    dict(paths=["C.txt"], depends_on=[]), dict(paths=["../escape"]), dict(validation={}),
    dict(task_id="existing-task"), dict(replan=dict(parent="C", depth=1, revision="fake")),
])
def test_invalid_addition_leaves_plan_and_history_unchanged(make_manager, git_repo, change):
    async def run():
        manager, ap, identity = await draft(make_manager, git_repo)
        before = ap.ledger.view(identity)
        node = dict(id="D", prompt="Add D", paths=["D.txt"], depends_on=["C"],
                    validation=dict(commands=[["python3", "-c", "print(1)"]]))
        node.update(change)
        with pytest.raises(ValueError):
            add_node(ap, identity, request(ap, identity, node=node))
        after = ap.ledger.view(identity)
        assert after["plan"] == before["plan"] and after["nodes"] == before["nodes"]
        assert after["events"] == []
        await manager.shutdown()
    asyncio.run(run())


def test_stale_and_stopped_additions_rejected_and_completed_addition_pauses(make_manager, git_repo):
    async def run():
        manager, ap, identity = await draft(make_manager, git_repo)
        node = dict(id="D", prompt="Add D", paths=["D.txt"], depends_on=["C"],
                    validation=dict(commands=[["python3", "-c", "print(1)"]]))
        body = request(ap, identity, node=node)
        with pytest.raises(ValueError, match="plan changed"):
            add_node(ap, identity, dict(body, expected_target="old"))
        with ap.ledger.transaction() as c:
            c.execute("UPDATE ap_workflows SET state='stopped' WHERE id=?", (identity,))
        with pytest.raises(ValueError, match="stopped"):
            add_node(ap, identity, body)
        with ap.ledger.transaction() as c:
            c.execute("UPDATE ap_workflows SET state='completed' WHERE id=?", (identity,))
            c.execute("UPDATE ap_nodes SET state='accepted' WHERE workflow_id=?", (identity,))
        assert add_node(ap, identity, body)["state"] == "paused"
        assert manager.db.list_tasks() == []
        await manager.shutdown()
    asyncio.run(run())


def test_workflow_notes_are_durable_and_never_dispatch(make_manager, git_repo):
    async def run():
        manager, ap, identity = await draft(make_manager, git_repo)
        body = request(ap, identity, prompt="全体の確認メモ")
        result = await add_instruction(ap, identity, body)
        assert result["events"][0]["kind"] == "note"
        assert result["events"][0]["state"] == "saved"
        assert result["events"][0]["payload"]["prompt"] == "全体の確認メモ"
        assert len((await add_instruction(ap, identity, body))["events"]) == 1
        assert manager.db.list_tasks() == []
        assert Ledger(manager.db).view(identity)["events"] == result["events"]
        await manager.shutdown()
    asyncio.run(run())


def test_child_instruction_reserves_same_thread_and_history_in_one_transaction(make_manager, git_repo):
    async def run():
        manager, ap, identity = await draft(make_manager, git_repo)
        task = await manager.create_task(repository=str(git_repo), prompt="Prepare C", name="C", _defer_dispatch=True)
        with ap.ledger.transaction() as c:
            c.execute("UPDATE tasks SET status='completed',task_outcome='needs_review',completion_pending=0,codex_thread_id='existing-thread' WHERE id=?", (task["id"],))
            c.execute("UPDATE ap_nodes SET task_id=?,state='blocked_result' WHERE id=?", (task["id"], identity + ":C"))
            c.execute("UPDATE ap_workflows SET state='paused' WHERE id=?", (identity,))
        body = request(ap, identity, prompt="追加の検証をしてください", node_id="C")
        result = await add_instruction(ap, identity, body)
        queued = manager.db.get_task(task["id"])
        assert queued["status"] == "queued" and queued["codex_thread_id"] == "existing-thread"
        assert "existing-thread" in queued["pending_turn"]
        assert result["state"] == "paused"
        assert result["nodes"][0]["generation"] == 1
        assert result["events"][0]["payload"]["thread"] == "existing-thread"
        messages = manager.db.list_messages(task["id"])
        await add_instruction(ap, identity, body)
        assert manager.db.list_messages(task["id"]) == messages
        assert len(ap.ledger.rows("operations", "node_id=?", (identity + ":C",))) == 1
        await manager.shutdown()
    asyncio.run(run())


def test_instruction_cannot_clear_manual_gate_or_target_another_workflow(make_manager, git_repo):
    async def run():
        from app.task_manager import TaskError

        manager, ap, identity = await draft(make_manager, git_repo)
        task = await manager.create_task(repository=str(git_repo), prompt="Prepare C", name="C", _defer_dispatch=True)
        node = ap.ledger.get("nodes", identity + ":C")
        node["spec"]["manual_gates"] = ["Owner design choice"]
        with ap.ledger.transaction() as c:
            c.execute("UPDATE tasks SET status='completed',completion_pending=0,codex_thread_id='thread' WHERE id=?", (task["id"],))
            c.execute("UPDATE ap_nodes SET task_id=?,state='blocked_decision',spec=? WHERE id=?",
                      (task["id"], canonical(node["spec"]), node["id"]))
            c.execute("UPDATE ap_workflows SET state='paused' WHERE id=?", (identity,))
        with pytest.raises(TaskError, match="approval"):
            await add_instruction(ap, identity, request(ap, identity, prompt="continue", node_id="C"))
        with pytest.raises(ValueError, match="in this workflow"):
            await add_instruction(ap, identity, request(ap, identity, prompt="continue", node_id="other:C"))
        assert ap.ledger.rows("workflow_events") == []
        assert manager.db.get_task(task["id"])["status"] == "completed"
        await manager.shutdown()
    asyncio.run(run())


def test_review_summary_deduplicates_findings_and_ignores_accepted_history():
    finding = dict(id="bug", path="src/a.py", line=3, reason="入力境界の検査が不足")
    node = dict(id="w:C", name="C", state="blocked_budget", reason="round limit", task_id="t",
                candidate_id="c", spec=dict(manual_gates=[]), decision_target="target",
                candidates=[dict(id="c", reviews=[dict(id="r", state="completed", payload=dict(
                    blocking=[finding, dict(finding, id="duplicate")], unverified=[]))])])
    view = dict(state="running", nodes=[node], plan_revisions=[], graph_revisions=[])
    summary = summarize(view)
    assert summary["count"] == 1
    assert summary["text"].count("入力境界の検査が不足") == 1
    assert "src/a.py:3" in summary["text"] and "修正回数の上限" in summary["text"]
    assert summary["items"][0]["target"] == "target"
    accepted = copy.deepcopy(node)
    accepted["state"] = "accepted"
    assert summarize(dict(view, nodes=[accepted], state="completed"))["count"] == 0
    assert "不足" not in summarize(dict(view, nodes=[accepted]))["text"]
    assert progress([accepted, dict(node, state="superseded")]) == dict(
        total=1, accepted=1, active=0, blocked=0, waiting=0, superseded=1, percent=100)


@pytest.mark.parametrize("node_state", ["replan_queued", "replanning"])
def test_addition_does_not_invalidate_a_reserved_replan(make_manager, git_repo, node_state):
    async def run():
        manager, ap, identity = await draft(make_manager, git_repo)
        with ap.ledger.transaction() as c:
            c.execute("UPDATE ap_nodes SET state=? WHERE workflow_id=?", (node_state, identity))
        before = ap.ledger.get("workflows", identity)
        node = dict(id="D", prompt="Add D", paths=["D.txt"], depends_on=["C"],
                    validation=dict(commands=[["python3", "-c", "print(1)"]]))
        with pytest.raises(ValueError, match="planning operation"):
            add_node(ap, identity, request(ap, identity, node=node))
        assert ap.ledger.get("workflows", identity) == before
        assert len(ap.ledger.rows("nodes")) == 1 and ap.ledger.rows("workflow_events") == []
        await manager.shutdown()
    asyncio.run(run())


def test_running_child_instruction_rolls_back_before_dispatch_if_event_write_fails(make_manager, git_repo, monkeypatch):
    async def run():
        manager, ap, identity = await draft(make_manager, git_repo)
        task = await manager.create_task(repository=str(git_repo), prompt="Prepare C", name="C", _defer_dispatch=True)
        with ap.ledger.transaction() as c:
            c.execute("UPDATE tasks SET status='completed',completion_pending=0,codex_thread_id='existing-thread' WHERE id=?",
                      (task["id"],))
            c.execute("UPDATE ap_nodes SET task_id=?,state='blocked_result' WHERE id=?", (task["id"], identity + ":C"))
            c.execute("UPDATE ap_workflows SET state='running' WHERE id=?", (identity,))
        before_task = manager.db.get_task(task["id"])
        before_node = ap.ledger.get("nodes", identity + ":C")
        before_messages = manager.db.list_messages(task["id"])
        dispatches = []
        monkeypatch.setattr(manager, "_launch", lambda task_id, turn: dispatches.append(task_id))

        def fail_event_write(*args, **kwargs):
            raise RuntimeError("synthetic event insert failure")

        monkeypatch.setattr("app.autopilot.workflow_controls._event", fail_event_write)
        with pytest.raises(RuntimeError, match="synthetic event insert failure"):
            await add_instruction(ap, identity, request(ap, identity, prompt="Add a check", node_id="C"))
        assert manager.db.get_task(task["id"]) == before_task
        assert ap.ledger.get("nodes", before_node["id"]) == before_node
        assert manager.db.list_messages(task["id"]) == before_messages
        assert ap.ledger.rows("operations", "node_id=?", (before_node["id"],)) == []
        assert ap.ledger.rows("workflow_events") == []
        assert dispatches == []
        await manager.shutdown()
    asyncio.run(run())
