"""Small opt-in state machine driven by TaskManager.tick, with durable conditional claims."""

import asyncio
import json
import logging
import re
from pathlib import Path
import time
import uuid

from app import completion
from app.models import now_iso, make_task_id, ACTIVE_STATUSES, WAITING_STATUSES
from app.procinfo import process_identity, same_process
from . import git, inputs as fixed_inputs, decisions, planning, goals, replanning
from .ledger import Ledger, unpack
from .policy import canonical, digest, manifest, input_digest
from .providers import ClaudeProvider, CodexProvider, ProviderFailure, validate_result, validate_codex_route
from .runner import Validator, InfrastructureUnavailable, redact

log = logging.getLogger(__name__)
BLOCKED = {
    "blocked_decision",
    "blocked_infrastructure",
    "blocked_configuration",
    "blocked_auth",
    "blocked_no_progress",
    "blocked_budget",
    "blocked_scope",
    "blocked_result",
    "blocked_dependencies",
    "reconciling",
    "stopped",
    "blocked_replan",
    "awaiting_replacement",
    "superseded",
}


class Autopilot:
    def __init__(self, manager, *, providers=None, validator=None):
        self.manager = manager
        self.ledger = Ledger(manager.db)
        self.providers = providers or dict(claude=ClaudeProvider(), codex=CodexProvider(manager.settings.codex_bin))
        self.validator = validator or Validator()
        self.root = manager.settings.home / "autopilot"
        self.jobs = {}
        self.processes = {}
        self.owner = uuid.uuid4().hex
        self.recovered = False
        self.closed = False

    async def dry_run(self, raw):
        plan = manifest(raw)
        repo = Path(plan["repository"]).expanduser().resolve()
        from app.git_manager import repo_toplevel

        if await repo_toplevel(repo) != str(repo):
            raise ValueError("workflow repository must be its Git root")
        plan["repository"] = str(repo)
        plan["base_sha"] = await git.sha(repo, plan["base_ref"])
        plan["rules"] = await fixed_inputs.freeze_rules(repo, plan["base_sha"])
        for n in plan["nodes"]:
            if n.get("task_id"):
                task = self.manager.db.get_task(n["task_id"])
                if not task or task["repository"] != str(repo):
                    raise ValueError("backlog task repository mismatch")
                if json.loads(task["completion_contract"]) != n["contract"]:
                    raise ValueError("backlog opt-in requires the unchanged task contract, including manual gates")
                if (
                    task["status"] != "completed"
                    or task["task_outcome"] != "needs_review"
                    or task["completion_pending"]
                ):
                    raise ValueError("backlog task is not idle needs_review")
                if await git.changed_paths(task["worktree"], task["base_sha"]):
                    # Existing dirty worktrees may include unrelated owner edits. Only precommitted backlogs are safe.
                    _, status, _ = await git.git(
                        task["worktree"],
                        "status",
                        "--porcelain",
                        "--",
                        ".",
                        ":(exclude).autopilot-inputs",
                        ":(exclude).autopilot-fix",
                    )
                    if status:
                        raise ValueError("commit and inspect backlog changes before opt-in")
        warnings = []
        if not plan["policy"]["extra_usage_disabled_confirmed"]:
            warnings.append("owner billing confirmation missing; real providers will not start")
        for n in plan["nodes"]:
            if n["manual_gates"] or n["contract"].get("manual_approval"):
                warnings.append(f"{n['id']}: explicit owner decision required")
        return dict(
            plan=plan,
            input_digest=digest(plan),
            warnings=warnings,
            execution_order=[n["id"] for n in plan["nodes"]],
            review_provider="Claude Code; only confirmed included quota permits independent Codex fallback",
            target="main (explicit permission)"
            if plan["permissions"]["publish_main"]
            else "workflow-owned integration ref",
            isolation="managed validators only; agent-issued shell builds are not OS concurrency limited",
        )

    async def register(self, raw, expected_digest):
        preview = await self.dry_run(raw)
        if preview["input_digest"] != expected_digest:
            raise ValueError("dry-run digest changed; inspect the new plan before Start")
        return self.ledger.register(preview["plan"])

    async def control(self, workflow_id, action):
        if action not in ("start", "pause", "resume", "stop", "reconcile"):
            raise ValueError("invalid workflow action")
        w = self.ledger.get("workflows", workflow_id)
        if not w:
            raise ValueError("workflow not found")
        if action == "reconcile":
            self.reconcile(workflow_id)
            return self.ledger.view(workflow_id)
        with self.ledger.transaction() as c:
            if action == "stop":
                c.execute(
                    "UPDATE ap_workflows SET state='stopped',generation=generation+1,reason='Owner Stop; automatic continuation disabled' WHERE id=?",
                    (workflow_id,),
                )
                c.execute(
                    "UPDATE ap_nodes SET state='stopped',reason='Owner Stop' WHERE workflow_id=? AND state!='accepted'",
                    (workflow_id,),
                )
            elif action == "pause":
                c.execute(
                    "UPDATE ap_workflows SET state='paused',reason='New dispatch paused; running read-only reviews and validation may finish; acceptance waits for Resume' WHERE id=? AND state='running'",
                    (workflow_id,),
                )
            elif action in ("start", "resume"):
                allowed = ("draft",) if action == "start" else ("paused", "waiting", "running")
                if w["state"] not in allowed:
                    raise ValueError("stopped workflows cannot resume; register a reviewed new plan")
                c.execute("UPDATE ap_workflows SET state='running',reason='' WHERE id=?", (workflow_id,))
                if action == "resume":
                    # An explicit owner Resume may retry repaired infrastructure/auth/config; it never clears gates,
                    # fix budgets, no-progress stops, Stop or unknown external operation outcomes.
                    for row in c.execute(
                        "SELECT * FROM ap_nodes WHERE workflow_id=? AND state IN ('blocked_auth','blocked_configuration','blocked_infrastructure')",
                        (workflow_id,),
                    ).fetchall():
                        node = unpack(row)
                        if not node["task_id"]:
                            next_state = "pending"
                        elif not self.manager.db.get_task(node["task_id"]):
                            next_state = "reconciling"
                        elif not node["candidate_id"]:
                            next_state = "implementing"
                        else:
                            passed = c.execute(
                                "SELECT 1 FROM ap_validation WHERE candidate_id=? AND state='PASS'",
                                (node["candidate_id"],),
                            ).fetchone()
                            next_state = "review_queued" if passed else "submitted"
                        c.execute(
                            "UPDATE ap_nodes SET state=?,reason='Owner Resume: retry after infrastructure correction' WHERE id=?",
                            (next_state, node["id"]),
                        )
        if action == "stop":
            for node in self.ledger.rows("nodes", "workflow_id=?", (workflow_id,)):
                job = self.jobs.get(node["id"])
                if job:
                    job.cancel()
                task = self.manager.db.get_task(node["task_id"]) if node["task_id"] else None
                if task and task["status"] in ACTIVE_STATUSES | WAITING_STATUSES | {"blocked"}:
                    await self.manager.stop(task["id"])
                for r in self.ledger.rows(
                    "reviews",
                    "candidate_id IN (SELECT id FROM ap_candidates WHERE node_id=?) AND state IN ('claimed','running')",
                    (node["id"],),
                ):
                    await self.providers[r["provider"]].cancel(r["id"])
            await asyncio.gather(
                *(j for nid, j in list(self.jobs.items()) if nid.startswith(workflow_id + ":")), return_exceptions=True
            )
            with self.ledger.transaction() as c:
                c.execute(
                    "UPDATE ap_reviews SET state='cancelled' WHERE state IN ('claimed','running') AND candidate_id IN (SELECT ac.id FROM ap_candidates ac JOIN ap_nodes n ON n.id=ac.node_id WHERE n.workflow_id=?)",
                    (workflow_id,),
                )
                c.execute(
                    "UPDATE ap_validation SET state='cancelled' WHERE state='running' AND candidate_id IN (SELECT ac.id FROM ap_candidates ac JOIN ap_nodes n ON n.id=ac.node_id WHERE n.workflow_id=?)",
                    (workflow_id,),
                )
        self.manager.scheduler.wake()
        if action == "resume":
            self.reconcile(workflow_id)
        return self.ledger.view(workflow_id)

    def can_dispatch(self, task_id):
        n = self.ledger.node_for_task(task_id)
        if not n:
            return True
        w = self.ledger.get("workflows", n["workflow_id"])
        if w["state"] != "running" or n["state"] not in ("implementing", "fixing", "conflict"):
            return False
        own = [x for x in self.ledger.rows("nodes", "workflow_id=?", (w["id"],)) if x["task_id"] in self.manager._jobs]
        # Reuse _slots; no second semaphore is held while an implementation waits for it.
        global_own = [x for x in self.ledger.rows("nodes") if x["task_id"] in self.manager._jobs]
        return (
            len(own) < w["plan"]["policy"]["implementation_slots"]
            and len(global_own) < self.manager.settings.autopilot_implementation_slots
        )

    async def preflight_execution(self, task):
        node = self.ledger.node_for_task(task["id"])
        if not node:
            return
        workflow = self.ledger.get("workflows", node["workflow_id"])
        from .environment import preflight

        await preflight(task["worktree"], node["spec"]["validation"])
        capability = await self.providers["codex"].probe_capabilities_and_auth(workflow["plan"]["policy"])
        if capability.get("fixture") is True:
            return
        client = await self.manager.client()
        await self.manager._require_subscription(client)
        response = await client.request("config/read", dict(cwd=task["worktree"], includeLayers=False))
        validate_codex_route(response.get("config") if isinstance(response, dict) else None)

    def queue_owner_turn(self, task, turn, fields, _connection=None, _scheduled=False):
        node = self.ledger.node_for_task(task["id"])
        if not node:
            return False
        from app.task_manager import TaskError

        workflow = self.ledger.get("workflows", node["workflow_id"])
        queued = self.manager._queue_fields(turn, **fields)
        from contextlib import nullcontext

        with nullcontext(_connection) if _connection is not None else self.ledger.transaction() as c:
            current = unpack(c.execute("SELECT * FROM ap_nodes WHERE id=?", (node["id"],)).fetchone())
            run = c.execute("SELECT state,generation FROM ap_workflows WHERE id=?", (workflow["id"],)).fetchone()
            if (
                run["state"] not in ("running", "paused", "completed")
                or run["generation"] != workflow["generation"]
                or current["state"] in ("reconciling", "reconciling_git", "publishing", "stopped", "awaiting_replacement", "superseded")
            ):
                raise TaskError(
                    "Stopped, replaced or uncertain workflow requires an inspected recovery plan", 409, "autopilot_stopped"
                )
            if (
                node["spec"]["manual_gates"]
                or node["spec"]["contract"].get("manual_approval")
                or json.loads(task["completion_contract"]).get("manual_approval")
            ):
                raise TaskError(
                    "An instruction cannot clear required owner/design approval", 409, "autopilot_manual_gate"
                )
            children = c.execute(
                "SELECT n.state FROM ap_nodes n JOIN ap_edges e ON e.node_id=n.id WHERE e.parent_id=?", (node["id"],)
            ).fetchall()
            if any(child["state"] not in ("pending", "blocked_dependencies") for child in children):
                raise TaskError(
                    "Pinned inputs were already consumed; use a revised workflow plan", 409, "autopilot_input_consumed"
                )
            if not _scheduled and (
                c.execute(
                    "UPDATE tasks SET status='queued',"
                    + ",".join(f"{key}=?" for key in queued)
                    + " WHERE id=? AND status=? AND completion_pending=0 AND codex_thread_id IS ?",
                    (*queued.values(), task["id"], task["status"], task["codex_thread_id"]),
                ).rowcount
                != 1
            ):
                raise TaskError("Task changed while reserving the new instruction", 409, "active")
            c.execute(
                "UPDATE ap_nodes SET state='implementing',generation=generation+1,reason='',fix_rounds=?,no_progress=? WHERE id=?",
                (
                    0 if current["state"] == "accepted" else current["fix_rounds"],
                    0 if current["state"] == "accepted" else current["no_progress"],
                    node["id"],
                ),
            )
            c.execute(
                "UPDATE ap_nodes SET state='pending',reason='' WHERE state='blocked_dependencies' AND id IN (SELECT node_id FROM ap_edges WHERE parent_id=?)",
                (node["id"],),
            )
            c.execute(
                "UPDATE ap_workflows SET state='running',reason='Owner supplied a new leaf turn' WHERE id=? AND state='completed'",
                (workflow["id"],),
            )
            c.execute(
                "INSERT INTO ap_operations VALUES(?,?, 'owner_turn',?,'reserved',?,?)",
                (
                    uuid.uuid4().hex,
                    node["id"],
                    current["generation"] + 1,
                    canonical(dict(prompt_digest=digest(turn.prompt), thread=turn.resume_thread)),
                    now_iso(),
                ),
            )
            if not _scheduled:
                self.manager.db._insert_message(task["id"], turn.message())
            if current["candidate_id"]:
                c.execute(
                    "UPDATE ap_reviews SET state='cancelled' WHERE candidate_id=? AND state IN ('claimed','running')",
                    (current["candidate_id"],),
                )
                c.execute(
                    "UPDATE ap_validation SET state='cancelled' WHERE candidate_id=? AND state='running'",
                    (current["candidate_id"],),
                )
        job = self.jobs.get(node["id"])
        if job:
            job.cancel()
        if not _scheduled:
            self.manager.db._notify(task["id"], task["status"], "queued")
        return True

    def _spawn(self, n, old, new, work):
        w = self.ledger.get("workflows", n["workflow_id"])
        if w["state"] != "running":
            return
        resource = {"reviewing": "review", "replanning": "review", "validating": "validator"}.get(new)
        if new == "launching" and n["spec"]["kind"] == "integration":
            resource = "integration"
        limit = (
            min(
                w["plan"]["policy"][resource + "_slots"],
                getattr(self.manager.settings, "autopilot_" + resource + "_slots"),
            )
            if resource
            else None
        )
        if n["id"] in self.jobs or not self.ledger.claim(n["id"], old, new, limit):
            return
        job = asyncio.create_task(self._guarded(n["id"], work))
        self.jobs[n["id"]] = job

        def done(j):
            if self.jobs.get(n["id"]) is j:
                self.jobs.pop(n["id"], None)
            self.manager.scheduler.wake()

        job.add_done_callback(done)

    async def _guarded(self, nid, work):
        try:
            await work(self.ledger.get("nodes", nid))
        except asyncio.CancelledError:
            # Durable pending operations are reconciled, never blindly relaunched.
            raise
        except (InfrastructureUnavailable, FileNotFoundError) as e:
            n = self.ledger.get("nodes", nid)
            self.ledger.transition(nid, n["state"], "blocked_infrastructure", reason=redact(str(e))[:1000])
        except Exception as e:
            log.error("Autopilot operation failed for %s: %s: %s", nid, type(e).__name__, redact(str(e)))
            n = self.ledger.get("nodes", nid)
            self.ledger.transition(nid, n["state"], "blocked_configuration", reason=redact(str(e))[:1000])

    def tick(self):
        if self.closed:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        if not self.recovered:
            self.reconcile()
            self.recovered = True
        for w in self.ledger.rows("workflows", "state='running'"):
            nodes = self.ledger.rows("nodes", "workflow_id=?", (w["id"],))
            policy = w["plan"]["policy"]
            for planner in nodes:
                if (
                    planner["state"] == "accepted"
                    and planner["spec"].get("plan_artifact")
                    and planner["id"] not in self.jobs
                ):
                    existing = self.ledger.rows("plan_revisions", "source_candidate=?", (planner["candidate_id"],))
                    if not existing:
                        self._spawn(planner, "accepted", "accepted", self._plan_revision)
            for revision in self.ledger.rows("plan_revisions", "workflow_id=? AND state='ready'", (w["id"],)):
                try:
                    planning.apply(self, revision)
                except ValueError as error:
                    with self.ledger.transaction() as c:
                        c.execute(
                            "UPDATE ap_plan_revisions SET state='stale',payload=? WHERE id=? AND state='ready'",
                            (canonical(dict(revision["payload"], reason=redact(str(error)))), revision["id"]),
                        )
            for revision in self.ledger.rows("graph_revisions", "workflow_id=? AND state='ready'", (w["id"],)):
                try:
                    planning.apply_graph(self, revision)
                except ValueError as error:
                    with self.ledger.transaction() as c:
                        c.execute("UPDATE ap_graph_revisions SET state='stale',payload=? WHERE id=? AND state='ready'",
                                  (canonical(dict(revision["payload"], reason=redact(str(error)))), revision["id"]))
            for revision in self.ledger.rows("graph_revisions", "workflow_id=? AND state='applied'", (w["id"],)):
                leaves = revision["payload"]["replacement_leaves"]
                if all(self.ledger.get("nodes", w["id"] + ":" + name)["state"] == "accepted" for name in leaves):
                    self.ledger.transition(revision["source_node"], "awaiting_replacement", "superseded",
                                           reason="Replacement deliverables accepted; original candidate was never accepted")
            w = self.ledger.get("workflows", w["id"])
            nodes = self.ledger.rows("nodes", "workflow_id=?", (w["id"],))
            if all(n["state"] in ("accepted", "superseded") for n in nodes):
                if w["plan"]["permissions"]["publish_main"]:
                    if not any(n["id"] in self.jobs for n in nodes):
                        # Publication is an explicit operation, never deployment or live checkout modification.
                        self._spawn(nodes[-1], "accepted", "publishing", self._publish)
                else:
                    with self.ledger.transaction() as c:
                        c.execute(
                            "UPDATE ap_workflows SET state='completed',reason='All candidates independently accepted; not deployed' WHERE id=? AND state='running'",
                            (w["id"],),
                        )
                continue
            by_name = {n["name"]: n for n in nodes}
            all_nodes = self.ledger.rows("nodes")
            reviews = sum(n["state"] in ("reviewing", "replanning") for n in all_nodes)
            validators = sum(n["state"] == "validating" for n in all_nodes)
            integrations = sum(
                n["state"] in ("launching", "integrating") and n["spec"]["kind"] == "integration" for n in all_nodes
            )
            for n in nodes:
                if (
                    n["state"] == "blocked_dependencies"
                    and n["spec"]["depends_on"]
                    and all(by_name[p]["state"] == "accepted" for p in n["spec"]["depends_on"])
                ):
                    if self.ledger.transition(
                        n["id"], "blocked_dependencies", "pending", reason="Pinned parent gates resolved"
                    ):
                        n = self.ledger.get("nodes", n["id"])
                if n["id"] in self.jobs or n["state"] in BLOCKED:
                    continue
                state = n["state"]
                if state == "pending":
                    parents = [by_name[p] for p in n["spec"]["depends_on"]]
                    if any(p["state"] in BLOCKED for p in parents):
                        self.ledger.transition(
                            n["id"], state, "blocked_dependencies", reason="A required accepted input is unavailable"
                        )
                        continue
                    if not all(p["state"] == "accepted" for p in parents):
                        continue
                    if n["spec"]["manual_gates"] and not decisions.gate_approved(self.ledger, n, w):
                        self.ledger.transition(
                            n["id"],
                            state,
                            "blocked_decision",
                            reason="Required owner gate: "
                            + ", ".join(n["spec"]["manual_gates"] or ["manual approval"]),
                        )
                        continue
                    if n["spec"]["kind"] == "integration" and integrations >= min(
                        policy["integration_slots"], self.manager.settings.autopilot_integration_slots
                    ):
                        continue
                    self._spawn(n, state, "launching", self._launch)
                    if n["spec"]["kind"] == "integration":
                        integrations += 1
                elif state in ("implementing", "fixing", "conflict"):
                    task = self.manager.db.get_task(n["task_id"])
                    if (
                        task
                        and task["status"] == "completed"
                        and not task["completion_pending"]
                        and n["task_id"] not in self.manager._jobs
                    ):
                        if state == "conflict":
                            self._spawn(n, state, "integrating", self._finish_conflict)
                        else:
                            self._spawn(n, state, "submitting", self._submit)
                    elif task and task["status"] == "waiting-for-quota":
                        self.ledger.transition(
                            n["id"],
                            state,
                            "waiting_implementation_provider",
                            reason="Included Codex quota unavailable; no provider slot held",
                        )
                    elif task and task["status"] in ("failed", "stopped", "blocked", "interrupted"):
                        self.ledger.transition(
                            n["id"], state, "blocked_result", reason="Implementation stopped: " + task["status"]
                        )
                elif state == "submitted" and validators < min(
                    policy["validator_slots"], self.manager.settings.autopilot_validator_slots
                ):
                    self._spawn(n, state, "validating", self._validate)
                    validators += 1
                elif state == "replan_queued" and reviews < min(policy["review_slots"], self.manager.settings.autopilot_review_slots):
                    self._spawn(n, state, "replanning", lambda node: replanning.run(self, node))
                    reviews += 1
                elif state in ("review_queued", "waiting_provider") and reviews < min(
                    policy["review_slots"], self.manager.settings.autopilot_review_slots
                ):
                    provider = self._select_provider(n, w)
                    if provider:
                        self._spawn(n, state, "reviewing", lambda node, p=provider: self._review(node, p))
                        reviews += 1
                elif state == "fix_queued":
                    self._spawn(n, state, "reserving_fix", self._fix)
                elif state == "review_complete":
                    self._spawn(n, state, "reviewing", self._accept_saved)
                elif state == "waiting_implementation_provider":
                    self._spawn(n, state, "probing_implementation", self._probe_implementation)
            # Waiting provider rows occupy no implementation, CPU or validator slot.

    async def _plan_revision(self, node):
        await planning.propose(self, node)

    def owner_decision(self, node_id, expected_target, action, note):
        return decisions.decide(self, node_id, expected_target, action, note)

    def _inputs(self, n):
        result = []
        for edge in self.ledger.rows("edges", "node_id=?", (n["id"],)):
            parent = self.ledger.get("nodes", edge["parent_id"])
            candidate = self.ledger.get("candidates", parent["candidate_id"])
            if parent["state"] != "accepted" or candidate["state"] != "accepted":
                raise ValueError("input candidate is not accepted")
            result.append(
                dict(
                    node=parent["name"],
                    candidate_id=candidate["id"],
                    head_sha=candidate["payload"]["scope"]["head_sha"],
                    kind=parent["spec"]["kind"],
                )
            )
        return sorted(result, key=lambda p: p["node"])

    async def _launch(self, n):
        w = self.ledger.get("workflows", n["workflow_id"])
        if w["state"] == "paused":
            self.ledger.transition(n["id"], "launching", "pending")
            return
        try:
            await self.providers["codex"].probe_capabilities_and_auth(w["plan"]["policy"])
        except ProviderFailure as e:
            self.ledger.transition(
                n["id"],
                "launching",
                "blocked_auth" if e.kind == "auth" else "blocked_configuration",
                reason=redact(str(e)),
            )
            return
        inputs = self._inputs(n)
        code = [p["head_sha"] for p in inputs if p["kind"] != "report"]
        base = w["plan"]["base_sha"] if n["spec"]["kind"] == "integration" or not code else code[0]
        if n["task_id"]:
            task = self.manager.db.get_task(n["task_id"])
            if not task:
                raise ValueError("reserved source task is missing; reconcile the operation before retry")
            bundle = await fixed_inputs.requirements(self, dict(n, inputs=inputs), w)
            if (Path(task["worktree"]) / fixed_inputs.DIRECTORY).exists():
                fixed_inputs.verify(task["worktree"], bundle)
            else:
                fixed_inputs.materialize(task["worktree"], bundle)
            self.ledger.transition(n["id"], "launching", "implementing", inputs=inputs)
            return
        pinned_node = dict(n, inputs=inputs)
        bundle = await fixed_inputs.requirements(self, pinned_node, w)
        task_id = make_task_id()
        # Persist the intended task before worktree/process side effects. Crash = reconcile, not a new task.
        self.ledger.transition(n["id"], "launching", "launching", task_id=task_id, inputs=inputs)
        prompt = (
            n["spec"]["prompt"]
            + "\nEdit ownership: "
            + canonical(n["spec"]["paths"])
            + "\nPinned accepted inputs: "
            + canonical(inputs)
            + "\nRead ALL fixed requirement documents (including COMMON, rules and handoffs) listed in "
            + fixed_inputs.DIRECTORY
            + "/requirements.json before implementation. These versioned requirements remain binding during fixes."
        )
        task = await self.manager.create_task(
            repository=w["plan"]["repository"],
            base_ref=base,
            name=f"Autopilot {w['name'] if 'name' in w else w['plan']['name']}: {n['name']}",
            prompt=prompt,
            completion_contract=n["spec"]["contract"],
            auto_retry=True,
            max_retries=w["plan"]["policy"]["infra_retries"],
            service_tier="default",
            _defer_dispatch=True,
            _task_id=task_id,
            _branch=f"autopilot/{w['id']}/integration/{n['name']}" if n["spec"]["kind"] == "integration" else None,
        )
        fixed_inputs.materialize(task["worktree"], bundle)
        from .environment import preflight

        await preflight(task["worktree"], n["spec"]["validation"])
        if n["spec"]["kind"] == "integration":
            op = self.ledger.record(
                "operations",
                node_id=n["id"],
                kind="integration",
                generation=n["generation"],
                state="running",
                payload=dict(
                    inputs=inputs,
                    start_sha=base,
                    task_id=task_id,
                    expected_ref="refs/heads/" + task["branch"],
                    initial_implementation_prompt=digest(prompt),
                    integration_mode=n["spec"].get("integration_mode", "merge-only"),
                ),
            )
            result = await git.integrate(task["worktree"], code, op["id"])
            self.ledger.finish("operations", op["id"], result["state"], dict(op["payload"], **result))
            if result["state"] == "conflict":
                from app.task_manager import _Turn

                conflict_prompt = (
                    prompt
                    + "\nResolve the integration conflicts in this dedicated worktree. Inspect every conflict, stage resolved files, and complete the merge commit within the approved ownership. Preserve all pinned inputs.\n"
                    + canonical(result)
                )
                self.manager.db.update_task(
                    task_id,
                    pending_turn=_Turn(conflict_prompt, trigger="initial").to_json(),
                    last_prompt=conflict_prompt,
                )
                self.ledger.transition(n["id"], "launching", "conflict", reason=canonical(result["conflicts"]))
                self.manager._dispatch(task_id)
                return
            if n["spec"].get("integration_mode") == "merge-then-implement":
                self.ledger.transition(n["id"], "launching", "implementing")
                self.manager._dispatch(task_id)
                return
            # A mechanical merge is only a submission, never acceptance.
            self.manager.db.set_status(task_id, "starting")
            self.manager.db.set_status(task_id, "running")
            self.manager.db.set_status(
                task_id,
                "completed",
                semantic_result=canonical(
                    dict(
                        status="SUCCESS",
                        reason="Pinned accepted inputs mechanically integrated; validation and review pending",
                    )
                ),
                task_outcome="needs_review",
                finished_at=now_iso(),
            )
        self.ledger.transition(n["id"], "launching", "implementing")
        self.manager._dispatch(task_id)

    async def _submit(self, n):
        w = self.ledger.get("workflows", n["workflow_id"])
        task = self.manager.db.get_task(n["task_id"])
        result = completion.parse_result(task["semantic_result"])
        reserved_fixes = self.ledger.rows("operations", "node_id=? AND kind='fix' AND state='reserved'", (n["id"],))
        if reserved_fixes:
            op = reserved_fixes[-1]
            if op["payload"]["thread"] and op["payload"]["thread"] != task["codex_thread_id"]:
                self.ledger.transition(
                    n["id"],
                    n["state"],
                    "blocked_result",
                    reason="Repair did not resume the reserved original implementation thread",
                )
                return
            self.ledger.finish(
                "operations",
                op["id"],
                "completed",
                dict(op["payload"], implementation_result=result, finished_at=now_iso()),
            )
        if not result:
            self._request_fix(
                n,
                "structured_result_missing",
                [
                    dict(
                        id="completion-result",
                        reason="Return the required complete completion JSON for the existing work; inspect artifacts first",
                        path="",
                        line=0,
                    )
                ],
            )
            return
        if result["status"] != "SUCCESS":
            self.ledger.transition(
                n["id"],
                n["state"],
                "blocked_result",
                reason=f"Implementation reported {result['status']}: {result['reason']}",
            )
            return
        if n["spec"].get("integration_mode") == "merge-then-implement":
            integration = self.ledger.rows("operations", "node_id=? AND kind='integration'", (n["id"],))[-1]
            allowed_prompts = {integration["payload"].get("initial_implementation_prompt")}
            allowed_prompts.update(
                op["payload"].get("prompt_digest")
                for op in self.ledger.rows(
                    "operations", "node_id=? AND kind IN ('integration_implementation','fix','owner_turn')", (n["id"],)
                )
            )
            if not task["codex_thread_id"] or digest(task["last_prompt"]) not in allowed_prompts:
                raise ValueError(
                    "integration implementation turn has not completed; merge/conflict resolution alone is insufficient"
                )
        bundle = await fixed_inputs.requirements(self, n, w)
        fixed_inputs.verify(task["worktree"], bundle)
        head = await git.submit_head(task, n["spec"], w["plan"]["permissions"]["commit_owned_changes"])
        s = await git.scope(task["worktree"], task["base_sha"], head)
        if n["spec"]["kind"] == "integration":
            for item in n["inputs"]:
                if item["kind"] != "report":
                    rc, _, _ = await git.git(
                        task["worktree"], "merge-base", "--is-ancestor", item["head_sha"], head, check=False
                    )
                    if rc != 0:
                        self.ledger.transition(
                            n["id"],
                            n["state"],
                            "blocked_scope",
                            reason="Integration candidate does not contain every pinned accepted input SHA",
                        )
                        return
        previous = self.ledger.get("candidates", n["candidate_id"]) if n["candidate_id"] else None
        owner_turns = self.ledger.rows("operations", "node_id=? AND kind='owner_turn' AND state='reserved'", (n["id"],))
        if previous and previous["payload"]["scope"]["tree_sha"] == s["tree_sha"] and not owner_turns:
            self._request_fix(n, "unchanged_tree", previous["payload"].get("findings", []))
            return
        inherited = self._findings(n["id"])
        instructions = [
            dict(prompt=m["prompt"], kind=m["kind"])
            for m in self.manager.db.list_messages(task["id"])
            if m["kind"] in ("instruction", "steer", "scheduled_instruction")
        ]
        payload = dict(
            scope=s,
            requirements=bundle,
            requirements_digest=bundle["digest"],
            review_files=n["spec"].get("review_files", []),
            task_id=task["id"],
            turn=len(self.manager.db.list_turns(task["id"])),
            execution_key=digest([task["last_turn_at"], task["semantic_result"]]),
            # Freeze the GUI-recorded completion claim for the independent reviewer.
            # The acceptance gate separately rechecks the live task result and execution key.
            recorded_completion=dict(
                parsed_result=result,
                origin=(
                    "controller_mechanical_integration"
                    if n["spec"]["kind"] == "integration" and not task["codex_thread_id"]
                    else "strict_final_assistant_json"
                ),
            ),
            prompt_digest=digest(task["last_prompt"]),
            instructions_digest=digest(instructions),
            repository=task["repository"],
            contract=json.loads(task["completion_contract"]),
            contract_digest=digest(json.loads(task["completion_contract"])),
            policy_digest=digest(w["plan"]["policy"]),
            input_digest=input_digest(n, w),
            profile_digest=digest(n["spec"]["validation"]),
            acceptance=n["spec"]["prompt"]
            + ("\nAdditional owner requirements: " + canonical(instructions) if instructions else ""),
            inputs=n["inputs"],
            changed_paths=sorted(await git.changed_paths(task["worktree"], task["base_sha"])),
            unresolved=[f["id"] for f in inherited],
            findings=inherited,
        )
        submitted = self.ledger.submit(n, w, payload)
        if submitted:
            for op in owner_turns:
                self.ledger.finish(
                    "operations", op["id"], "completed", dict(op["payload"], candidate_id=submitted["id"])
                )

    async def _snapshot(self, candidate):
        p = candidate["payload"]
        root = await git.snapshot(
            p["repository"], p["scope"]["head_sha"], p["scope"]["base_sha"], self.root / "snapshots" / candidate["id"]
        )
        bundle = p.get("requirements", dict(version=1, instruction=p.get("acceptance", ""), documents=[]))
        if (root / fixed_inputs.DIRECTORY).exists():
            fixed_inputs.verify(root, bundle)
        else:
            fixed_inputs.materialize(root, bundle)
        _, dirty, _ = await git.git(root, "status", "--porcelain", "--", ".", ":(exclude).autopilot-inputs")
        if dirty or await git.scope(root, p["scope"]["base_sha"], p["scope"]["head_sha"]) != p["scope"]:
            raise ValueError("snapshot modified; fixed source evidence invalid")
        return root

    def queue_priority(self, task):
        node = self.ledger.node_for_task(task["id"])
        from datetime import datetime, timezone

        try:
            age = (
                datetime.now(timezone.utc) - datetime.fromisoformat(task["created_at"].replace("Z", "+00:00"))
            ).total_seconds()
        except ValueError:
            age = 0
        # Eventually an old ordinary task wins over a continuous stream of repairs.
        return (
            0
            if age >= 300
            else 1
            if node and (node["state"] in ("fixing", "conflict") or node["spec"]["kind"] == "integration")
            else 2,
            task["created_at"],
        )

    def _process_record(self, table, record, identity):
        pid = identity.get("pid")
        identity["proc_identity"] = process_identity(pid)
        payload = dict(record["payload"], identity=identity, owner=self.owner)
        self.ledger.finish(table, record["id"], "running", payload)
        record["payload"] = payload
        if pid:
            self.processes[record["id"]] = identity

    async def _validate(self, n):
        candidate = self.ledger.get("candidates", n["candidate_id"])
        root = await self._snapshot(candidate)
        record = self.ledger.record(
            "validation",
            candidate_id=candidate["id"],
            state="running",
            payload=dict(owner=self.owner, scope=candidate["payload"]["scope"]),
        )
        try:
            result = await self.validator.validate(
                candidate,
                root,
                n["spec"]["validation"],
                self.root / "logs" / record["id"],
                on_start=lambda p: self._process_record("validation", record, dict(pid=p.pid)),
            )
        except (InfrastructureUnavailable, OSError) as e:
            self.ledger.finish(
                "validation", record["id"], "UNAVAILABLE", dict(record["payload"], reason=redact(str(e)))
            )
            raise InfrastructureUnavailable(redact(str(e))) from None
        finally:
            self.processes.pop(record["id"], None)
        # Controller evidence records must match the registered profile and tree.
        if (
            result["profile_digest"] != candidate["payload"]["profile_digest"]
            or result["scope"] != candidate["payload"]["scope"]
        ):
            raise ValueError("validator evidence identity mismatch")
        required_commands = [digest(cmd) for cmd in n["spec"]["validation"]["commands"]]
        executed = [x.get("command_digest") for x in result.get("checks", []) if "command" in x]
        if executed != required_commands:
            self.ledger.finish(
                "validation",
                record["id"],
                "UNAVAILABLE",
                dict(result, reason="Registered mandatory commands were not all executed"),
            )
            raise InfrastructureUnavailable("Registered mandatory commands were not all executed")
        contract = candidate["payload"]["contract"]
        for path in contract.get("required_paths", []):
            p = root / path
            result["checks"].append(
                dict(
                    artifact=path,
                    exit_status=0
                    if p.exists() and not p.is_symlink() and p.resolve().is_relative_to(root.resolve())
                    else 1,
                )
            )
        changed = candidate["payload"]["changed_paths"]
        for path in contract.get("required_changed_paths", []):
            result["checks"].append(dict(required_change=path, exit_status=0 if path in changed else 1))
        if contract.get("require_any_change"):
            result["checks"].append(dict(required_change="any", exit_status=0 if changed else 1))
        if contract.get("require_commit"):
            s = candidate["payload"]["scope"]
            result["checks"].append(dict(required_commit=True, exit_status=0 if s["base_sha"] != s["head_sha"] else 1))
        if any(c["exit_status"] != 0 for c in result["checks"]):
            result["state"] = "FAIL"
        self.ledger.finish("validation", record["id"], result["state"], result)
        if result["state"] == "PASS":
            self.ledger.transition(n["id"], "validating", "review_queued")
        else:
            findings = [
                dict(
                    id="validation-" + str(i),
                    path="",
                    line=0,
                    reason=c.get("excerpt")
                    or "Required validation failed: "
                    + canonical(
                        {
                            key: c[key]
                            for key in (
                                "command",
                                "command_digest",
                                "artifact",
                                "required_change",
                                "required_commit",
                                "exit_status",
                                "timed_out",
                            )
                            if key in c
                        }
                    ),
                )
                for i, c in enumerate(result["checks"])
                if c["exit_status"] != 0 or c.get("timed_out")
            ]
            stable = [dict(f, reason=re.sub(r"\bin \d+(?:\.\d+)?s\b", "in <elapsed>s", f["reason"])) for f in findings]
            if n["fix_rounds"] > 0 and self.ledger.get("workflows", n["workflow_id"])["plan"]["policy"].get("replanning", "propose_only") != "off":
                self.ledger.transition(n["id"], "validating", "review_queued", reason="Post-fix validation failed; independent diagnosis before another repair")
            else:
                self._request_fix(n, digest(stable), findings)

    def _availability_key(self, provider, w):
        p = w["plan"]["policy"]
        return digest([provider, p["account"], p["bucket"], p[provider + "_model"]])

    def _select_provider(self, n, w):
        previous = self.ledger.rows("reviews", "candidate_id=?", (n["candidate_id"],))
        if previous and previous[-1]["payload"].get("retry_at", 0) > time.time():
            return None
        # Only a quota event or the persisted confirmed quota cache switches providers.
        fallback = any(
            r["provider"] == "claude" and r["payload"].get("error_kind") == "included_quota" for r in previous
        )
        for provider in ["codex"] if fallback else ["claude", "codex"]:
            key = self._availability_key(provider, w)
            rows = self.ledger.rows("provider_availability", "key=?", (key,))
            if not rows or rows[0]["state"] != "included_quota":
                return provider
            row = rows[0]
            if row["recheck_at"] > time.time():
                continue
            with self.ledger.transaction() as c:
                if c.execute(
                    "UPDATE ap_provider_availability SET probe_owner=?,recheck_at=? WHERE key=? AND recheck_at<=?",
                    (n["id"], time.time() + w["plan"]["policy"]["review_timeout"], key, time.time()),
                ).rowcount:
                    return provider  # exactly one actual recheck, never mark available by elapsed time
        self.ledger.transition(
            n["id"],
            n["state"],
            "waiting_provider",
            reason="Both providers report included quota unavailable; no paid fallback",
        )
        return None

    async def _review(self, n, provider):
        w = self.ledger.get("workflows", n["workflow_id"])
        candidate = self.ledger.get("candidates", n["candidate_id"])
        attempts = self.ledger.rows("reviews", "candidate_id=?", (candidate["id"],))
        record = self.ledger.record(
            "reviews",
            candidate_id=candidate["id"],
            provider=provider,
            generation=len(attempts) + 1,
            state="claimed",
            payload=dict(
                owner=self.owner,
                run_generation=w["generation"],
                fallback_reason="confirmed Claude included quota" if provider == "codex" else "",
            ),
        )
        event_dir = self.root / "logs" / record["id"]
        event_dir.mkdir(parents=True, exist_ok=True)
        record["event_log"] = str(event_dir / "events.jsonl")
        record["payload"]["redacted_event_log"] = record["event_log"]
        self.ledger.finish("reviews", record["id"], "claimed", record["payload"])
        root = await self._snapshot(candidate)
        evidence = self.ledger.rows("validation", "candidate_id=? AND state IN ('PASS','FAIL')", (candidate["id"],))
        # Provider prompt includes evidence; candidate itself remains immutable.
        findings = self._findings(n["id"])
        review_candidate = dict(
            candidate,
            payload=dict(
                candidate["payload"],
                validation=evidence[-1]["payload"] if evidence else {},
                unresolved=[f["id"] for f in findings],
                findings=findings,
                owner_decisions=[
                    d["payload"]
                    for d in self.ledger.rows("decisions", "node_id=? AND state='approved'", (n["id"],))
                    if d["payload"].get("spec_digest") == digest(n["spec"])
                    and d["payload"].get("run_generation") == w["generation"]
                    and d["payload"].get("candidate_id") in (None, candidate["id"])
                    and d["payload"].get("authorization") == decisions.authorization(w)
                    and (
                        d["payload"].get("purpose") == "manual_gates"
                        or d["payload"].get("decision_epoch") == n["decision_epoch"]
                    )
                ],
            ),
        )
        try:
            if not evidence:
                raise ProviderFailure("validation_missing", "managed validation not executed")
            # Codex reviewer shares the existing provider semaphore, never holds an implementation semaphore too.
            slots = self.manager._slots if provider == "codex" else None
            if slots:
                await slots.acquire()
            try:
                from .review_context import review_all

                result = await review_all(
                    self.providers[provider],
                    review_candidate,
                    w["plan"]["policy"],
                    root,
                    record,
                    lambda identity: self._process_record("reviews", record, identity),
                )
            finally:
                if slots:
                    slots.release()
            try:
                result = validate_result(
                    goals.terminal_fields(result, candidate),
                    candidate,
                ) | {
                    k: v
                    for k, v in result.items()
                    if k in ("provider", "model", "version", "session", "independence", "partition_evidence")
                }
            except (ValueError, KeyError, TypeError):
                raise ProviderFailure(
                    "invalid_output", "adapter returned an invalid terminal review contract"
                ) from None
            implementation = self.manager.db.get_task(n["task_id"])
            sessions = [result.get("session")] + [entry.get("session") for entry in result.get("partition_evidence", [])]
            if not result.get("session") or implementation["codex_thread_id"] in sessions:
                raise ProviderFailure("invalid_output", "reviewer must be independent of the implementation thread")
            await self._snapshot(candidate)  # Detect unexpected host-side snapshot changes as well as sandbox writes.
            result = json.loads(redact(canonical(result)))
            self.ledger.finish("reviews", record["id"], "completed", dict(record["payload"], **result))
        except ProviderFailure as e:
            payload = dict(
                record["payload"], error_kind=e.kind, reason=redact(str(e)), findings=e.findings, reset_at=e.reset_at
            )
            self.ledger.finish("reviews", record["id"], "incomplete", payload)
            self._retain_findings(candidate, e.findings)
            if e.kind == "included_quota":
                key = self._availability_key(provider, w)
                with self.ledger.transaction() as c:
                    c.execute(
                        "INSERT INTO ap_provider_availability VALUES(?,?, 'included_quota',?,NULL,?) ON CONFLICT(key) DO UPDATE SET state='included_quota',recheck_at=excluded.recheck_at,probe_owner=NULL,payload=excluded.payload",
                        (
                            key,
                            provider,
                            e.reset_at
                            if e.reset_at and e.reset_at > time.time()
                            else time.time() + w["plan"]["policy"]["provider_backoff"],
                            canonical(payload),
                        ),
                    )
                self.ledger.transition(n["id"], "reviewing", "review_queued", reason=payload["reason"])
            elif e.kind in ("transient", "invalid_output", "timeout", "max_turns", "context", "unknown"):
                failures = (
                    sum(
                        r["payload"].get("error_kind")
                        in ("transient", "invalid_output", "timeout", "max_turns", "context", "unknown")
                        for r in attempts
                    )
                    + 1
                )
                if failures <= w["plan"]["policy"]["infra_retries"]:
                    payload["retry_at"] = time.time() + w["plan"]["policy"]["provider_backoff"]
                    self.ledger.finish("reviews", record["id"], "incomplete", payload)
                    self.ledger.transition(n["id"], "reviewing", "review_queued", reason=payload["reason"])
                else:
                    self.ledger.transition(
                        n["id"],
                        "reviewing",
                        "blocked_infrastructure",
                        reason="Review retry budget exhausted: " + payload["reason"],
                    )
            else:
                state = "blocked_auth" if e.kind == "auth" else "blocked_configuration"
                self.ledger.transition(n["id"], "reviewing", state, reason=payload["reason"])
            return
        except Exception as e:
            payload = dict(record["payload"], error_kind="infrastructure", reason=redact(str(e)))
            self.ledger.finish("reviews", record["id"], "incomplete", payload)
            self.ledger.transition(n["id"], "reviewing", "blocked_infrastructure", reason=payload["reason"][:1000])
            return
        finally:
            self.processes.pop(record["id"], None)
        key = self._availability_key(provider, w)
        with self.ledger.transaction() as c:
            c.execute("DELETE FROM ap_provider_availability WHERE key=?", (key,))
        if result["verdict"] == "APPROVE":
            await self._accept(n, candidate, self.ledger.get("reviews", record["id"]))
        elif result["verdict"] == "CHANGES_REQUESTED":
            self._retain_findings(candidate, result["blocking"])
            if not replanning.queue(self, n, self.ledger.get("reviews", record["id"])):
                self._request_fix(n, digest(result["blocking"]), result["blocking"])
        elif replanning.eligible_causes(result) and replanning.queue(self, n, self.ledger.get("reviews", record["id"])):
            pass
        elif result["verdict"] == "NEEDS_HUMAN":
            self.ledger.transition(
                n["id"], "reviewing", "blocked_decision", reason="Independent reviewer requires owner decision"
            )
        else:
            self.ledger.transition(
                n["id"],
                "reviewing",
                "blocked_infrastructure",
                reason="Independent review inconclusive; scope/contract needs inspection",
            )

    def _retain_findings(self, candidate, findings):
        # Candidate scope remains immutable; unresolved findings live in review attempts, not candidate payload.
        # The next candidate's handoff aggregates this history below.
        return None

    def _findings(self, node_id):
        found = {}
        for r in self.ledger.rows(
            "reviews", "candidate_id IN (SELECT id FROM ap_candidates WHERE node_id=?)", (node_id,)
        ):
            for f in r["payload"].get("blocking", []) + r["payload"].get("findings", []):
                found[f["id"]] = f
            if r["state"] == "completed":
                for identity in r["payload"].get("resolved", []):
                    found.pop(identity, None)
        return list(found.values())

    async def _accept(self, n, candidate, review):
        task = self.manager.db.get_task(n["task_id"])
        if candidate["payload"].get("requirements"):
            fixed_inputs.verify(task["worktree"], candidate["payload"]["requirements"])
        current = await git.scope(task["worktree"], task["base_sha"], await git.sha(task["worktree"]))
        _, dirty, _ = await git.git(
            task["worktree"],
            "status",
            "--porcelain",
            "--",
            ".",
            ":(exclude).autopilot-inputs",
            ":(exclude).autopilot-fix",
        )
        w = self.ledger.get("workflows", n["workflow_id"])
        if (
            dirty
            or current != candidate["payload"]["scope"]
            or input_digest(n, w) != candidate["payload"]["input_digest"]
        ):
            self.ledger.transition(
                n["id"],
                "reviewing",
                "blocked_scope",
                reason="Candidate head/tree/inputs changed during review; stale approval rejected",
            )
            return
        if w["state"] == "paused":
            with self.ledger.transaction() as c:
                c.execute("UPDATE ap_nodes SET state='review_complete' WHERE id=? AND state='reviewing'", (n["id"],))
            return
        final_owner_required = (
            not w["plan"]["policy"]["automatic_acceptance"]
            or n["spec"]["contract"].get("manual_approval")
            or candidate["payload"]["contract"].get("manual_approval")
        )
        if final_owner_required and not decisions.candidate_approved(self.ledger, n, w):
            self.ledger.transition(
                n["id"],
                "reviewing",
                "blocked_decision",
                reason="Candidate owner acceptance required after validation and independent review",
            )
            return
        if not self.ledger.accept(candidate, review, current):
            self.ledger.transition(
                n["id"],
                "reviewing",
                "blocked_scope",
                reason="Acceptance gate rejected policy, contract, generation or unresolved findings",
            )

    async def _accept_saved(self, n):
        cand = self.ledger.get("candidates", n["candidate_id"])
        reviews = self.ledger.rows("reviews", "candidate_id=? AND state='completed'", (cand["id"],))
        await self._accept(n, cand, reviews[-1])

    def _request_fix(self, n, fingerprint, findings):
        w = self.ledger.get("workflows", n["workflow_id"])
        current = self.ledger.get("nodes", n["id"])
        if current["generation"] != n["generation"] or current["candidate_id"] != n["candidate_id"]:
            return
        progress = current["no_progress"] + 1 if current["last_failure"] == fingerprint else 1
        policy = w["plan"]["policy"]
        if progress >= policy["no_progress_limit"]:
            self.ledger.transition(
                n["id"],
                current["state"],
                "blocked_no_progress",
                reason="Repeated failure without progress",
                no_progress=progress,
            )
            return
        if current["fix_rounds"] >= policy["max_fix_rounds"]:
            self.ledger.transition(
                n["id"], current["state"], "blocked_budget", reason="Shared validation/review fix budget exhausted"
            )
            return
        self.ledger.transition(
            n["id"],
            current["state"],
            "fix_queued",
            reason=canonical(findings),
            last_failure=fingerprint,
            no_progress=progress,
        )

    async def _fix(self, n):
        from app.task_manager import _Turn

        task = self.manager.db.get_task(n["task_id"])
        from .environment import preflight

        await preflight(task["worktree"], n["spec"]["validation"])
        prompt = (
            "Autopilot correction for the existing work. Keep the existing worktree/thread. Resolve findings with evidence; retain the original acceptance contract.\n"
            + n["reason"]
            + "\nRe-read the unchanged fixed requirements and handoffs in .autopilot-inputs/requirements.json.\n"
        )
        if not task["codex_thread_id"]:
            reference = Path(task["worktree"]) / ".autopilot-fix" / f"{n['name']}-{n['fix_rounds'] + 1}.md"
            reference.parent.mkdir(parents=True, exist_ok=True)
            reference.write_text(
                n["spec"]["prompt"]
                + "\nContract: "
                + canonical(n["spec"]["contract"])
                + "\nPinned inputs: "
                + canonical(n["inputs"])
            )
            reference.chmod(0o444)
            prompt += (
                "\nNo recoverable implementation thread exists. Inspect existing Git state before correcting only these findings. Read immutable acceptance instructions: "
                + str(reference)
            )
        turn = _Turn(prompt, resume_thread=task["codex_thread_id"], service_tier="default", trigger="autopilot_fix")
        fields = self.manager._queue_fields(turn)
        op_id = uuid.uuid4().hex
        with self.ledger.transaction() as c:
            w = c.execute("SELECT state FROM ap_workflows WHERE id=?", (n["workflow_id"],)).fetchone()
            if w["state"] != "running":
                c.execute("UPDATE ap_nodes SET state='fix_queued' WHERE id=? AND state='reserving_fix'", (n["id"],))
                return
            cur = c.execute(
                "UPDATE tasks SET status='queued',"
                + ",".join(f"{k}=?" for k in fields)
                + " WHERE id=? AND status='completed' AND completion_pending=0 AND codex_thread_id IS ?",
                (*fields.values(), task["id"], task["codex_thread_id"]),
            )
            if cur.rowcount != 1:
                raise ValueError("original implementation turn is no longer idle")
            c.execute(
                "INSERT INTO ap_operations VALUES(?,?, 'fix',?,'reserved',?,?)",
                (
                    op_id,
                    n["id"],
                    n["fix_rounds"] + 1,
                    canonical(
                        dict(
                            thread=task["codex_thread_id"], prompt_digest=digest(prompt), candidate_id=n["candidate_id"]
                        )
                    ),
                    now_iso(),
                ),
            )
            conflict = n["spec"]["kind"] == "integration" and any(
                o["state"] == "conflict"
                for o in self.ledger.rows("operations", "node_id=? AND kind='integration'", (n["id"],))
            )
            c.execute(
                "UPDATE ap_nodes SET state=?,fix_rounds=fix_rounds+1 WHERE id=? AND state='reserving_fix'",
                ("conflict" if conflict else "fixing", n["id"]),
            )
            self.manager.db._insert_message(task["id"], turn.message())
        self.manager.db._notify(task["id"], "completed", "queued")

    async def _probe_implementation(self, n):
        w = self.ledger.get("workflows", n["workflow_id"])
        key = self._availability_key("codex", w)
        now = time.time()
        with self.ledger.transaction() as c:
            row = c.execute("SELECT * FROM ap_provider_availability WHERE key=?", (key,)).fetchone()
            if row and row["recheck_at"] > now:
                c.execute(
                    "UPDATE ap_nodes SET state='waiting_implementation_provider' WHERE id=? AND state='probing_implementation'",
                    (n["id"],),
                )
                return
            c.execute(
                "INSERT INTO ap_provider_availability VALUES(?,'codex','included_quota',?,?,?) ON CONFLICT(key) DO UPDATE SET recheck_at=excluded.recheck_at,probe_owner=excluded.probe_owner",
                (
                    key,
                    now + w["plan"]["policy"]["provider_backoff"],
                    n["id"],
                    canonical(dict(reason="implementation provider recheck")),
                ),
            )
        try:
            await self.providers["codex"].probe_capabilities_and_auth(w["plan"]["policy"])
            limits = await self.manager.read_limits("autopilot_quota_recheck", force_history=True)
            # A typed positive signal is required. Time passing alone never means available.
            if limits and limits.get("ordinary_usage_allowed") is True:
                task = self.manager.db.get_task(n["task_id"])
                turn = self.manager._turn_of(task, "autopilot_quota_resume")
                with self.ledger.transaction() as c:
                    if c.execute("SELECT state FROM ap_workflows WHERE id=?", (w["id"],)).fetchone()[0] != "running":
                        return
                    changed = c.execute(
                        "UPDATE tasks SET status='queued',pid=NULL,proc_identity=NULL,claimed_by=NULL,claimed_at=NULL,pending_turn=?,finished_at=NULL WHERE id=? AND status='waiting-for-quota'",
                        (turn.to_json(), task["id"]),
                    ).rowcount
                    if changed:
                        c.execute(
                            "UPDATE ap_nodes SET state='implementing',reason='' WHERE id=? AND state='probing_implementation'",
                            (n["id"],),
                        )
                        c.execute("DELETE FROM ap_provider_availability WHERE key=?", (key,))
                if changed:
                    self.manager.db._notify(task["id"], "waiting-for-quota", "queued")
                    return
        except ProviderFailure as e:
            self.ledger.transition(
                n["id"],
                "probing_implementation",
                "blocked_auth" if e.kind == "auth" else "blocked_configuration",
                reason=redact(str(e)),
            )
            return
        self.ledger.transition(n["id"], "probing_implementation", "waiting_implementation_provider")

    async def _finish_conflict(self, n):
        w = self.ledger.get("workflows", n["workflow_id"])
        task = self.manager.db.get_task(n["task_id"])
        if (
            not completion.parse_result(task["semantic_result"])
            or completion.parse_result(task["semantic_result"])["status"] != "SUCCESS"
        ):
            self.ledger.transition(
                n["id"], "integrating", "blocked_result", reason="Conflict resolver did not report complete resolution"
            )
            return
        _, conflicts, _ = await git.git(task["worktree"], "diff", "--name-only", "--diff-filter=U")
        if conflicts:
            self._request_fix(
                n, "integration-conflict", [dict(id="integration-conflict", path="", line=0, reason=conflicts)]
            )
            return
        op = self.ledger.rows("operations", "node_id=? AND kind='integration'", (n["id"],))[-1]
        await git.submit_head(task, n["spec"], w["plan"]["permissions"]["commit_owned_changes"])
        result = await git.integrate(task["worktree"], op["payload"].get("remaining", []), op["id"])
        result["steps"] = op["payload"].get("steps", []) + result["steps"]
        self.ledger.finish("operations", op["id"], result["state"], dict(op["payload"], **result))
        if result["state"] == "conflict":
            self._request_fix(
                n, "integration-conflict", [dict(id="integration-conflict", path="", line=0, reason=canonical(result))]
            )
        elif n["spec"].get("integration_mode") == "merge-then-implement":
            self._integration_turn(n, task, "integrating")
        else:
            self.ledger.transition(n["id"], "integrating", "submitting")
            await self._submit(self.ledger.get("nodes", n["id"]))

    def _integration_turn(self, n, task, old_state):
        from app.task_manager import _Turn

        prompt = (
            "All pinned inputs are now merged. Perform the requested integration implementation, including wiring and additional code. Read all fixed documents in .autopilot-inputs/requirements.json. "
            + n["spec"]["prompt"]
        )
        turn = _Turn(
            prompt, resume_thread=task["codex_thread_id"], service_tier="default", trigger="autopilot_integration"
        )
        fields = self.manager._queue_fields(turn)
        with self.ledger.transaction() as c:
            workflow = c.execute("SELECT state FROM ap_workflows WHERE id=?", (n["workflow_id"],)).fetchone()
            if workflow["state"] != "running":
                raise ValueError("integration implementation waits for an active workflow")
            c.execute(
                "INSERT INTO ap_operations VALUES(?,?,'integration_implementation',?,'reserved',?,?)",
                (
                    uuid.uuid4().hex,
                    n["id"],
                    n["generation"],
                    canonical(dict(thread=task["codex_thread_id"], prompt_digest=digest(prompt))),
                    now_iso(),
                ),
            )
            if (
                c.execute(
                    "UPDATE tasks SET status='queued',"
                    + ",".join(f"{k}=?" for k in fields)
                    + " WHERE id=? AND status='completed'",
                    (*fields.values(), task["id"]),
                ).rowcount
                != 1
            ):
                raise ValueError("integration implementation task is no longer idle")
            if (
                c.execute(
                    "UPDATE ap_nodes SET state='implementing' WHERE id=? AND state=?", (n["id"], old_state)
                ).rowcount
                != 1
            ):
                raise ValueError("integration generation changed")
        self.manager._dispatch(task["id"])

    async def _publish(self, n):
        w = self.ledger.get("workflows", n["workflow_id"])
        candidate = self.ledger.get("candidates", n["candidate_id"])
        target = "refs/heads/main"
        head = candidate["payload"]["scope"]["head_sha"]
        initial = w["plan"]["base_sha"]
        repo = w["plan"]["repository"]
        if n["spec"]["kind"] != "integration":
            self.ledger.transition(
                n["id"],
                "publishing",
                "blocked_configuration",
                reason="main publication requires a final independently reviewed integration node",
            )
            return
        current = await git.sha(repo, target)
        _, worktrees, _ = await git.git(repo, "worktree", "list", "--porcelain")
        if current != initial or "branch " + target in worktrees:
            self.ledger.transition(
                n["id"],
                "publishing",
                "blocked_decision",
                reason="Target advanced or has a live checkout; accepted candidate retained for safe publication",
            )
            return
        op = self.ledger.record(
            "operations",
            node_id=n["id"],
            kind="publish",
            generation=n["generation"],
            state="running",
            payload=dict(expected_ref=target, start_sha=initial, head_sha=head),
        )
        await git.git(repo, "update-ref", target, head, initial)
        self.ledger.finish("operations", op["id"], "completed", op["payload"])
        self.ledger.transition(n["id"], "publishing", "accepted")
        with self.ledger.transaction() as c:
            c.execute(
                "UPDATE ap_workflows SET state='completed',reason='Explicit main publication completed; no deploy or restart' WHERE id=? AND state='running'",
                (w["id"],),
            )

    def reconcile(self, workflow_id=None):
        nodes = self.ledger.rows(
            "nodes", "workflow_id=?" if workflow_id else "1", (workflow_id,) if workflow_id else ()
        )
        for n in nodes:
            if n["id"] in self.jobs:
                continue
            state = n["state"]
            if state == "replanning":
                records = self.ledger.rows("replans", "source_node=?", (n["id"],))
                if records and records[0]["state"] == "queued":
                    self.ledger.transition(n["id"], state, "replan_queued")
                else:
                    self.ledger.transition(n["id"], state, "blocked_replan", reason="Replanner interrupted; saved proposal/identity requires inspection; no duplicate session")
            elif state in ("launching", "integrating", "publishing", "reserving_fix", "submitting"):
                task = self.manager.db.get_task(n["task_id"]) if n["task_id"] else None
                operations = self.ledger.rows("operations", "node_id=? AND kind='integration'", (n["id"],))
                if state in ("launching", "integrating") and operations and task:
                    self._spawn(n, state, "reconciling_git", self._reconcile_git)
                    continue
                if state == "submitting" and task and task["status"] == "completed":
                    self.ledger.transition(n["id"], state, "implementing")
                else:
                    self.ledger.transition(
                        n["id"],
                        state,
                        "reconciling",
                        reason="External operation result uncertain; inspect Git/operation ledger before retry",
                    )
            elif state in ("reviewing", "validating"):
                table = "reviews" if state == "reviewing" else "validation"
                records = self.ledger.rows(table, "candidate_id=?", (n["candidate_id"],))
                complete = [r for r in records if r["state"] in ("completed", "PASS", "FAIL")]
                active = [r for r in records if r["state"] in ("running", "claimed")]
                if active:
                    for r in active:
                        identity = r["payload"].get("identity", {})
                        alive = same_process(identity.get("pid"), identity.get("proc_identity"), needle="")
                        self.ledger.finish(
                            table,
                            r["id"],
                            "uncertain",
                            dict(
                                r["payload"],
                                reconciliation="process_alive" if alive else "process_dead_or_identity_unknown",
                            ),
                        )
                    self.ledger.transition(
                        n["id"],
                        state,
                        "reconciling",
                        reason="Review/validator outcome unknown; no duplicate launch or stale acceptance",
                    )
                elif complete:
                    latest = complete[-1]
                    if state == "validating" and latest["state"] == "PASS":
                        self.ledger.transition(n["id"], state, "review_queued")
                    elif state == "reviewing" and latest["payload"].get("verdict") == "APPROVE":
                        self.ledger.transition(n["id"], state, "review_complete")
                    elif state == "reviewing" and latest["payload"].get("verdict") == "CHANGES_REQUESTED":
                        if not replanning.queue(self, n, latest):
                            self._request_fix(
                                n, digest(latest["payload"].get("blocking", [])), latest["payload"].get("blocking", [])
                            )
                    elif state == "reviewing" and latest["payload"].get("verdict") == "NEEDS_HUMAN":
                        self.ledger.transition(
                            n["id"], state, "blocked_decision", reason="Recovered reviewer requires owner decision"
                        )
                    elif state == "validating" and latest["state"] == "FAIL":
                        findings = [
                            dict(id="validation-" + str(i), path="", line=0, reason=x.get("excerpt") or canonical(x))
                            for i, x in enumerate(latest["payload"].get("checks", []))
                            if x.get("exit_status") != 0
                        ]
                        self._request_fix(n, digest(findings), findings)
                    else:
                        self.ledger.transition(
                            n["id"], state, "reconciling", reason="Persisted non-approval requires inspected recovery"
                        )
                else:
                    self.ledger.transition(
                        n["id"], state, "reconciling", reason="Claimed operation has no durable result"
                    )

    async def _reconcile_git(self, node):
        """Inspect actual refs/index before completing a recorded integration; never rerun merge."""
        workflow = self.ledger.get("workflows", node["workflow_id"])
        task = self.manager.db.get_task(node["task_id"])
        operation = self.ledger.rows("operations", "node_id=? AND kind='integration'", (node["id"],))[-1]
        payload = operation["payload"]
        result = completion.parse_result(task["semantic_result"])
        if task["status"] == "stopped" or (task["codex_thread_id"] and (not result or result["status"] != "SUCCESS")):
            self.ledger.transition(
                node["id"],
                "reconciling_git",
                "blocked_result",
                reason="Explicit stop or incomplete integration turn cannot be promoted by reconciliation",
            )
            return
        _, branch, _ = await git.git(task["worktree"], "symbolic-ref", "-q", "HEAD", check=False)
        head = await git.sha(task["worktree"])
        _, dirty, _ = await git.git(
            task["worktree"],
            "status",
            "--porcelain",
            "--",
            ".",
            ":(exclude).autopilot-inputs",
            ":(exclude).autopilot-fix",
        )
        merge_pending, _, _ = await git.git(task["worktree"], "rev-parse", "--verify", "MERGE_HEAD", check=False)
        contained = True
        for item in payload["inputs"]:
            if item["kind"] != "report":
                rc, _, _ = await git.git(
                    task["worktree"], "merge-base", "--is-ancestor", item["head_sha"], head, check=False
                )
                contained = contained and rc == 0
        if (
            branch.strip() != payload["expected_ref"]
            or dirty
            or merge_pending == 0
            or not contained
            or (payload.get("head_sha") and head != payload["head_sha"])
        ):
            self.ledger.transition(
                node["id"],
                "reconciling_git",
                "reconciling",
                reason="Integration ref/index/input reconciliation is uncertain; retained without another merge",
            )
            return
        identity = task.get("proc_identity")
        if task.get("pid") and same_process(task["pid"], identity):
            self.ledger.transition(
                node["id"],
                "reconciling_git",
                "reconciling",
                reason="Integration implementation process remains alive; no duplicate launch",
            )
            return
        self.ledger.finish(
            "operations",
            operation["id"],
            "merged",
            dict(payload, head_sha=head, reconciliation="refs_and_all_pinned_inputs_confirmed"),
        )
        if node["spec"].get("integration_mode") == "merge-then-implement":
            if task["status"] == "queued" and not task["codex_thread_id"] and not task["started_at"]:
                self.ledger.transition(
                    node["id"],
                    "reconciling_git",
                    "implementing",
                    reason="Durable merge reconciled; initial integration implementation still required",
                )
                self.manager._dispatch(task["id"])
            elif task["status"] == "completed" and result and result["status"] == "SUCCESS":
                turns = self.ledger.rows("operations", "node_id=? AND kind='integration_implementation'", (node["id"],))
                if digest(task["last_prompt"]) == payload.get("initial_implementation_prompt") or turns:
                    self.ledger.transition(node["id"], "reconciling_git", "implementing")
                else:
                    self._integration_turn(node, task, "reconciling_git")
            else:
                self.ledger.transition(
                    node["id"],
                    "reconciling_git",
                    "reconciling",
                    reason="Integration implementation outcome uncertain; merge cannot substitute for the turn",
                )
            return
        with self.ledger.transaction() as c:
            state = c.execute("SELECT state,generation FROM ap_workflows WHERE id=?", (workflow["id"],)).fetchone()
            if state["state"] not in ("running", "paused") or state["generation"] != workflow["generation"]:
                return
            c.execute(
                "UPDATE tasks SET status='completed',task_outcome='needs_review',completion_pending=0,pid=NULL,proc_identity=NULL,claimed_by=NULL,claimed_at=NULL,semantic_result=?,finished_at=? WHERE id=?",
                (
                    canonical(
                        dict(
                            status="SUCCESS",
                            reason="Recorded integration reconciled against refs and every pinned input; validation/review pending",
                        )
                    ),
                    now_iso(),
                    task["id"],
                ),
            )
            c.execute(
                "UPDATE ap_nodes SET state='implementing',reason='' WHERE id=? AND state='reconciling_git'",
                (node["id"],),
            )

    async def shutdown(self):
        self.closed = True
        for job in self.jobs.values():
            job.cancel()
        await asyncio.gather(*list(self.jobs.values()), return_exceptions=True)
