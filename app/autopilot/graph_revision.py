"""Atomic dynamic DAG revisions; historical source tasks never become successful by replanning."""

import uuid

from app.models import now_iso
from .policy import canonical, digest, manifest, owns, profile
from .ledger import unpack


def normalize(plan, retired=()):
    active = [n for n in plan["nodes"] if n["id"] not in retired]
    raw = {k: v for k, v in plan.items() if k not in ("base_sha", "rules")}
    raw["nodes"] = active
    valid = manifest(raw)
    valid["nodes"] = [n for n in plan["nodes"] if n["id"] in retired] + valid["nodes"]
    valid.update(base_sha=plan["base_sha"], rules=plan.get("rules", {}))
    if len(valid["nodes"]) > 200:
        raise ValueError("workflow node limit exceeded, including historical nodes")
    return valid


def propose(ap, replan_id):
    record = ap.ledger.get("replans", replan_id)
    data = record["payload"]
    p = data["proposal"]
    packet = data["packet"]
    source = ap.ledger.get("nodes", record["node_id"])
    w = ap.ledger.get("workflows", source["workflow_id"])
    if ap.ledger.rows("graph_revisions", "replan_id=?", (replan_id,)):
        return
    if not data.get("evidence_supported"):
        raise ValueError("unverified premise cannot create a graph revision")
    original = source["spec"]
    existing = {n["id"]: n for n in w["plan"]["nodes"]}
    identities = [t["task_id"] for t in p["tasks"]]
    if not identities or len(set(identities)) != len(identities) or any(i in existing for i in identities):
        raise ValueError("dynamic tasks need distinct unused node IDs")
    changes = []
    hard = []
    new = []
    authority_paths = original["envelope"]["paths"] or original["paths"]
    profiles = [original["validation"], *original["envelope"]["validation_profiles"]]
    allowed_goals = {g["id"] for g in w["plan"].get("goals", [])}
    assumptions = {a["id"] for a in original.get("assumptions", [])}
    for t in p["tasks"]:
        terminal = t["task_id"] == p["replacement_task"]
        v = profile(t["validation"], "code")
        if any(not owns(path, authority_paths) for path in t["paths"]):
            changes.append(t["task_id"] + ": unapproved edit paths")
        if v not in profiles:
            changes.append(t["task_id"] + ": unregistered validation profile")
        if t["manual_gates"] != original["manual_gates"]:
            hard.append(t["task_id"] + ": manual gate change")
        if set(t["goal_ids"]) - allowed_goals or not set(original.get("goal_ids", [])) <= set(t["goal_ids"]):
            hard.append(t["task_id"] + ": global goal association change")
        if not set(t["invalidated_assumptions"]) <= assumptions:
            hard.append(t["task_id"] + ": unregistered/inferred assumption correction")
        if p["diagnosis"]["classification"] == "TASK_ASSUMPTION_FALSE":
            anchored = {
                a["ref"].removeprefix("assumption:")
                for a in p["diagnosis"]["anchors"]
                if a["kind"] == "requirement" and a["ref"].startswith("assumption:")
            }
            if not t["invalidated_assumptions"] or not set(t["invalidated_assumptions"]) <= anchored:
                changes.append(t["task_id"] + ": free-form premise correction needs an owner interpretation")
        if terminal:
            if t["completion_contract"] != original["contract"]:
                hard.append(t["task_id"] + ": completion contract change")
            if any(c not in t["acceptance_conditions"] for c in original.get("acceptance_conditions", [])):
                hard.append(t["task_id"] + ": acceptance condition removal")
            if any(cmd not in v["commands"] for cmd in original["validation"]["commands"]) or not set(
                original["validation"]["artifacts"]
            ) <= set(v["artifacts"]):
                hard.append(t["task_id"] + ": mandatory validation weakening")
            if not original.get("acceptance_conditions"):
                changes.append(t["task_id"] + ": free-form original acceptance needs owner interpretation")
        elif t["completion_contract"].get("manual_approval", False) != original["contract"].get(
            "manual_approval", False
        ):
            hard.append(t["task_id"] + ": manual approval removal")
        if set(t["depends_on"]) - set(identities) - set(original["depends_on"]):
            hard.append(t["task_id"] + ": dependency outside approved inputs")
        new.append(
            dict(
                id=t["task_id"],
                kind="code",
                prompt=t["purpose"] + "\n" + t["implementation"],
                paths=t["paths"],
                depends_on=t["depends_on"],
                validation=v,
                contract=t["completion_contract"],
                manual_gates=t["manual_gates"],
                input_files=original["input_files"],
                goal_ids=t["goal_ids"],
                acceptance_conditions=t["acceptance_conditions"],
                assumptions=original.get("assumptions", []),
                envelope=original["envelope"],
            )
        )
    if p["requested_changes"]:
        hard += [
            "Requested authority/design change: " + c["kind"] + ": " + c["description"]
            for c in p["requested_changes"]
        ]
    by_id = {n["id"]: n for n in new}
    ancestors = set()

    def walk(nid, seen):
        if nid in seen:
            raise ValueError("dynamic dependency cycle")
        if nid not in by_id:
            return
        ancestors.add(nid)
        for parent in by_id[nid]["depends_on"]:
            walk(parent, seen | {nid})

    walk(p["replacement_task"], set())
    if set(identities) - ancestors:
        hard.append("Replacement terminal must include every new task as an ancestor")
    rewired = []
    nodes = []
    for spec in w["plan"]["nodes"]:
        changed = dict(spec)
        if source["name"] in changed["depends_on"]:
            child = ap.ledger.get("nodes", w["id"] + ":" + changed["id"])
            if (
                child["task_id"]
                or child["candidate_id"]
                or child["generation"]
                or child["inputs"]
                or child["state"] not in ("pending", "draft", "blocked_dependencies")
            ):
                hard.append("Consumed successor cannot be rewired: " + changed["id"])
            changed["depends_on"] = [
                p["replacement_task"] if parent == source["name"] else parent
                for parent in changed["depends_on"]
            ]
            rewired.append(changed["id"])
        nodes.append(changed)
    retired = {
        r["source_node_id"].split(":", 1)[1]
        for r in ap.ledger.rows(
            "replacements",
            "source_node_id IN (SELECT id FROM ap_nodes WHERE workflow_id=?)",
            (w["id"],),
        )
    } | {source["name"]}
    try:
        after = normalize(dict(w["plan"], nodes=nodes + new), retired)
    except (ValueError, TypeError) as error:
        # Invalid graphs remain proposals; an owner click cannot bypass DAG/ownership checks.
        hard.append(str(error))
        after = dict(w["plan"], nodes=nodes + new)
    mode = w["plan"]["policy"]["replanning"]["mode"]
    payload = dict(
        expected_plan=packet["expected_plan"],
        run_generation=record["run_generation"],
        source_candidate=record["source_candidate"],
        source_node=source["id"],
        source_generation=source["generation"],
        requirements_digest=packet["candidate"]["requirements_digest"],
        packet_digest=packet["digest"],
        before=w["plan"],
        after=after,
        new_tasks=new,
        proposal=p,
        rewired=rewired,
        expansions=changes,
        hard_blocks=hard,
        replacement_task=p["replacement_task"],
        retired=sorted(retired),
        remaining=dict(
            max_tasks=w["plan"]["policy"]["replanning"]["max_tasks"] - len(new), max_per_node=0, max_depth=0
        ),
    )
    state = "ready" if mode == "auto_within_envelope" and not changes and not hard else "awaiting_owner"
    with ap.ledger.transaction() as db:
        live = unpack(db.execute("SELECT * FROM ap_workflows WHERE id=?", (w["id"],)).fetchone())
        node = db.execute(
            "SELECT candidate_id,generation,state FROM ap_nodes WHERE id=?",
            (source["id"],),
        ).fetchone()
        if (
            live["state"] not in ("running", "paused")
            or live["generation"] != record["run_generation"]
            or digest(live["plan"]) != packet["expected_plan"]
            or node["candidate_id"] != record["source_candidate"]
            or node["generation"] != source["generation"]
            or node["state"] != "analyzing"
        ):
            raise ValueError("dynamic graph proposal source is stale")
        db.execute(
            "INSERT OR IGNORE INTO ap_graph_revisions VALUES(?,?,?,?,?,?)",
            (
                uuid.uuid4().hex,
                w["id"],
                replan_id,
                state,
                canonical(payload),
                now_iso(),
            ),
        )
        db.execute(
            "UPDATE ap_nodes SET state='awaiting_replan_decision',reason='Independent Codex proposed new tasks; original task not successful' WHERE id=?",
            (source["id"],),
        )


def apply(ap, identity, *, expected=None, action=None, note=""):
    with ap.ledger.transaction() as db:
        r = unpack(db.execute("SELECT * FROM ap_graph_revisions WHERE id=?", (identity,)).fetchone())
        if not r:
            raise ValueError("dynamic graph revision not found")
        p = r["payload"]
        if action:
            if action not in ("approve", "reject") or not note.strip() or expected != digest(p):
                raise ValueError("explicit owner decision must match this complete before/after revision")
        if r["state"] == "applied":
            if action:
                raise ValueError("dynamic graph decision is already consumed")
            return
        w = unpack(db.execute("SELECT * FROM ap_workflows WHERE id=?", (r["workflow_id"],)).fetchone())
        n = unpack(db.execute("SELECT * FROM ap_nodes WHERE id=?", (p["source_node"],)).fetchone())
        if (
            w["state"] not in ("running", "paused")
            or w["generation"] != p["run_generation"]
            or digest(w["plan"]) != p["expected_plan"]
            or n["candidate_id"] != p["source_candidate"]
            or n["generation"] != p["source_generation"]
            or n["state"] != "awaiting_replan_decision"
        ):
            raise ValueError("dynamic graph/source/run changed; stale revision refused")
        candidate = unpack(
            db.execute("SELECT * FROM ap_candidates WHERE id=?", (p["source_candidate"],)).fetchone()
        )
        if candidate["payload"]["requirements_digest"] != p["requirements_digest"]:
            raise ValueError("replan requirements changed")
        task = db.execute("SELECT * FROM tasks WHERE id=?", (n["task_id"],)).fetchone()
        if (
            not task
            or task["status"] != "completed"
            or task["completion_pending"]
            or digest([task["last_turn_at"], task["semantic_result"]])
            != candidate["payload"]["execution_key"]
        ):
            raise ValueError("source execution changed after independent analysis")
        if action == "reject":
            db.execute(
                "UPDATE ap_graph_revisions SET state='rejected',payload=? WHERE id=?",
                (
                    canonical(
                        dict(
                            p,
                            owner_decision=dict(action=action, note=note, target=expected),
                        )
                    ),
                    identity,
                ),
            )
            return
        if p["hard_blocks"]:
            raise ValueError("new owner-authored workflow/contract required: " + "; ".join(p["hard_blocks"]))
        if r["state"] != "ready" and not (r["state"] == "awaiting_owner" and action == "approve"):
            raise ValueError("dynamic graph expansion waits for an exact owner decision")
        if w["plan"]["policy"]["replanning"]["mode"] == "off":
            raise ValueError("replanning disabled")
        for name in p["rewired"]:
            child = unpack(
                db.execute(
                    "SELECT * FROM ap_nodes WHERE workflow_id=? AND name=?",
                    (w["id"], name),
                ).fetchone()
            )
            if (
                child["task_id"]
                or child["candidate_id"]
                or child["generation"]
                or child["inputs"]
                or child["state"] not in ("pending", "draft", "blocked_dependencies")
            ):
                raise ValueError("successor started/consumed inputs; cannot rewire")
        after = normalize(p["after"], p["retired"])
        stamp = now_iso()
        new_names = {t["id"] for t in p["new_tasks"]}
        if (
            db.execute("SELECT count(*) FROM ap_nodes WHERE workflow_id=?", (w["id"],)).fetchone()[0]
            + len(new_names)
            > 200
        ):
            raise ValueError("workflow node budget exceeded")
        for spec in after["nodes"]:
            nid = w["id"] + ":" + spec["id"]
            if spec["id"] in new_names:
                db.execute(
                    "INSERT INTO ap_nodes(id,workflow_id,name,spec,state,ready_at) VALUES(?,?,?,?, 'pending',?)",
                    (nid, w["id"], spec["id"], canonical(spec), stamp),
                )
                task = next(t for t in p["proposal"]["tasks"] if t["task_id"] == spec["id"])
                lineage = dict(
                    depth=1,
                    source_node=n["name"],
                    source_candidate=p["source_candidate"],
                    source_scope=candidate["payload"]["scope"],
                    original_spec=n["spec"],
                    finding_ids=task["finding_ids"],
                    relationship=task["relationship"],
                    invalidated_assumptions=task["invalidated_assumptions"],
                    packet_digest=p["packet_digest"],
                    owner_approved=action == "approve",
                    root_cause=p["proposal"]["diagnosis"],
                    replacement_task=p["replacement_task"],
                )
                db.execute(
                    "INSERT INTO ap_replacements VALUES(?,?,?,?,?,?)",
                    (
                        uuid.uuid4().hex,
                        identity,
                        n["id"],
                        nid,
                        canonical(lineage),
                        stamp,
                    ),
                )
            elif spec["id"] in p["rewired"]:
                db.execute(
                    "UPDATE ap_nodes SET spec=?,state=?,reason='' WHERE id=?",
                    (canonical(spec), "draft" if spec.get("draft") else "pending", nid),
                )
            if spec["id"] in new_names | set(p["rewired"]):
                db.execute("DELETE FROM ap_edges WHERE node_id=?", (nid,))
                for parent in spec["depends_on"]:
                    db.execute(
                        "INSERT INTO ap_edges VALUES(?,?)",
                        (nid, w["id"] + ":" + parent),
                    )
        db.execute("UPDATE ap_workflows SET plan=? WHERE id=?", (canonical(after), w["id"]))
        db.execute(
            "UPDATE ap_nodes SET state='awaiting_replacement',reason='Original candidate retained; waiting for independently accepted replacement' WHERE id=?",
            (n["id"],),
        )
        db.execute(
            "UPDATE ap_graph_revisions SET state='applied',payload=? WHERE id=?",
            (
                canonical(
                    dict(
                        p,
                        owner_decision=dict(
                            action=action or "auto_within_envelope",
                            note=note,
                            target=expected,
                        ),
                    )
                ),
                identity,
            ),
        )
    ap.manager.scheduler.wake()


async def checked_apply(ap, identity, **decision):
    from . import git

    revision = ap.ledger.get("graph_revisions", identity)
    if not revision:
        raise ValueError("graph revision not found")
    if revision["state"] == "applied":
        return apply(ap, identity, **decision)
    node = ap.ledger.get("nodes", revision["payload"]["source_node"])
    candidate = ap.ledger.get("candidates", revision["payload"]["source_candidate"])
    task = ap.manager.db.get_task(node["task_id"])
    actual = await git.scope(
        task["worktree"],
        candidate["payload"]["scope"]["base_sha"],
        await git.sha(task["worktree"]),
    )
    if actual != candidate["payload"]["scope"] or await git.changed_paths(
        task["worktree"], actual["head_sha"]
    ):
        with ap.ledger.transaction() as db:
            db.execute("UPDATE ap_graph_revisions SET state='stale' WHERE id=?", (identity,))
        raise ValueError("source SHA/tree changed after replanning; old proposal refused")
    return apply(ap, identity, **decision)
