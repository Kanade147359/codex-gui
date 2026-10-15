"""Accepted planning artifacts may refine only unconsumed Draft nodes."""

import json
import uuid

from app.models import now_iso
from . import git
from .policy import canonical, digest, manifest, owns


async def propose(ap, planner):
    w = ap.ledger.get("workflows", planner["workflow_id"])
    candidate = ap.ledger.get("candidates", planner["candidate_id"])
    if ap.ledger.rows("plan_revisions", "source_candidate=?", (candidate["id"],)):
        return
    path = planner["spec"].get("plan_artifact")
    if not path:
        return
    _, mode, _ = await git.git(
        w["plan"]["repository"], "ls-tree", candidate["payload"]["scope"]["head_sha"], "--", path
    )
    if not mode.startswith(("100644 ", "100755 ")):
        raise ValueError("planning artifact must be a regular accepted file")
    from .inputs import accepted_text, bytes_digest

    text = await accepted_text(w["plan"]["repository"], candidate["payload"]["scope"]["head_sha"], path)
    raw = json.loads(text)
    if not isinstance(raw, dict) or set(raw) != {"nodes"} or not isinstance(raw["nodes"], list) or not raw["nodes"]:
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
    normalized = manifest({k: v for k, v in plan.items() if k not in ("base_sha", "rules")})
    normalized.update(base_sha=plan["base_sha"], rules=plan.get("rules", {}))
    payload = dict(
        expected_plan=digest(w["plan"]),
        plan=normalized,
        changed=changed,
        before={name: next(spec for spec in w["plan"]["nodes"] if spec["id"] == name) for name in changed},
        expansions=expansions,
        artifact=dict(
            head_sha=candidate["payload"]["scope"]["head_sha"], path=path, digest=bytes_digest(text.encode())
        ),
    )
    with ap.ledger.transaction() as c:
        if c.execute("SELECT 1 FROM ap_plan_revisions WHERE source_candidate=?", (candidate["id"],)).fetchone():
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


def propose_graph(ap, source, replan, fixed, proposal, session):
    """Persist a complete proposed graph; only apply_graph may change nodes/edges."""
    from .ledger import unpack
    revision_id = uuid.uuid4().hex
    w = ap.ledger.get("workflows", source["workflow_id"])
    old = source["spec"]
    if not proposal["tasks"]:
        with ap.ledger.transaction() as c:
            c.execute("UPDATE ap_replans SET state='inconclusive',payload=? WHERE id=? AND state='running'",
                      (canonical(dict(replan["payload"], fixed_input=fixed, proposal=proposal, session=session)), replan["id"]))
            c.execute("UPDATE ap_nodes SET state='blocked_replan',reason='Root cause inconclusive; owner inspection required' WHERE id=? AND state='replanning'", (source["id"],))
        return
    plan = json.loads(canonical(w["plan"]))
    added, expansions = [], []
    existing = {n["id"] for n in plan["nodes"]}
    for task in proposal["tasks"]:
        name = task["task_id"]
        if name in existing:
            raise ValueError("dynamic task ID already exists")
        existing.add(name)
        validation = fixed["validation_profiles"][task["validation_profile"]]
        # An authorized alternative may add checks, never drop the source's mandatory checks.
        if (any(cmd not in validation["commands"] for cmd in old["validation"]["commands"])
            or not set(old["validation"]["artifacts"]) <= set(validation["artifacts"])):
            raise ValueError("root-cause tasks cannot relax mandatory validation")
        if any(not owns(p, old["envelope"]["paths"]) for p in task["paths"]):
            expansions.append(name + ": edit ownership expansion")
        protected = set(w["plan"]["inputs"]) | set(w["plan"].get("rules", {}))
        if any(owns(path, task["paths"]) for path in protected):
            expansions.append(name + ": fixed requirement/rule source may be edited; owner review required")
        spec = dict(id=name, kind=old["kind"],
                    prompt=task["title"] + "\nPurpose: " + task["purpose"] + "\n" + task["implementation"]
                           + "\nCompletion: " + canonical(task["completion_conditions"])
                           + "\nResolve root cause: " + task["root_cause"]
                           + "\nOriginal finding: " + task["finding_id"] + "\nSource candidate SHA (evidence, not accepted input): " + fixed["scope"]["head_sha"]
                           + "\nOriginal instructions: " + old["prompt"],
                    paths=task["paths"], depends_on=task["depends_on"], validation=validation,
                    contract=old["contract"], manual_gates=old["manual_gates"], input_files=old["input_files"],
                    goal_ids=old.get("goal_ids", []), acceptance_ids=old.get("acceptance_ids", []),
                    handoff=old.get("handoff", []), review_files=old.get("review_files", []),
                    integration_mode=old["integration_mode"],
                    replan=dict(parent=source["name"], depth=1, revision=revision_id))
        if old.get("plan_artifact"):
            spec["plan_artifact"] = old["plan_artifact"]
        added.append(spec)
    names = {n["id"] for n in added}
    for spec in added:
        if set(spec["depends_on"]) - (names | set(old["depends_on"])):
            raise ValueError("replan dependencies must be original prerequisites or new siblings")
        # Carry every accepted prerequisite; sibling code chains already contain their ancestry.
        inherited_reports = [p for p in old["depends_on"] if next(n for n in plan["nodes"] if n["id"] == p)["kind"] == "report"]
        if not set(spec["depends_on"]) & names:
            spec["depends_on"] = list(dict.fromkeys(spec["depends_on"] + old["depends_on"]))
        else:
            spec["depends_on"] = list(dict.fromkeys(spec["depends_on"] + inherited_reports))
    leaves = sorted(names - {p for n in added for p in n["depends_on"] if p in names})
    changed, before = [], {}
    for spec in plan["nodes"]:
        if source["name"] in spec["depends_on"]:
            before[spec["id"]] = json.loads(canonical(spec))
            spec["depends_on"] = list(dict.fromkeys([p for p in spec["depends_on"] if p != source["name"]] + leaves))
            changed.append(spec["id"])
    plan["nodes"].extend(added)
    normalized = manifest({k: v for k, v in plan.items() if k not in ("base_sha", "rules")})
    normalized.update(base_sha=plan["base_sha"], rules=plan.get("rules", {}))
    payload = dict(version=1, expected_plan=fixed["expected_plan"], run_generation=fixed["run_generation"],
                   source_node=source["id"], source_candidate=replan["source_candidate"], scope=fixed["scope"],
                   input_digest=fixed["digest"], requirements_digest=fixed["requirements"]["digest"],
                   source_spec_digest=digest(old), plan=normalized, added=sorted(names), changed=changed,
                   before=before, replacement_leaves=leaves, expansions=expansions,
                   proposal=proposal, session=session, owner_changes=proposal["owner_changes"])
    for name in changed:
        node = next(n for n in ap.ledger.rows("nodes", "workflow_id=?", (w["id"],)) if n["name"] == name)
        if not _unconsumed(node):
            payload["owner_changes"].append(name + ": successor already consumed inputs; cannot rewrite")
    mode = w["plan"]["policy"].get("replanning", "propose_only")
    state = "ready" if mode == "auto_within_envelope" and not expansions and not payload["owner_changes"] else "awaiting_owner"
    with ap.ledger.transaction() as c:
        live = unpack(c.execute("SELECT * FROM ap_workflows WHERE id=?", (w["id"],)).fetchone())
        node = unpack(c.execute("SELECT * FROM ap_nodes WHERE id=?", (source["id"],)).fetchone())
        if (live["state"] != "running" or live["generation"] != fixed["run_generation"]
            or digest(live["plan"]) != fixed["expected_plan"] or node["state"] != "replanning"
            or node["candidate_id"] != replan["source_candidate"]):
            raise ValueError("replan source/plan changed before proposal reservation")
        c.execute("INSERT INTO ap_graph_revisions VALUES(?,?,?,?,?,?,?)",
                  (revision_id, w["id"], replan["id"], source["id"], state, canonical(payload), now_iso()))
        c.execute("UPDATE ap_replans SET state='proposed',payload=? WHERE id=? AND state='running'",
                  (canonical(dict(replan["payload"], fixed_input=fixed, proposal=proposal, session=session)), replan["id"]))
        c.execute("UPDATE ap_nodes SET state='blocked_replan',reason='New tasks proposed; graph revision pending, original is not successful' WHERE id=?", (source["id"],))


def _unconsumed(node):
    return (node["state"] in ("pending", "draft", "blocked_dependencies") and not node["task_id"]
            and not node["candidate_id"] and not node["generation"] and not node["inputs"])


def _verify_source(source, candidate, connection):
    """Synchronous final check while the atomic graph transaction holds the ledger lock."""
    import subprocess
    from .inputs import verify
    row = connection.execute("SELECT * FROM tasks WHERE id=?", (source["task_id"],)).fetchone()
    task = dict(row) if row else None
    p = candidate["payload"]
    if not task or task["status"] != "completed" or task["completion_pending"]:
        raise ValueError("source task is no longer idle")
    instructions = [dict(prompt=m["prompt"], kind=m["kind"]) for m in connection.execute(
        "SELECT prompt,kind FROM task_messages WHERE task_id=? AND kind IN ('instruction','steer','scheduled_instruction') ORDER BY id", (task["id"],))]
    if digest(instructions) != p["instructions_digest"] or digest(task["last_prompt"]) != p["prompt_digest"]:
        raise ValueError("source owner instructions changed")
    if digest([task["last_turn_at"], task["semantic_result"]]) != p["execution_key"]:
        raise ValueError("source execution changed")
    command = ["git", "-C", task["worktree"], "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
               "-c", "core.attributesFile=/dev/null"]
    try:
        probe = subprocess.run(command + ["config", "--null", "--name-only", "--get-regexp", r"^filter\..*\.(clean|smudge|process)$"],
                               capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError("cannot safely inspect Git state for graph revision") from error
    if probe.returncode not in (0, 1):
        raise ValueError("cannot safely inspect Git filters for graph revision")
    for section in sorted({k.rsplit(".", 1)[0] for k in probe.stdout.split("\0") if k}):
        for suffix, value in (("clean", ""), ("smudge", ""), ("process", ""), ("required", "false")):
            command.extend(["-c", section + "." + suffix + "=" + value])
    def output(*args):
        try:
            return subprocess.check_output(command + list(args), text=True, timeout=10).strip()
        except (OSError, subprocess.SubprocessError) as error:
            raise ValueError("source Git state cannot be verified for graph revision") from error
    if output("rev-parse", "HEAD") != p["scope"]["head_sha"] or output("rev-parse", "HEAD^{tree}") != p["scope"]["tree_sha"]:
        raise ValueError("stale candidate SHA/tree in graph revision")
    if output("status", "--porcelain", "--", ".", ":(exclude).autopilot-inputs", ":(exclude).autopilot-fix"):
        raise ValueError("source worktree changed")
    verify(task["worktree"], p["requirements"])


def apply_graph(ap, revision, *, expected=None, approve=False, reject=False, note=""):
    """Commit nodes, dependency edges, replacement lineage and plan in ONE transaction."""
    from .ledger import unpack
    with ap.ledger.transaction() as c:
        r = unpack(c.execute("SELECT * FROM ap_graph_revisions WHERE id=?", (revision["id"],)).fetchone())
        if not r:
            raise ValueError("graph revision missing")
        if r["state"] == "applied":
            if approve or reject:
                raise ValueError("graph revision was already decided")
            return
        w = unpack(c.execute("SELECT * FROM ap_workflows WHERE id=?", (r["workflow_id"],)).fetchone())
        p = r["payload"]
        n = unpack(c.execute("SELECT * FROM ap_nodes WHERE id=?", (r["source_node"],)).fetchone())
        candidate = unpack(c.execute("SELECT * FROM ap_candidates WHERE id=?", (p["source_candidate"],)).fetchone())
        if (w["state"] not in ("running", "paused") or w["generation"] != p["run_generation"]
            or digest(w["plan"]) != p["expected_plan"] or n["state"] != "blocked_replan"
            or n["candidate_id"] != p["source_candidate"] or digest(n["spec"]) != p["source_spec_digest"]
            or candidate["payload"]["scope"] != p["scope"]):
            raise ValueError("graph revision source, generation or plan is stale/stopped")
        if approve or reject:
            if expected != digest(p) or not note.strip():
                raise ValueError("owner must inspect exact version/digest and provide a rationale")
        if r["state"] not in ("ready", "awaiting_owner"):
            raise ValueError("graph revision is no longer actionable")
        if r["state"] == "awaiting_owner" and not (approve or reject):
            raise ValueError("explicit owner graph decision required")
        if reject:
            c.execute("UPDATE ap_graph_revisions SET state='rejected' WHERE id=?", (r["id"],))
        else:
            if p["owner_changes"]:
                raise ValueError("proposal requires owner-authored requirements/authorization changes; inspect owner_changes before creating a revised workflow")
            if not approve and w["plan"]["policy"].get("replanning") != "auto_within_envelope":
                raise ValueError("automatic graph mutation is not authorized")
            _verify_source(n, candidate, c)
            for name in p["changed"]:
                child = unpack(c.execute("SELECT * FROM ap_nodes WHERE workflow_id=? AND name=?", (w["id"], name)).fetchone())
                if not _unconsumed(child):
                    raise ValueError("started, accepted or input-fixed successor cannot be rewritten")
            for spec in p["plan"]["nodes"]:
                nid = w["id"] + ":" + spec["id"]
                if spec["id"] in p["added"]:
                    c.execute("INSERT INTO ap_nodes(id,workflow_id,name,spec,state,ready_at) VALUES(?,?,?,?,'pending',?)",
                              (nid, w["id"], spec["id"], canonical(spec), now_iso()))
                elif spec["id"] in p["changed"]:
                    c.execute("UPDATE ap_nodes SET spec=?,state=?,reason='' WHERE id=?",
                              (canonical(spec), "draft" if spec["draft"] else "pending", nid))
                    c.execute("DELETE FROM ap_edges WHERE node_id=?", (nid,))
                else:
                    continue
                for parent in spec["depends_on"]:
                    c.execute("INSERT INTO ap_edges VALUES(?,?)", (nid, w["id"] + ":" + parent))
            c.execute("UPDATE ap_workflows SET plan=? WHERE id=?", (canonical(p["plan"]), w["id"]))
            c.execute("UPDATE ap_nodes SET state='awaiting_replacement',reason='Original candidate remains unaccepted; replacement validation and independent review required' WHERE id=?", (n["id"],))
            c.execute("UPDATE ap_graph_revisions SET state='applied' WHERE id=?", (r["id"],))
        if approve or reject:
            from .decisions import authorization
            c.execute("INSERT INTO ap_decisions VALUES(?,?,?,?,?,?)", (
                uuid.uuid4().hex, n["id"], expected, "approved" if approve else "rejected",
                canonical(dict(version=1, purpose="graph_revision", graph_revision=r["id"],
                               run_generation=w["generation"], candidate_id=n["candidate_id"],
                               authorization=authorization(w), note=note)), now_iso()))
