"""Owner-authored additions with atomic reservations and durable replay protection."""

import copy
import re
import uuid

from app.models import now_iso
from .ledger import unpack
from .policy import canonical, digest, manifest


def control_target(workflow):
    return digest(dict(plan=workflow["plan"], generation=workflow["generation"]))


def _request(body, kind):
    if not isinstance(body, dict) or body.get("owner_action") is not True:
        raise ValueError("explicit owner action is required")
    key = body.get("request_key")
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", key):
        raise ValueError("a unique request_key (8..128 letters, numbers, '-' or '_') is required")
    if not isinstance(body.get("expected_target"), str) or not body["expected_target"]:
        raise ValueError("inspect the current workflow before submitting an addition")
    payload = {k: v for k, v in body.items() if k not in ("owner_action", "expected_target", "request_key")}
    return key, digest(dict(kind=kind, payload=payload)), payload


def _current(connection, identity, body, key, fingerprint):
    workflow = unpack(connection.execute("SELECT * FROM ap_workflows WHERE id=?", (identity,)).fetchone())
    if not workflow:
        raise ValueError("workflow not found")
    previous = unpack(connection.execute(
        "SELECT * FROM ap_workflow_events WHERE workflow_id=? AND request_key=?", (identity, key)
    ).fetchone())
    if previous:
        if previous["payload"]["request_digest"] != fingerprint:
            raise ValueError("request_key was already used for different content")
        return workflow, True
    if control_target(workflow) != body["expected_target"]:
        raise ValueError("workflow plan changed; reload and inspect the current plan")
    if workflow["state"] not in ("draft", "running", "paused", "completed"):
        raise ValueError("stopped or uncertain workflow requires an inspected recovery plan")
    return workflow, False


def _event(connection, identity, kind, state, key, fingerprint, payload):
    connection.execute("INSERT INTO ap_workflow_events VALUES(?,?,?,?,?,?,?)", (
        uuid.uuid4().hex, identity, kind, state,
        canonical(dict(payload, request_digest=fingerprint)), key, now_iso(),
    ))


async def add_instruction(ap, identity, body):
    """Save a workflow note, or atomically continue exactly one existing idle child thread."""
    from app import ctx_guard
    from app.task_manager import _Turn, TaskError

    key, fingerprint, payload = _request(body, "instruction")
    if set(payload) - {"prompt", "node_id"}:
        raise ValueError("unknown instruction fields")
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode()) > 262144:
        raise ValueError("instruction must contain 1..262144 bytes of text")
    node_id = payload.get("node_id", "")
    if not isinstance(node_id, str):
        raise ValueError("node_id must be a child node name")
    committed_task = None
    with ap.ledger.transaction() as connection:
        workflow, replay = _current(connection, identity, body, key, fingerprint)
        if not replay:
            event = dict(prompt=prompt.strip(), run_generation=workflow["generation"])
            if node_id:
                qualified = node_id if node_id.startswith(identity + ":") else identity + ":" + node_id
                node = unpack(connection.execute(
                    "SELECT * FROM ap_nodes WHERE id=? AND workflow_id=?", (qualified, identity)
                ).fetchone())
                if not node or not node["task_id"]:
                    raise ValueError("select a started child task in this workflow")
                task = ap.manager.get(node["task_id"])
                ap.manager._require_idle_with_worktree(task)
                if not task["codex_thread_id"]:
                    raise TaskError("child task has no recoverable session; inspect its task details", 409, "no_session")
                if ctx_guard.blocks_resend(task.get("stop_reason") or ""):
                    raise TaskError(ctx_guard.STOP_MESSAGES["context_window_exceeded"], 409, "context_overflow")
                turn = _Turn(prompt.strip(), resume_thread=task["codex_thread_id"], service_tier="default")
                ap.queue_owner_turn(task, turn, {}, _connection=connection, _defer_effects=True)
                committed_task = task
                event.update(node_id=node["id"], task_id=task["id"], thread=task["codex_thread_id"])
            _event(connection, identity, "instruction" if node_id else "note",
                   "queued" if node_id else "saved", key, fingerprint, event)
    if committed_task:
        ap.finish_owner_turn(committed_task)
    ap.manager.scheduler.wake()
    return ap.ledger.view(identity)


def add_node(ap, identity, body):
    """Append explicit owner work without rewriting any already registered node or evidence."""
    key, fingerprint, payload = _request(body, "add_node")
    if set(payload) != {"node"} or not isinstance(payload["node"], dict):
        raise ValueError("provide one explicit node specification")
    if payload["node"].get("task_id") or payload["node"].get("replan"):
        raise ValueError("new owner tasks cannot import existing tasks or fabricate replan lineage")
    with ap.ledger.transaction() as connection:
        workflow, replay = _current(connection, identity, body, key, fingerprint)
        if not replay:
            # A publication plan has a pinned final target; extending it requires a new reviewed plan.
            if workflow["plan"]["permissions"].get("publish_main"):
                raise ValueError("a main publication plan requires a new reviewed final integration plan")
            for table in ("plan_revisions", "graph_revisions"):
                if connection.execute(
                    f"SELECT 1 FROM ap_{table} WHERE workflow_id=? AND state IN ('ready','awaiting_owner')", (identity,)
                ).fetchone():
                    raise ValueError("finish the pending plan revision before adding a task")
            nodes = [unpack(r) for r in connection.execute(
                "SELECT * FROM ap_nodes WHERE workflow_id=? ORDER BY rowid", (identity,)
            )]
            for node in nodes:
                if node["state"] in ("replan_queued", "replanning") or (
                    node["state"] == "accepted" and node["spec"].get("plan_artifact")
                    and not connection.execute(
                        "SELECT 1 FROM ap_plan_revisions WHERE source_candidate=?", (node["candidate_id"],)
                    ).fetchone()
                ):
                    raise ValueError("wait for the current planning operation before adding a task")
            raw = {k: copy.deepcopy(v) for k, v in workflow["plan"].items()
                   if k not in ("base_sha", "rules")}
            raw["nodes"] = [copy.deepcopy(n["spec"]) for n in nodes] + [copy.deepcopy(payload["node"])]
            validated = manifest(raw)
            name = payload["node"].get("id")
            spec = next(n for n in validated["nodes"] if n["id"] == name)
            for parent in spec["depends_on"]:
                previous = next(n for n in nodes if n["name"] == parent)
                if previous["state"] in ("stopped", "superseded", "awaiting_replacement"):
                    raise ValueError("select a current parent task instead of a stopped or replaced node")
            # Keep every existing spec, frozen input and model setting byte-for-byte intact.
            amended = copy.deepcopy(workflow["plan"])
            amended["nodes"] = [n["spec"] for n in nodes] + [spec]
            nid = identity + ":" + name
            connection.execute(
                "INSERT INTO ap_nodes(id,workflow_id,name,spec,state,ready_at) VALUES(?,?,?,?,?,?)",
                (nid, identity, name, canonical(spec), "draft" if spec["draft"] else "pending", now_iso()),
            )
            for parent in spec["depends_on"]:
                connection.execute("INSERT INTO ap_edges VALUES(?,?)", (nid, identity + ":" + parent))
            # Completed work resumes in paused state so appending a task never starts it unexpectedly.
            state = "paused" if workflow["state"] == "completed" else workflow["state"]
            connection.execute("UPDATE ap_workflows SET plan=?,state=?,reason=? WHERE id=?", (
                canonical(amended), state, "Owner added a task" if state == "paused" else workflow["reason"], identity,
            ))
            _event(connection, identity, "add_node", "registered", key, fingerprint,
                   dict(node_id=nid, node=spec, run_generation=workflow["generation"],
                        previous_plan_digest=digest(workflow["plan"]), plan_digest=digest(amended)))
    ap.manager.scheduler.wake()
    return ap.ledger.view(identity)
