"""Versioned owner decisions. Generic Resume and AI verdicts never create these records."""

import uuid

from app.models import now_iso
from .policy import canonical, digest


def authorization(workflow):
    return digest({key: workflow["plan"].get(key, {}) for key in ("policy", "permissions", "inputs", "rules", "goals")})


def target(node, workflow):
    return digest(
        dict(
            node=node["id"],
            spec=node["spec"],
            candidate=node["candidate_id"],
            generation=node["generation"],
            decision_epoch=node.get("decision_epoch", 0),
            authorization=authorization(workflow),
            run_generation=workflow["generation"],
            reason=node["reason"],
        )
    )


def gate_approved(ledger, node, workflow, connection=None):
    args = (node["id"],)
    if connection is not None:
        from .ledger import unpack

        rows = [unpack(r) for r in connection.execute("SELECT * FROM ap_decisions WHERE node_id=?", args)]
    else:
        rows = ledger.rows("decisions", "node_id=?", args)
    return any(
        r["state"] == "approved"
        and r["payload"].get("purpose") == "manual_gates"
        and r["payload"].get("spec_digest") == digest(node["spec"])
        and r["payload"].get("run_generation") == workflow["generation"]
        and r["payload"].get("authorization") == authorization(workflow)
        for r in rows
    )


def candidate_approved(ledger, node, workflow, connection=None):
    from .ledger import unpack

    rows = (
        [unpack(r) for r in connection.execute("SELECT * FROM ap_decisions WHERE node_id=?", (node["id"],))]
        if connection is not None
        else ledger.rows("decisions", "node_id=?", (node["id"],))
    )
    return any(
        r["state"] == "approved"
        and r["payload"].get("purpose") == "candidate_acceptance"
        and r["payload"].get("candidate_id") == node["candidate_id"]
        and r["payload"].get("spec_digest") == digest(node["spec"])
        and r["payload"].get("run_generation") == workflow["generation"]
        and r["payload"].get("decision_epoch") == node["decision_epoch"]
        and r["payload"].get("authorization") == authorization(workflow)
        for r in rows
    )


def decide(ap, nid, expected, action, note):
    if action not in ("approve", "reject", "resume") or not isinstance(note, str) or not note.strip():
        raise ValueError("explicit owner approve/reject/resume and rationale are required")
    with ap.ledger.transaction() as c:
        from .ledger import unpack

        n = unpack(c.execute("SELECT * FROM ap_nodes WHERE id=?", (nid,)).fetchone())
        if not n:
            raise ValueError("node not found")
        w = unpack(c.execute("SELECT * FROM ap_workflows WHERE id=?", (n["workflow_id"],)).fetchone())
        if w["state"] not in ("running", "paused") or target(n, w) != expected:
            raise ValueError("owner decision target changed or workflow stopped")
        previous = c.execute("SELECT * FROM ap_decisions WHERE node_id=? AND target=?", (nid, expected)).fetchone()
        if action == "resume":
            if not previous or previous["state"] != "approved" or n["state"] != "blocked_decision":
                raise ValueError("Resume requires an explicit approval for this exact target")
            state = (
                "review_complete"
                if json_payload(previous).get("purpose") == "candidate_acceptance"
                else "review_queued"
                if n["candidate_id"]
                else "pending"
            )
            c.execute("UPDATE ap_nodes SET state=?,reason='' WHERE id=? AND state='blocked_decision'", (state, nid))
        else:
            if n["state"] != "blocked_decision":
                raise ValueError("node is not awaiting an owner decision")
            if previous:
                if previous["state"] != ("approved" if action == "approve" else "rejected"):
                    raise ValueError("decision already recorded; create a new target revision")
            else:
                payload = dict(
                    note=note,
                    run_generation=w["generation"],
                    spec_digest=digest(n["spec"]),
                    candidate_id=n["candidate_id"],
                    decision_epoch=n["decision_epoch"],
                    authorization=authorization(w),
                    purpose=(
                        "manual_gates"
                        if not n["candidate_id"]
                        else "candidate_acceptance"
                        if n["reason"].startswith("Candidate owner acceptance required")
                        else "review_decision"
                    ),
                )
                c.execute(
                    "INSERT INTO ap_decisions VALUES(?,?,?,?,?,?)",
                    (
                        uuid.uuid4().hex,
                        nid,
                        expected,
                        "approved" if action == "approve" else "rejected",
                        canonical(payload),
                        now_iso(),
                    ),
                )
    ap.manager.scheduler.wake()
    return ap.ledger.view(n["workflow_id"])


def json_payload(row):
    import json

    return json.loads(row["payload"])
