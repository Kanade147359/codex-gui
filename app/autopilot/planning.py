"""Accepted planning artifacts may refine only unconsumed Draft nodes."""

import json
import uuid

from app.models import now_iso
from . import git
from .policy import canonical, digest, owns


async def propose(ap, planner):
    w = ap.ledger.get("workflows", planner["workflow_id"])
    candidate = ap.ledger.get("candidates", planner["candidate_id"])
    if ap.ledger.rows("plan_revisions", "source_candidate=?", (candidate["id"],)):
        return
    path = planner["spec"].get("plan_artifact")
    if not path:
        return
    _, mode, _ = await git.git(
        w["plan"]["repository"],
        "ls-tree",
        candidate["payload"]["scope"]["head_sha"],
        "--",
        path,
    )
    if not mode.startswith(("100644 ", "100755 ")):
        raise ValueError("planning artifact must be a regular accepted file")
    from .inputs import accepted_text, bytes_digest

    text = await accepted_text(w["plan"]["repository"], candidate["payload"]["scope"]["head_sha"], path)
    raw = json.loads(text)
    if (
        not isinstance(raw, dict)
        or set(raw) != {"nodes"}
        or not isinstance(raw["nodes"], list)
        or not raw["nodes"]
    ):
        raise ValueError("planning artifact must contain nonempty nodes revisions")
    plan = dict(w["plan"])
    nodes = {n["id"]: dict(n) for n in plan["nodes"]}
    expansions, changed = [], []
    for change in raw["nodes"]:
        if not isinstance(change, dict) or set(change) - {
            "id",
            "paths",
            "validation",
            "prompt",
            "input_files",
            "handoff",
            "review_files",
            "manual_gates",
            "contract",
        }:
            raise ValueError("plan revision cannot change graph, permissions or execution mode")
        nid = change["id"]
        if nid not in nodes or nid in changed or not nodes[nid]["draft"]:
            raise ValueError("plan revision requires distinct Draft targets")
        old = nodes[nid]
        spec = dict(old, **change, draft=False)
        envelope = old["envelope"]
        if any(not owns(p, envelope["paths"]) for p in spec["paths"]):
            expansions.append(nid + ": edit ownership expansion")
        from .policy import profile

        selected = profile(spec["validation"], spec["kind"])
        if selected not in envelope["validation_profiles"]:
            expansions.append(nid + ": unapproved validation profile")
        if spec["manual_gates"] != old["manual_gates"] or spec["contract"] != old["contract"]:
            expansions.append(nid + ": approval contract change")
        nodes[nid] = spec
        changed.append(nid)
    plan["nodes"] = list(nodes.values())
    from .graph_revision import normalize

    retired = {
        r["source_node_id"].split(":", 1)[1]
        for r in ap.ledger.rows(
            "replacements",
            "source_node_id IN (SELECT id FROM ap_nodes WHERE workflow_id=?)",
            (w["id"],),
        )
    }
    normalized = normalize(plan, retired)
    payload = dict(
        expected_plan=digest(w["plan"]),
        plan=normalized,
        changed=changed,
        before={name: next(spec for spec in w["plan"]["nodes"] if spec["id"] == name) for name in changed},
        expansions=expansions,
        artifact=dict(
            head_sha=candidate["payload"]["scope"]["head_sha"],
            path=path,
            digest=bytes_digest(text.encode()),
        ),
    )
    with ap.ledger.transaction() as c:
        if c.execute(
            "SELECT 1 FROM ap_plan_revisions WHERE source_candidate=?",
            (candidate["id"],),
        ).fetchone():
            return  # Another scheduler/controller already reserved or applied this exact artifact.
        current = c.execute("SELECT candidate_id,state FROM ap_nodes WHERE id=?", (planner["id"],)).fetchone()
        live = c.execute("SELECT plan,state FROM ap_workflows WHERE id=?", (w["id"],)).fetchone()
        if (
            current["candidate_id"] != candidate["id"]
            or current["state"] != "accepted"
            or live["state"] != "running"
            or digest(json.loads(live["plan"])) != payload["expected_plan"]
        ):
            raise ValueError(
                "planning source changed before revision reservation: "
                + canonical(
                    dict(
                        candidate_matches=current["candidate_id"] == candidate["id"],
                        source_state=current["state"],
                        workflow_state=live["state"],
                        plan_matches=digest(json.loads(live["plan"])) == payload["expected_plan"],
                    )
                )
            )
        c.execute(
            "INSERT OR IGNORE INTO ap_plan_revisions VALUES(?,?,?,?,?,?)",
            (
                uuid.uuid4().hex,
                w["id"],
                candidate["id"],
                "awaiting_owner" if expansions else "ready",
                canonical(payload),
                now_iso(),
            ),
        )


def apply(ap, revision, *, expected=None, approve=False, reject=False):
    from .ledger import unpack

    with ap.ledger.transaction() as c:
        r = unpack(c.execute("SELECT * FROM ap_plan_revisions WHERE id=?", (revision["id"],)).fetchone())
        w = unpack(c.execute("SELECT * FROM ap_workflows WHERE id=?", (r["workflow_id"],)).fetchone())
        if r["state"] == "applied":
            if approve or reject:
                if reject or expected != r["payload"].get("owner_decision_target"):
                    raise ValueError("plan decision target changed or was already applied")
            return
        if w["state"] not in ("running", "paused") or digest(w["plan"]) != r["payload"]["expected_plan"]:
            raise ValueError("plan revision is stale or workflow stopped")
        source = c.execute(
            "SELECT n.state,n.candidate_id,ac.run_generation FROM ap_nodes n JOIN ap_candidates ac ON ac.id=? WHERE n.id=ac.node_id",
            (r["source_candidate"],),
        ).fetchone()
        if (
            not source
            or source["state"] != "accepted"
            or source["candidate_id"] != r["source_candidate"]
            or source["run_generation"] != w["generation"]
        ):
            raise ValueError("planning source acceptance is no longer current")
        if approve or reject:
            if expected != digest(r["payload"]):
                raise ValueError("owner must inspect this exact plan revision")
            if reject:
                c.execute("UPDATE ap_plan_revisions SET state='rejected' WHERE id=?", (r["id"],))
                return
        if r["state"] != "ready" and not (r["state"] == "awaiting_owner" and approve):
            raise ValueError("plan expansion requires an explicit owner decision")
        for name in r["payload"]["changed"]:
            n = unpack(c.execute("SELECT * FROM ap_nodes WHERE workflow_id=? AND name=?", (w["id"], name)).fetchone())
            if not n or n["task_id"] or n["candidate_id"] or n["generation"] or n["state"] != "draft":
                raise ValueError("executed or consumed inputs cannot be rewritten")
        for spec in r["payload"]["plan"]["nodes"]:
            if spec["id"] in r["payload"]["changed"]:
                c.execute(
                    "UPDATE ap_nodes SET spec=?,state='pending',reason='' WHERE workflow_id=? AND name=? AND state='draft'",
                    (canonical(spec), w["id"], spec["id"]),
                )
        c.execute("UPDATE ap_workflows SET plan=? WHERE id=?", (canonical(r["payload"]["plan"]), w["id"]))
        audit = dict(r["payload"], owner_approved=approve, owner_decision_target=expected if approve else None)
        c.execute("UPDATE ap_plan_revisions SET state='applied',payload=? WHERE id=?", (canonical(audit), r["id"]))
