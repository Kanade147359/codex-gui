"""Bounded independent diagnosis. Model claims never directly mutate the graph or authority."""

import json
import time
import uuid

from app.models import now_iso
from .policy import canonical, digest
from .goals import ROOT_CAUSES, diagnosis_schema, validate_diagnosis
from .runner import redact


def normalize_policy(raw):
    if not isinstance(raw, dict) or set(raw) - {
        "mode",
        "max_tasks",
        "max_per_node",
        "max_depth",
    }:
        raise ValueError("invalid replanning policy")
    value = dict(mode="propose_only", max_tasks=3, max_per_node=1, max_depth=1) | raw
    if value["mode"] not in ("off", "propose_only", "auto_within_envelope"):
        raise ValueError("invalid replanning mode")
    for key, maximum in (("max_tasks", 3), ("max_per_node", 1), ("max_depth", 1)):
        if type(value[key]) is not int or not 1 <= value[key] <= maximum:
            raise ValueError("replanning limit cannot exceed its supported bound")
    return value


def proposal_schema():
    string = dict(type="string")
    strings = dict(type="array", items=string)
    task_props = {
        k: string
        for k in (
            "task_id",
            "title",
            "purpose",
            "parent_task",
            "root_cause",
            "implementation",
            "rationale",
        )
    }
    task_props.update(
        {
            k: strings
            for k in (
                "finding_ids",
                "goal_ids",
                "paths",
                "depends_on",
                "manual_gates",
                "invalidated_assumptions",
            )
        }
    )
    task_props.update(
        relationship=dict(type="string", enum=["modify", "replace", "complement"]),
        validation=dict(type="object"),
        completion_contract=dict(type="object"),
        acceptance_conditions=dict(type="array", items=dict(type="object")),
    )
    properties = dict(
        version=dict(type="integer", enum=[1]),
        packet_digest=string,
        source_candidate=string,
        scope=dict(
            type="object",
            additionalProperties=False,
            properties={k: string for k in ("base_sha", "head_sha", "tree_sha", "diff_digest")},
            required=["base_sha", "head_sha", "tree_sha", "diff_digest"],
        ),
        diagnosis=diagnosis_schema(),
        tasks=dict(
            type="array",
            maxItems=3,
            items=dict(
                type="object",
                additionalProperties=False,
                properties=task_props,
                required=list(task_props),
            ),
        ),
        replacement_task=string,
        requested_changes=dict(
            type="array",
            items=dict(
                type="object",
                additionalProperties=False,
                properties=dict(kind=string, description=string),
                required=["kind", "description"],
            ),
        ),
        requested_reads=dict(
            type="array",
            maxItems=16,
            items=dict(
                type="object",
                additionalProperties=False,
                properties=dict(path=string, start=dict(type="integer"), end=dict(type="integer")),
                required=["path", "start", "end"],
            ),
        ),
    )
    return dict(
        type="object",
        additionalProperties=False,
        properties=properties,
        required=list(properties),
    )


def validate_proposal(value, packet):
    if (
        not isinstance(value, dict)
        or set(value) != set(proposal_schema()["required"])
        or value["version"] != 1
        or value["packet_digest"] != packet["digest"]
        or value["source_candidate"] != packet["candidate"]["id"]
        or value["scope"] != packet["candidate"]["scope"]
    ):
        raise ValueError("replan output does not match the fixed packet/candidate")
    validate_diagnosis(value["diagnosis"])
    if not isinstance(value["requested_reads"], list) or len(value["requested_reads"]) > 16:
        raise ValueError("invalid bounded replanner source reads")
    for request in value["requested_reads"]:
        if not isinstance(request, dict) or set(request) != {"path", "start", "end"}:
            raise ValueError("invalid replanner source range")
    if value["requested_reads"] and (
        value["tasks"] or value["diagnosis"]["classification"] != "INCONCLUSIVE"
    ):
        raise ValueError("analysis must remain inconclusive while evidence is missing")
    if (
        not isinstance(value["tasks"], list)
        or len(value["tasks"]) > packet["authority"]["policy"]["max_tasks"]
    ):
        raise ValueError("replan task budget exceeded")
    required = set(proposal_schema()["properties"]["tasks"]["items"]["required"])
    for task in value["tasks"]:
        if (
            not isinstance(task, dict)
            or set(task) != required
            or any(
                not isinstance(task[k], str) or not task[k].strip()
                for k in (
                    "task_id",
                    "title",
                    "purpose",
                    "parent_task",
                    "root_cause",
                    "implementation",
                    "rationale",
                )
            )
        ):
            raise ValueError("new tasks need identity, purpose, lineage and rationale")
        if task["parent_task"] != packet["source_node"]["name"] or task["relationship"] not in (
            "modify",
            "replace",
            "complement",
        ):
            raise ValueError("invalid replan relationship")
        for key in (
            "finding_ids",
            "goal_ids",
            "paths",
            "depends_on",
            "manual_gates",
            "invalidated_assumptions",
        ):
            if not isinstance(task[key], list) or any(not isinstance(v, str) for v in task[key]):
                raise ValueError("invalid replan task references")
        if not task["finding_ids"] or not set(task["finding_ids"]) <= {f["id"] for f in packet["findings"]}:
            raise ValueError("replan must reference original findings")
    if not isinstance(value["requested_changes"], list) or any(
        not isinstance(c, dict)
        or set(c) != {"kind", "description"}
        or any(not isinstance(c[k], str) or not c[k] for k in c)
        for c in value["requested_changes"]
    ):
        raise ValueError("invalid requested authority changes")
    if value["tasks"] and (
        value["diagnosis"]["classification"] not in ROOT_CAUSES
        or value["replacement_task"] not in {t["task_id"] for t in value["tasks"]}
    ):
        raise ValueError("ordinary defects/environment/unknown cannot generate replacement tasks")
    if not value["tasks"] and value["replacement_task"]:
        raise ValueError("replacement is absent")
    return value


async def verify_evidence(diagnosis, packet, root):
    """Validate quotes against controller records and fixed files, not arbitrary logs or assertions."""
    from .review_context import read
    from . import git

    validate_diagnosis(diagnosis)
    if diagnosis["classification"] not in ROOT_CAUSES:
        return False
    refs = {"task:instruction": packet["source_node"]["spec"]["prompt"]}
    refs.update({d["name"]: d["body"] for d in packet["requirements"]["documents"]})
    for a in packet["source_node"]["spec"].get("assumptions", []):
        refs["assumption:" + a["id"]] = a["text"]
    for a in packet["source_node"]["spec"].get("acceptance_conditions", []):
        refs["acceptance:" + a["id"]] = a["text"]
    for a in packet["requirements"]["goal_index"]["declared"]:
        refs["goal:" + a["id"]] = a["text"]
    found = set()
    for anchor in diagnosis["anchors"]:
        kind, ref, quote = anchor["kind"], anchor["ref"], anchor["quote"]
        if kind == "requirement":
            if ref not in refs or quote not in refs[ref]:
                raise ValueError("root cause cites a nonexistent fixed requirement quote")
        elif kind == "source":
            from .policy import safe_path

            ref = safe_path(ref)
            _, tracked, _ = await git.git(root, "ls-files", "--error-unmatch", "--", ref)
            if tracked.strip() != ref:
                raise ValueError("counterexample is not a tracked fixed source file")
            text = read(root, ref, 1, 240)["text"]
            if quote not in text:
                raise ValueError("source counterexample quote is absent")
        else:
            evidence = next((v for v in packet["validation"] if v["id"] == ref), None)
            if (
                not evidence
                or evidence["state"] not in ("PASS", "FAIL")
                or quote not in canonical(evidence["payload"])
            ):
                raise ValueError("counterexample cites unavailable controller validation evidence")
        found.add(kind)
    # A failed test alone or a model's premise claim is insufficient. A fixed
    # requirement AND an independently inspectable source counterexample are mandatory.
    return {"requirement", "source"} <= found and set(diagnosis["finding_ids"]) <= {
        f["id"] for f in packet["findings"]
    }


def packet(ap, node, workflow, candidate):
    p = candidate["payload"]
    try:
        failure = json.loads(node["reason"])
        failure = failure.get("failure_context", {}) if isinstance(failure, dict) else {}
    except ValueError:
        failure = {}
    value = dict(
        version=1,
        source_node=dict(
            id=node["id"],
            name=node["name"],
            spec=node["spec"],
            generation=node["generation"],
            fix_rounds=node["fix_rounds"],
        ),
        candidate=dict(
            id=candidate["id"],
            scope=p["scope"],
            generation=candidate["generation"],
            requirements_digest=p["requirements_digest"],
        ),
        requirements=p["requirements"],
        plan=workflow["plan"],
        expected_plan=digest(workflow["plan"]),
        run_generation=workflow["generation"],
        findings=ap._findings(node["id"]),
        validation=ap.ledger.rows("validation", "candidate_id=?", (candidate["id"],)),
        reviews=ap.ledger.rows(
            "reviews",
            "candidate_id IN (SELECT id FROM ap_candidates WHERE node_id=?)",
            (node["id"],),
        ),
        fix_history=ap.ledger.rows("operations", "node_id=? AND kind='fix'", (node["id"],)),
        failure_context=failure,
        accepted_inputs=node["inputs"],
        authority=dict(
            paths=node["spec"]["paths"],
            envelope=node["spec"]["envelope"],
            validation=node["spec"]["validation"],
            contract=node["spec"]["contract"],
            permissions=workflow["plan"]["permissions"],
            policy=workflow["plan"]["policy"]["replanning"],
        ),
    )
    value["findings"] = list({f["id"]: f for f in value["findings"] + failure.get("findings", [])}.values())
    if not value["findings"]:
        value["findings"] = [dict(id="validation-root", path="", line=0, reason=node["reason"])]
    value = json.loads(redact(canonical(value)))
    # Requirements and their digests must remain byte exact; secrets in logs are redacted separately.
    value["requirements"] = p["requirements"]
    value["digest"] = digest(value)
    return value


def queue(ap, node, reason, *, fingerprint=None, findings=()):
    w = ap.ledger.get("workflows", node["workflow_id"])
    config = w["plan"]["policy"].get("replanning", normalize_policy({}))
    if (
        config["mode"] == "off"
        or not node["candidate_id"]
        or ap.ledger.rows("replans", "node_id=?", (node["id"],))
    ):
        return False
    if ap.ledger.rows("replacements", "new_node_id=?", (node["id"],)):
        return False  # Depth-one tasks cannot recursively spawn replanners.
    if fingerprint:
        reason = canonical(
            dict(reason=reason, failure_context=dict(fingerprint=fingerprint, findings=findings))
        )
    return ap.ledger.transition(node["id"], node["state"], "analysis_queued", reason=reason)


def provider_ready(ap, node, workflow):
    rows = ap.ledger.rows("replans", "node_id=?", (node["id"],))
    if rows and rows[0]["payload"].get("retry_at", 0) > time.time():
        return False
    key = ap._availability_key("codex", workflow)
    availability = ap.ledger.rows("provider_availability", "key=?", (key,))
    if not availability or availability[0]["state"] != "included_quota":
        return True
    if availability[0]["recheck_at"] > time.time():
        return False
    with ap.ledger.transaction() as db:
        return (
            db.execute(
                "UPDATE ap_provider_availability SET probe_owner=?,recheck_at=? WHERE key=? AND recheck_at<=?",
                (
                    node["id"],
                    time.time() + workflow["plan"]["policy"]["review_timeout"],
                    key,
                    time.time(),
                ),
            ).rowcount
            == 1
        )


async def analyze(ap, node):
    from .providers import ProviderFailure
    from . import git, graph_revision

    w = ap.ledger.get("workflows", node["workflow_id"])
    c = ap.ledger.get("candidates", node["candidate_id"])
    root = await ap._snapshot(c)
    task = ap.manager.db.get_task(node["task_id"])
    if (
        await git.scope(
            task["worktree"],
            c["payload"]["scope"]["base_sha"],
            await git.sha(task["worktree"]),
        )
        != c["payload"]["scope"]
    ):
        raise ValueError("source SHA changed before independent replanning")
    existing = ap.ledger.rows("replans", "node_id=?", (node["id"],))
    if existing:
        record = existing[0]
        if record["state"] != "waiting_provider":
            ap.ledger.transition(
                node["id"],
                "analyzing",
                "reconciling",
                reason="Existing independent analysis outcome requires reconciliation",
            )
            return
        p = record["payload"]["packet"]
    else:
        p = packet(ap, node, w, c)
        from .review_context import partitions

        pages, related = await partitions(c, root)
        p["source_context"] = dict(diff_partitions=pages, related=related, requested_code=[])
        p.pop("digest")
        p["digest"] = digest(p)
        record = dict(
            id=uuid.uuid4().hex,
            node_id=node["id"],
            source_candidate=c["id"],
            run_generation=w["generation"],
            state="claimed",
            payload=dict(packet=p),
            created_at=now_iso(),
        )
        with ap.ledger.transaction() as db:
            db.execute(
                "INSERT INTO ap_replans VALUES(?,?,?,?,?,?,?)",
                (
                    record["id"],
                    node["id"],
                    c["id"],
                    w["generation"],
                    "claimed",
                    canonical(record["payload"]),
                    record["created_at"],
                ),
            )
    directory = ap.root / "logs" / record["id"]
    directory.mkdir(parents=True, exist_ok=True)
    attempt = dict(id=record["id"], event_log=str(directory / "events.jsonl"))

    def identity(value):
        from app.procinfo import process_identity

        value["proc_identity"] = process_identity(value.get("pid"))
        with ap.ledger.transaction() as db:
            db.execute(
                "UPDATE ap_replans SET state='running',payload=? WHERE id=? AND state IN ('claimed','waiting_provider')",
                (canonical(dict(record["payload"], identity=value)), record["id"]),
            )

    try:
        slots = ap.manager._slots
        if slots:
            await slots.acquire()
        try:
            from .review_context import read
            from .policy import safe_path

            for round_number in range(5):
                result = await ap.providers["codex"].start_replan(
                    p, w["plan"]["policy"], root, attempt, identity
                )
                proposal = validate_proposal({k: result[k] for k in proposal_schema()["required"]}, p)
                if not result.get("session") or result["session"] in {
                    task["codex_thread_id"],
                    *[r["payload"].get("session") for r in p["reviews"]],
                }:
                    raise ValueError("replanner must use a fresh independent Codex thread")
                if not proposal["requested_reads"]:
                    break
                reads = p["source_context"]["requested_code"]
                if round_number == 4 or len(reads) + len(proposal["requested_reads"]) > 16:
                    raise ValueError("bounded replanner read budget exhausted; no graph applied")
                for request in proposal["requested_reads"]:
                    path = safe_path(request["path"])
                    _, tracked, _ = await git.git(root, "ls-files", "--error-unmatch", "--", path)
                    if tracked.strip() != path:
                        raise ValueError("replanner requested an unregistered snapshot file")
                    reads.append(read(root, path, request["start"], request["end"]))
                p.pop("digest")
                p["digest"] = digest(p)
                record["payload"]["packet"] = p
                with ap.ledger.transaction() as db:
                    db.execute(
                        "UPDATE ap_replans SET payload=? WHERE id=? AND state='running'",
                        (canonical(record["payload"]), record["id"]),
                    )
        finally:
            if slots:
                slots.release()
        proposal = validate_proposal({k: result[k] for k in proposal_schema()["required"]}, p)
        await ap._snapshot(c)
        supported = await verify_evidence(proposal["diagnosis"], p, root)
        with ap.ledger.transaction() as db:
            live = db.execute("SELECT state,generation FROM ap_workflows WHERE id=?", (w["id"],)).fetchone()
            now = db.execute(
                "SELECT candidate_id,generation,state FROM ap_nodes WHERE id=?",
                (node["id"],),
            ).fetchone()
            if (
                live["state"] not in ("running", "paused")
                or live["generation"] != w["generation"]
                or now["candidate_id"] != c["id"]
                or now["generation"] != c["generation"]
                or now["state"] != "analyzing"
            ):
                db.execute("UPDATE ap_replans SET state='stale' WHERE id=?", (record["id"],))
                return
            db.execute(
                "UPDATE ap_replans SET state='completed',payload=? WHERE id=?",
                (
                    canonical(
                        dict(
                            record["payload"],
                            proposal=proposal,
                            session=result["session"],
                            provider="codex",
                            evidence_supported=supported,
                        )
                    ),
                    record["id"],
                ),
            )
            db.execute(
                "DELETE FROM ap_provider_availability WHERE key=?", (ap._availability_key("codex", w),)
            )
        if proposal["tasks"] and supported:
            graph_revision.propose(ap, record["id"])
        elif proposal["diagnosis"]["classification"] == "IMPLEMENTATION_DEFECT":
            context = p.get("failure_context", {})
            ap._request_fix(
                node,
                context.get("fingerprint", digest(p["findings"])),
                context.get("findings", p["findings"]),
                _skip_replan=True,
            )
        else:
            ap.ledger.transition(
                node["id"],
                "analyzing",
                "blocked_decision",
                reason="Root cause remains unconfirmed; no tasks or authority changes applied",
            )
    except ProviderFailure as error:
        state = "waiting_provider" if error.kind == "included_quota" else "incomplete"
        with ap.ledger.transaction() as db:
            db.execute(
                "UPDATE ap_replans SET state=?,payload=? WHERE id=?",
                (
                    state,
                    canonical(
                        dict(
                            record["payload"],
                            error_kind=error.kind,
                            reason=redact(str(error)),
                            retry_at=time.time() + w["plan"]["policy"]["provider_backoff"],
                        )
                    ),
                    record["id"],
                ),
            )
            if error.kind == "included_quota":
                reset = error.reset_at or time.time() + w["plan"]["policy"]["provider_backoff"]
                db.execute(
                    "INSERT INTO ap_provider_availability VALUES(?,?,'included_quota',?,NULL,?) ON CONFLICT(key) DO UPDATE SET state='included_quota',recheck_at=excluded.recheck_at,probe_owner=NULL,payload=excluded.payload",
                    (
                        ap._availability_key("codex", w),
                        "codex",
                        reset,
                        canonical(
                            dict(
                                error_kind="included_quota",
                                reason="Replanner Codex quota; no paid fallback",
                            )
                        ),
                    ),
                )
        ap.ledger.transition(
            node["id"],
            "analyzing",
            "waiting_replan_provider" if state == "waiting_provider" else "blocked_infrastructure",
            reason="Independent replanner unavailable: " + error.kind,
        )
