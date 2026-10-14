"""Small opt-in state machine driven by TaskManager.tick, with durable conditional claims."""
import asyncio
import json
import logging
import os
from pathlib import Path
import signal
import time
import uuid

from app import completion
from app.models import now_iso, make_task_id, ACTIVE_STATUSES
from app.procinfo import process_identity, same_process
from . import git
from .ledger import Ledger, unpack
from .policy import canonical, digest, manifest
from .providers import ClaudeProvider, CodexProvider, ProviderFailure, validate_result
from .runner import Validator, InfrastructureUnavailable, redact

log = logging.getLogger(__name__)
BLOCKED = {"blocked_decision", "blocked_infrastructure", "blocked_configuration", "blocked_auth", "blocked_no_progress", "blocked_budget", "blocked_scope", "blocked_result", "blocked_dependencies", "reconciling", "stopped"}


class Autopilot:
    def __init__(self, manager, *, providers=None, validator=None):
        self.manager = manager
        self.ledger = Ledger(manager.db)
        self.providers = providers or dict(claude=ClaudeProvider(),codex=CodexProvider(manager.settings.codex_bin))
        self.validator = validator or Validator()
        self.root = manager.settings.home/"autopilot"
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
        plan["base_sha"] = await git.sha(repo,plan["base_ref"])
        for n in plan["nodes"]:
            if n.get("task_id"):
                task = self.manager.db.get_task(n["task_id"])
                if not task or task["repository"] != str(repo):
                    raise ValueError("backlog task repository mismatch")
                if json.loads(task["completion_contract"]) != n["contract"]:
                    raise ValueError("backlog opt-in requires the unchanged task contract, including manual gates")
                if task["status"] != "completed" or task["task_outcome"] != "needs_review" or task["completion_pending"]:
                    raise ValueError("backlog task is not idle needs_review")
                if await git.changed_paths(task["worktree"], task["base_sha"]):
                    # Existing dirty worktrees may include unrelated owner edits. Only precommitted backlogs are safe.
                    _, status, _ = await git.git(task["worktree"],"status","--porcelain")
                    if status:
                        raise ValueError("commit and inspect backlog changes before opt-in")
        warnings = []
        if not plan["policy"]["extra_usage_disabled_confirmed"]:
            warnings.append("owner billing confirmation missing; real providers will not start")
        for n in plan["nodes"]:
            if n["manual_gates"] or n["contract"].get("manual_approval"):
                warnings.append(f"{n['id']}: explicit owner decision required")
        return dict(plan=plan,input_digest=digest(plan),warnings=warnings,execution_order=[n["id"] for n in plan["nodes"]],
                    review_provider="Claude Code; only confirmed included quota permits independent Codex fallback",
                    target="main (explicit permission)" if plan["permissions"]["publish_main"] else "workflow-owned integration ref",
                    isolation="managed validators only; agent-issued shell builds are not OS concurrency limited")

    async def register(self,raw,expected_digest):
        preview = await self.dry_run(raw)
        if preview["input_digest"] != expected_digest:
            raise ValueError("dry-run digest changed; inspect the new plan before Start")
        return self.ledger.register(preview["plan"])

    async def control(self,workflow_id,action):
        if action not in ("start", "pause", "resume", "stop", "reconcile"):
            raise ValueError("invalid workflow action")
        w = self.ledger.get("workflows",workflow_id)
        if not w:
            raise ValueError("workflow not found")
        if action == "reconcile":
            self.reconcile(workflow_id)
            return self.ledger.view(workflow_id)
        with self.ledger.transaction() as c:
            if action == "stop":
                c.execute("UPDATE ap_workflows SET state='stopped',generation=generation+1,reason='Owner Stop; automatic continuation disabled' WHERE id=?",(workflow_id,))
                c.execute("UPDATE ap_nodes SET state='stopped',reason='Owner Stop' WHERE workflow_id=? AND state!='accepted'",(workflow_id,))
            elif action == "pause":
                c.execute("UPDATE ap_workflows SET state='paused',reason='New dispatch paused; running read-only reviews and validation may finish; acceptance waits for Resume' WHERE id=? AND state='running'",(workflow_id,))
            elif action in ("start", "resume"):
                allowed = ("draft",) if action == "start" else ("paused", "waiting", "running")
                if w["state"] not in allowed:
                    raise ValueError("stopped workflows cannot resume; register a reviewed new plan")
                c.execute("UPDATE ap_workflows SET state='running',reason='' WHERE id=?",(workflow_id,))
        if action == "stop":
            for node in self.ledger.rows("nodes","workflow_id=?",(workflow_id,)):
                job = self.jobs.get(node["id"])
                if job:
                    job.cancel()
                task = self.manager.db.get_task(node["task_id"]) if node["task_id"] else None
                if task and task["status"] in ACTIVE_STATUSES:
                    await self.manager.stop(task["id"])
                for r in self.ledger.rows("reviews","candidate_id IN (SELECT id FROM ap_candidates WHERE node_id=?) AND state IN ('claimed','running')",(node["id"],)):
                    await self.providers[r["provider"]].cancel(r["id"])
            await asyncio.gather(*(j for nid,j in list(self.jobs.items()) if nid.startswith(workflow_id+":")),return_exceptions=True)
        self.manager.scheduler.wake()
        return self.ledger.view(workflow_id)

    def can_dispatch(self,task_id):
        n = self.ledger.node_for_task(task_id)
        if not n:
            return True
        w = self.ledger.get("workflows",n["workflow_id"])
        if w["state"] != "running" or n["state"] not in ("implementing", "fixing", "conflict"):
            return False
        own = [x for x in self.ledger.rows("nodes","workflow_id=?",(w["id"],)) if x["task_id"] in self.manager._jobs]
        # Reuse _slots; no second semaphore is held while an implementation waits for it.
        global_own = [x for x in self.ledger.rows("nodes") if x["task_id"] in self.manager._jobs]
        return (len(own) < w["plan"]["policy"]["implementation_slots"] and
                len(global_own) < self.manager.settings.autopilot_implementation_slots)

    def _spawn(self,n,old,new,work):
        if self.ledger.get("workflows",n["workflow_id"])["state"] != "running":
            return
        if n["id"] in self.jobs or not self.ledger.transition(n["id"],old,new):
            return
        job = asyncio.create_task(self._guarded(n["id"],work))
        self.jobs[n["id"]] = job
        def done(j):
            if self.jobs.get(n["id"]) is j:
                self.jobs.pop(n["id"],None)
            self.manager.scheduler.wake()
        job.add_done_callback(done)

    async def _guarded(self,nid,work):
        try:
            await work(self.ledger.get("nodes",nid))
        except asyncio.CancelledError:
            # Durable pending operations are reconciled, never blindly relaunched.
            raise
        except (InfrastructureUnavailable,FileNotFoundError) as e:
            n = self.ledger.get("nodes",nid)
            self.ledger.transition(nid,n["state"],"blocked_infrastructure",reason=redact(str(e))[:1000])
        except Exception as e:
            log.exception("Autopilot operation failed for %s",nid)
            n = self.ledger.get("nodes",nid)
            self.ledger.transition(nid,n["state"],"blocked_configuration",reason=redact(str(e))[:1000])

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
        for w in self.ledger.rows("workflows","state='running'"):
            nodes = self.ledger.rows("nodes","workflow_id=?",(w["id"],))
            policy = w["plan"]["policy"]
            if all(n["state"] == "accepted" for n in nodes):
                if w["plan"]["permissions"]["publish_main"]:
                    if not any(n["id"] in self.jobs for n in nodes):
                        # Publication is an explicit operation, never deployment or live checkout modification.
                        self._spawn(nodes[-1],"accepted","publishing",self._publish)
                else:
                    with self.ledger.transaction() as c:
                        c.execute("UPDATE ap_workflows SET state='completed',reason='All candidates independently accepted; not deployed' WHERE id=? AND state='running'",(w["id"],))
                continue
            by_name = {n["name"]:n for n in nodes}
            all_nodes = self.ledger.rows("nodes")
            reviews = sum(n["state"] == "reviewing" and n["id"] in self.jobs for n in all_nodes)
            validators = sum(n["state"] == "validating" and n["id"] in self.jobs for n in all_nodes)
            integrations = sum(n["state"] in ("launching","integrating") and n["spec"]["kind"] == "integration" for n in all_nodes)
            for n in nodes:
                if n["id"] in self.jobs or n["state"] in BLOCKED:
                    continue
                state = n["state"]
                if state == "pending":
                    parents = [by_name[p] for p in n["spec"]["depends_on"]]
                    if any(p["state"] in BLOCKED for p in parents):
                        self.ledger.transition(n["id"],state,"blocked_dependencies",reason="A required accepted input is unavailable")
                        continue
                    if not all(p["state"] == "accepted" for p in parents):
                        continue
                    if n["spec"]["manual_gates"] or n["spec"]["contract"].get("manual_approval"):
                        self.ledger.transition(n["id"],state,"blocked_decision",reason="Required owner gate: " + ", ".join(n["spec"]["manual_gates"] or ["manual approval"]))
                        continue
                    if n["spec"]["kind"] == "integration" and integrations >= min(policy["integration_slots"],self.manager.settings.autopilot_integration_slots):
                        continue
                    self._spawn(n,state,"launching",self._launch)
                    if n["spec"]["kind"] == "integration":
                        integrations += 1
                elif state in ("implementing", "fixing", "conflict"):
                    task = self.manager.db.get_task(n["task_id"])
                    if task and task["status"] == "completed" and not task["completion_pending"] and n["task_id"] not in self.manager._jobs:
                        if state == "conflict":
                            self._spawn(n,state,"integrating",self._finish_conflict)
                        else:
                            self._spawn(n,state,"submitting",self._submit)
                    elif task and task["status"] in ("failed","stopped","blocked","waiting-for-quota","interrupted"):
                        self.ledger.transition(n["id"],state,"blocked_result",reason="Implementation stopped: " + task["status"])
                elif state == "submitted" and validators < min(policy["validator_slots"],self.manager.settings.autopilot_validator_slots):
                    self._spawn(n,state,"validating",self._validate)
                    validators += 1
                elif state in ("review_queued","waiting_provider") and reviews < min(policy["review_slots"],self.manager.settings.autopilot_review_slots):
                    provider = self._select_provider(n,w)
                    if provider:
                        self._spawn(n,state,"reviewing",lambda node,p=provider:self._review(node,p))
                        reviews += 1
                elif state == "fix_queued":
                    self._spawn(n,state,"reserving_fix",self._fix)
                elif state == "review_complete":
                    self._spawn(n,state,"reviewing",self._accept_saved)
            # Waiting provider rows occupy no implementation, CPU or validator slot.

    def _inputs(self,n):
        result = []
        for edge in self.ledger.rows("edges","node_id=?",(n["id"],)):
            parent = self.ledger.get("nodes",edge["parent_id"])
            candidate = self.ledger.get("candidates",parent["candidate_id"])
            if parent["state"] != "accepted" or candidate["state"] != "accepted":
                raise ValueError("input candidate is not accepted")
            result.append(dict(node=parent["name"],candidate_id=candidate["id"],head_sha=candidate["payload"]["scope"]["head_sha"],kind=parent["spec"]["kind"]))
        return sorted(result,key=lambda p:p["node"])

    async def _launch(self,n):
        w = self.ledger.get("workflows",n["workflow_id"])
        if w["state"] == "paused":
            self.ledger.transition(n["id"],"launching","pending")
            return
        inputs = self._inputs(n)
        code = [p["head_sha"] for p in inputs if p["kind"] != "report"]
        base = w["plan"]["base_sha"] if n["spec"]["kind"] == "integration" or not code else code[0]
        if n["task_id"]:
            task = self.manager.db.get_task(n["task_id"])
            self.ledger.transition(n["id"],"launching","implementing",inputs=inputs)
            return
        task_id = make_task_id()
        # Persist the intended task before worktree/process side effects. Crash = reconcile, not a new task.
        self.ledger.transition(n["id"],"launching","launching",task_id=task_id,inputs=inputs)
        bundle = self.root/w["id"]/"inputs"
        bundle.mkdir(parents=True,exist_ok=True)
        for name,text in w["plan"]["inputs"].items():
            p = bundle/name
            p.parent.mkdir(parents=True,exist_ok=True)
            if not p.exists():
                p.write_text(text)
                p.chmod(0o444)
        prompt = n["spec"]["prompt"] + "\nEdit ownership: " + canonical(n["spec"]["paths"]) + "\nPinned accepted inputs: " + canonical(inputs)
        if n["spec"]["input_files"]:
            prompt += "\nRead immutable input bundle: " + canonical([str(bundle/p) for p in n["spec"]["input_files"]])
        task = await self.manager.create_task(repository=w["plan"]["repository"],base_ref=base,name=f"Autopilot {w['name'] if 'name' in w else w['plan']['name']}: {n['name']}",prompt=prompt,
                        completion_contract=n["spec"]["contract"],auto_retry=False,service_tier="default",_defer_dispatch=True,_task_id=task_id)
        if n["spec"]["kind"] == "integration":
            op = self.ledger.record("operations",node_id=n["id"],kind="integration",generation=n["generation"],state="running",payload=dict(inputs=inputs,start_sha=base,task_id=task_id,expected_ref="refs/heads/"+task["branch"]))
            result = await git.integrate(task["worktree"],code,op["id"])
            self.ledger.finish("operations",op["id"],result["state"],dict(op["payload"],**result))
            if result["state"] == "conflict":
                self.ledger.transition(n["id"],"launching","conflict",reason=canonical(result["conflicts"]))
                self.manager._dispatch(task_id)
                return
            # A mechanical merge is only a submission, never acceptance.
            self.manager.db.set_status(task_id,"starting")
            self.manager.db.set_status(task_id,"running")
            self.manager.db.set_status(task_id,"completed",semantic_result=canonical(dict(status="SUCCESS",reason="Pinned accepted inputs mechanically integrated; validation and review pending")),task_outcome="needs_review",finished_at=now_iso())
        self.ledger.transition(n["id"],"launching","implementing")
        self.manager._dispatch(task_id)

    async def _submit(self,n):
        w = self.ledger.get("workflows",n["workflow_id"])
        task = self.manager.db.get_task(n["task_id"])
        result = completion.parse_result(task["semantic_result"])
        if not result:
            self._request_fix(n,"structured_result_missing",[dict(id="completion-result",reason="Return the required complete completion JSON for the existing work; inspect artifacts first",path="",line=0)])
            return
        if result["status"] != "SUCCESS":
            self.ledger.transition(n["id"],n["state"],"blocked_result",reason=f"Implementation reported {result['status']}: {result['reason']}")
            return
        head = await git.submit_head(task,n["spec"],w["plan"]["permissions"]["commit_owned_changes"])
        s = await git.scope(task["worktree"],task["base_sha"],head)
        previous = self.ledger.get("candidates",n["candidate_id"]) if n["candidate_id"] else None
        if previous and previous["payload"]["scope"]["tree_sha"] == s["tree_sha"]:
            self._request_fix(n,"unchanged_tree",previous["payload"].get("findings",[]))
            return
        inherited = self._findings(n["id"])
        payload = dict(scope=s,task_id=task["id"],turn=len(self.manager.db.list_turns(task["id"])),execution_key=digest([task["last_turn_at"],task["semantic_result"]]),repository=task["repository"],
                       contract=json.loads(task["completion_contract"]),contract_digest=digest(json.loads(task["completion_contract"])),
                       policy_digest=digest(w["plan"]["policy"]),input_digest=digest(n["inputs"]),profile_digest=digest(n["spec"]["validation"]),
                       acceptance=n["spec"]["prompt"],inputs=n["inputs"],changed_paths=sorted(await git.changed_paths(task["worktree"],task["base_sha"])),
                       unresolved=[f["id"] for f in inherited],findings=inherited)
        self.ledger.submit(n,w,payload)

    async def _snapshot(self,candidate):
        p = candidate["payload"]
        root = await git.snapshot(p["repository"],p["scope"]["head_sha"],p["scope"]["base_sha"],self.root/"snapshots"/candidate["id"])
        _, dirty, _ = await git.git(root,"status","--porcelain")
        if dirty or await git.scope(root,p["scope"]["base_sha"],p["scope"]["head_sha"]) != p["scope"]:
            raise ValueError("snapshot modified; fixed source evidence invalid")
        return root

    def queue_priority(self,task):
        node = self.ledger.node_for_task(task["id"])
        from datetime import datetime,timezone
        try:
            age = (datetime.now(timezone.utc)-datetime.fromisoformat(task["created_at"].replace("Z","+00:00"))).total_seconds()
        except ValueError:
            age = 0
        # Eventually an old ordinary task wins over a continuous stream of repairs.
        return (0 if age >= 300 else 1 if node and (node["state"] in ("fixing","conflict") or node["spec"]["kind"] == "integration") else 2,task["created_at"])

    def _process_record(self,table,record,identity):
        pid = identity.get("pid")
        identity["proc_identity"] = process_identity(pid)
        payload = dict(record["payload"],identity=identity,owner=self.owner)
        self.ledger.finish(table,record["id"],"running",payload)
        record["payload"] = payload
        if pid:
            self.processes[record["id"]] = identity

    async def _validate(self,n):
        candidate = self.ledger.get("candidates",n["candidate_id"])
        root = await self._snapshot(candidate)
        record = self.ledger.record("validation",candidate_id=candidate["id"],state="running",payload=dict(owner=self.owner,scope=candidate["payload"]["scope"]))
        try:
            result = await self.validator.validate(candidate,root,n["spec"]["validation"],self.root/"logs"/record["id"],
                          on_start=lambda p:self._process_record("validation",record,dict(pid=p.pid)))
        except InfrastructureUnavailable as e:
            self.ledger.finish("validation",record["id"],"UNAVAILABLE",dict(record["payload"],reason=redact(str(e))))
            raise
        finally:
            self.processes.pop(record["id"],None)
        # Controller evidence records must match the registered profile and tree.
        if result["profile_digest"] != candidate["payload"]["profile_digest"] or result["scope"] != candidate["payload"]["scope"]:
            raise ValueError("validator evidence identity mismatch")
        contract = candidate["payload"]["contract"]
        for path in contract.get("required_paths",[]):
            p = root/path
            result["checks"].append(dict(artifact=path,exit_status=0 if p.exists() and not p.is_symlink() and p.resolve().is_relative_to(root.resolve()) else 1))
        changed = candidate["payload"]["changed_paths"]
        for path in contract.get("required_changed_paths",[]):
            result["checks"].append(dict(required_change=path,exit_status=0 if path in changed else 1))
        if contract.get("require_any_change"):
            result["checks"].append(dict(required_change="any",exit_status=0 if changed else 1))
        if contract.get("require_commit"):
            s = candidate["payload"]["scope"]
            result["checks"].append(dict(required_commit=True,exit_status=0 if s["base_sha"] != s["head_sha"] else 1))
        if any(c["exit_status"] != 0 for c in result["checks"]):
            result["state"] = "FAIL"
        self.ledger.finish("validation",record["id"],result["state"],result)
        if result["state"] == "PASS":
            self.ledger.transition(n["id"],"validating","review_queued")
        else:
            findings = [dict(id="validation-"+str(i),path="",line=0,reason=c.get("excerpt") or f"Required validation failed: {c}") for i,c in enumerate(result["checks"]) if c["exit_status"] != 0 or c.get("timed_out")]
            self._request_fix(n,digest(findings),findings)

    def _availability_key(self,provider,w):
        p = w["plan"]["policy"]
        return digest([provider,p["account"],p["bucket"],p[provider+"_model"]])

    def _select_provider(self,n,w):
        previous = self.ledger.rows("reviews","candidate_id=?",(n["candidate_id"],))
        if previous and previous[-1]["payload"].get("retry_at",0) > time.time():
            return None
        # Only a quota event or the persisted confirmed quota cache switches providers.
        fallback = any(r["provider"] == "claude" and r["payload"].get("error_kind") == "included_quota" for r in previous)
        for provider in (["codex"] if fallback else ["claude","codex"]):
            key = self._availability_key(provider,w)
            rows = self.ledger.rows("provider_availability","key=?",(key,))
            if not rows or rows[0]["state"] != "included_quota":
                return provider
            row = rows[0]
            if row["recheck_at"] > time.time():
                continue
            with self.ledger.transaction() as c:
                if c.execute("UPDATE ap_provider_availability SET probe_owner=?,recheck_at=? WHERE key=? AND recheck_at<=?",
                             (n["id"],time.time()+w["plan"]["policy"]["review_timeout"],key,time.time())).rowcount:
                    return provider  # exactly one actual recheck, never mark available by elapsed time
        self.ledger.transition(n["id"],n["state"],"waiting_provider",reason="Both providers report included quota unavailable; no paid fallback")
        return None

    async def _review(self,n,provider):
        w = self.ledger.get("workflows",n["workflow_id"])
        candidate = self.ledger.get("candidates",n["candidate_id"])
        attempts = self.ledger.rows("reviews","candidate_id=?",(candidate["id"],))
        record = self.ledger.record("reviews",candidate_id=candidate["id"],provider=provider,generation=len(attempts)+1,state="claimed",
                     payload=dict(owner=self.owner,run_generation=w["generation"],fallback_reason="confirmed Claude included quota" if provider == "codex" else ""))
        root = await self._snapshot(candidate)
        evidence = self.ledger.rows("validation","candidate_id=? AND state='PASS'",(candidate["id"],))
        # Provider prompt includes evidence; candidate itself remains immutable.
        findings = self._findings(n["id"])
        review_candidate = dict(candidate,payload=dict(candidate["payload"],validation=evidence[-1]["payload"] if evidence else {},
                                   unresolved=[f["id"] for f in findings],findings=findings))
        try:
            if not evidence:
                raise ProviderFailure("validation_missing","managed validation not executed")
            # Codex reviewer shares the existing provider semaphore, never holds an implementation semaphore too.
            slots = self.manager._slots if provider == "codex" else None
            if slots:
                await slots.acquire()
            try:
                result = await self.providers[provider].start_review(review_candidate,w["plan"]["policy"],root,record,
                                      lambda identity:self._process_record("reviews",record,identity))
            finally:
                if slots:
                    slots.release()
            result = validate_result({k:result[k] for k in ("candidate_id","run_generation","scope","verdict","blocking","nonblocking","resolved","coverage","unverified")},candidate) | {k:v for k,v in result.items() if k in ("provider","model","version","session","independence")}
            self.ledger.finish("reviews",record["id"],"completed",dict(record["payload"],**result))
        except ProviderFailure as e:
            payload = dict(record["payload"],error_kind=e.kind,reason=redact(str(e)),findings=e.findings,reset_at=e.reset_at)
            self.ledger.finish("reviews",record["id"],"incomplete",payload)
            self._retain_findings(candidate,e.findings)
            if e.kind == "included_quota":
                key = self._availability_key(provider,w)
                with self.ledger.transaction() as c:
                    c.execute("INSERT INTO ap_provider_availability VALUES(?,?, 'included_quota',?,NULL,?) ON CONFLICT(key) DO UPDATE SET state='included_quota',recheck_at=excluded.recheck_at,probe_owner=NULL,payload=excluded.payload",
                              (key,provider,e.reset_at if e.reset_at and e.reset_at>time.time() else time.time()+w["plan"]["policy"]["provider_backoff"],canonical(payload)))
                self.ledger.transition(n["id"],"reviewing","review_queued",reason=payload["reason"])
            elif e.kind in ("transient","invalid_output","timeout","max_turns","context","unknown"):
                failures = sum(r["payload"].get("error_kind") in ("transient","invalid_output","timeout","max_turns","context","unknown") for r in attempts)+1
                if failures <= w["plan"]["policy"]["infra_retries"]:
                    payload["retry_at"] = time.time()+w["plan"]["policy"]["provider_backoff"]
                    self.ledger.finish("reviews",record["id"],"incomplete",payload)
                    self.ledger.transition(n["id"],"reviewing","review_queued",reason=payload["reason"])
                else:
                    self.ledger.transition(n["id"],"reviewing","blocked_infrastructure",reason="Review retry budget exhausted: "+payload["reason"])
            else:
                state = "blocked_auth" if e.kind == "auth" else "blocked_configuration"
                self.ledger.transition(n["id"],"reviewing",state,reason=payload["reason"])
            return
        finally:
            self.processes.pop(record["id"],None)
        key = self._availability_key(provider,w)
        with self.ledger.transaction() as c:
            c.execute("DELETE FROM ap_provider_availability WHERE key=?",(key,))
        if result["verdict"] == "APPROVE":
            await self._accept(n,candidate,self.ledger.get("reviews",record["id"]))
        elif result["verdict"] == "CHANGES_REQUESTED":
            self._retain_findings(candidate,result["blocking"])
            self._request_fix(n,digest(result["blocking"]),result["blocking"])
        elif result["verdict"] == "NEEDS_HUMAN":
            self.ledger.transition(n["id"],"reviewing","blocked_decision",reason="Independent reviewer requires owner decision")
        else:
            self.ledger.transition(n["id"],"reviewing","blocked_infrastructure",reason="Independent review inconclusive; scope/contract needs inspection")

    def _retain_findings(self,candidate,findings):
        # Candidate scope remains immutable; unresolved findings live in review attempts, not candidate payload.
        # The next candidate's handoff aggregates this history below.
        return None

    def _findings(self,node_id):
        found = {}
        for r in self.ledger.rows("reviews","candidate_id IN (SELECT id FROM ap_candidates WHERE node_id=?)",(node_id,)):
            for f in r["payload"].get("blocking",[])+r["payload"].get("findings",[]):
                found[f["id"]] = f
            if r["state"] == "completed":
                for identity in r["payload"].get("resolved",[]):
                    found.pop(identity,None)
        return list(found.values())

    async def _accept(self,n,candidate,review):
        task = self.manager.db.get_task(n["task_id"])
        current = await git.scope(task["worktree"],task["base_sha"],await git.sha(task["worktree"]))
        _, dirty, _ = await git.git(task["worktree"],"status","--porcelain")
        w = self.ledger.get("workflows",n["workflow_id"])
        if dirty or current != candidate["payload"]["scope"] or digest(n["inputs"]) != candidate["payload"]["input_digest"]:
            self.ledger.transition(n["id"],"reviewing","blocked_scope",reason="Candidate head/tree/inputs changed during review; stale approval rejected")
            return
        if w["state"] == "paused":
            with self.ledger.transaction() as c:
                c.execute("UPDATE ap_nodes SET state='review_complete' WHERE id=? AND state='reviewing'",(n["id"],))
            return
        if not self.ledger.accept(candidate,review,current):
            self.ledger.transition(n["id"],"reviewing","blocked_scope",reason="Acceptance gate rejected policy, contract, generation or unresolved findings")

    async def _accept_saved(self,n):
        cand = self.ledger.get("candidates",n["candidate_id"])
        reviews = self.ledger.rows("reviews","candidate_id=? AND state='completed'",(cand["id"],))
        await self._accept(n,cand,reviews[-1])

    def _request_fix(self,n,fingerprint,findings):
        w = self.ledger.get("workflows",n["workflow_id"])
        current = self.ledger.get("nodes",n["id"])
        progress = current["no_progress"]+1 if current["last_failure"] == fingerprint else 0
        policy = w["plan"]["policy"]
        if progress >= policy["no_progress_limit"]:
            self.ledger.transition(n["id"],current["state"],"blocked_no_progress",reason="Repeated failure without progress",no_progress=progress)
            return
        if current["fix_rounds"] >= policy["max_fix_rounds"]:
            self.ledger.transition(n["id"],current["state"],"blocked_budget",reason="Shared validation/review fix budget exhausted")
            return
        self.ledger.transition(n["id"],current["state"],"fix_queued",reason=canonical(findings),last_failure=fingerprint,no_progress=progress)

    async def _fix(self,n):
        from app.task_manager import _Turn
        task = self.manager.db.get_task(n["task_id"])
        if not task["codex_thread_id"]:
            self.ledger.transition(n["id"],"reserving_fix","blocked_result",reason="No recoverable original implementation thread")
            return
        prompt = "Autopilot correction for the existing work. Keep the existing worktree/thread. Resolve findings with evidence; retain the original acceptance contract.\n"+n["reason"]
        turn = _Turn(prompt,resume_thread=task["codex_thread_id"],service_tier="default",trigger="autopilot_fix")
        fields = self.manager._queue_fields(turn)
        op_id = uuid.uuid4().hex
        with self.ledger.transaction() as c:
            w = c.execute("SELECT state FROM ap_workflows WHERE id=?",(n["workflow_id"],)).fetchone()
            if w["state"] != "running":
                c.execute("UPDATE ap_nodes SET state='fix_queued' WHERE id=? AND state='reserving_fix'",(n["id"],))
                return
            cur = c.execute("UPDATE tasks SET status='queued',"+",".join(f"{k}=?" for k in fields)+" WHERE id=? AND status='completed' AND completion_pending=0 AND codex_thread_id=?",
                            (*fields.values(),task["id"],task["codex_thread_id"]))
            if cur.rowcount != 1:
                raise ValueError("original implementation turn is no longer idle")
            c.execute("INSERT INTO ap_operations VALUES(?,?, 'fix',?,'reserved',?,?)",(op_id,n["id"],n["fix_rounds"]+1,canonical(dict(thread=task["codex_thread_id"],prompt_digest=digest(prompt),candidate_id=n["candidate_id"])),now_iso()))
            c.execute("UPDATE ap_nodes SET state='fixing',fix_rounds=fix_rounds+1 WHERE id=? AND state='reserving_fix'",(n["id"],))
            self.manager.db._insert_message(task["id"],turn.message())
        self.manager.db._notify(task["id"],"completed","queued")

    async def _finish_conflict(self,n):
        w = self.ledger.get("workflows",n["workflow_id"])
        task = self.manager.db.get_task(n["task_id"])
        if not completion.parse_result(task["semantic_result"]) or completion.parse_result(task["semantic_result"])["status"] != "SUCCESS":
            self.ledger.transition(n["id"],"integrating","blocked_result",reason="Conflict resolver did not report complete resolution")
            return
        _, conflicts, _ = await git.git(task["worktree"],"diff","--name-only","--diff-filter=U")
        if conflicts:
            self._request_fix(n,"integration-conflict",[dict(id="integration-conflict",path="",line=0,reason=conflicts)])
            return
        op = self.ledger.rows("operations","node_id=? AND kind='integration'",(n["id"],))[-1]
        await git.submit_head(task,n["spec"],w["plan"]["permissions"]["commit_owned_changes"])
        result = await git.integrate(task["worktree"],op["payload"].get("remaining",[]),op["id"])
        self.ledger.finish("operations",op["id"],result["state"],dict(op["payload"],**result))
        if result["state"] == "conflict":
            self._request_fix(n,"integration-conflict",[dict(id="integration-conflict",path="",line=0,reason=canonical(result))])
        else:
            self.ledger.transition(n["id"],"integrating","submitting")
            await self._submit(self.ledger.get("nodes",n["id"]))

    async def _publish(self,n):
        w = self.ledger.get("workflows",n["workflow_id"])
        candidate = self.ledger.get("candidates",n["candidate_id"])
        target = "refs/heads/main"
        head = candidate["payload"]["scope"]["head_sha"]
        initial = w["plan"]["base_sha"]
        repo = w["plan"]["repository"]
        if n["spec"]["kind"] != "integration":
            self.ledger.transition(n["id"],"publishing","blocked_configuration",reason="main publication requires a final independently reviewed integration node")
            return
        current = await git.sha(repo,target)
        _, worktrees, _ = await git.git(repo,"worktree","list","--porcelain")
        if current != initial or "branch "+target in worktrees:
            self.ledger.transition(n["id"],"publishing","blocked_decision",reason="Target advanced or has a live checkout; accepted candidate retained for safe publication")
            return
        op = self.ledger.record("operations",node_id=n["id"],kind="publish",generation=n["generation"],state="running",payload=dict(expected_ref=target,start_sha=initial,head_sha=head))
        await git.git(repo,"update-ref",target,head,initial)
        self.ledger.finish("operations",op["id"],"completed",op["payload"])
        self.ledger.transition(n["id"],"publishing","accepted")
        with self.ledger.transaction() as c:
            c.execute("UPDATE ap_workflows SET state='completed',reason='Explicit main publication completed; no deploy or restart' WHERE id=? AND state='running'",(w["id"],))

    def reconcile(self,workflow_id=None):
        nodes = self.ledger.rows("nodes","workflow_id=?" if workflow_id else "1",(workflow_id,) if workflow_id else ())
        for n in nodes:
            if n["id"] in self.jobs:
                continue
            state = n["state"]
            if state in ("launching","integrating","publishing","reserving_fix","submitting"):
                task = self.manager.db.get_task(n["task_id"]) if n["task_id"] else None
                if state == "submitting" and task and task["status"] == "completed":
                    self.ledger.transition(n["id"],state,"implementing")
                else:
                    self.ledger.transition(n["id"],state,"reconciling",reason="External operation result uncertain; inspect Git/operation ledger before retry")
            elif state in ("reviewing","validating"):
                table = "reviews" if state == "reviewing" else "validation"
                records = self.ledger.rows(table,"candidate_id=?",(n["candidate_id"],))
                complete = [r for r in records if r["state"] in ("completed","PASS","FAIL")]
                active = [r for r in records if r["state"] in ("running","claimed")]
                if active:
                    for r in active:
                        identity = r["payload"].get("identity",{})
                        alive = same_process(identity.get("pid"),identity.get("proc_identity"),needle="")
                        self.ledger.finish(table,r["id"],"uncertain",dict(r["payload"],reconciliation="process_alive" if alive else "process_dead_or_identity_unknown"))
                    self.ledger.transition(n["id"],state,"reconciling",reason="Review/validator outcome unknown; no duplicate launch or stale acceptance")
                elif complete:
                    latest = complete[-1]
                    if state == "validating" and latest["state"] == "PASS":
                        self.ledger.transition(n["id"],state,"review_queued")
                    elif state == "reviewing" and latest["payload"].get("verdict") == "APPROVE":
                        self.ledger.transition(n["id"],state,"review_complete")
                    else:
                        self.ledger.transition(n["id"],state,"reconciling",reason="Persisted non-approval requires inspected recovery")
                else:
                    self.ledger.transition(n["id"],state,"reconciling",reason="Claimed operation has no durable result")

    async def shutdown(self):
        self.closed = True
        for job in self.jobs.values():
            job.cancel()
        await asyncio.gather(*list(self.jobs.values()),return_exceptions=True)
