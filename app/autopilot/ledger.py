"""Durable controller ledger. All claims and acceptance use the existing database lock."""

import json
import uuid
from contextlib import contextmanager

from app.models import now_iso
from .policy import canonical, digest, input_digest

SCHEMA = """
CREATE TABLE IF NOT EXISTS ap_workflows (
 id TEXT PRIMARY KEY, input_digest TEXT NOT NULL UNIQUE, plan TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'draft', generation INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS ap_nodes (
 id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL REFERENCES ap_workflows(id), name TEXT NOT NULL,
 spec TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', task_id TEXT UNIQUE,
 generation INTEGER NOT NULL DEFAULT 0, candidate_id TEXT, fix_rounds INTEGER NOT NULL DEFAULT 0,
 no_progress INTEGER NOT NULL DEFAULT 0, last_failure TEXT NOT NULL DEFAULT '',
 reason TEXT NOT NULL DEFAULT '', inputs TEXT NOT NULL DEFAULT '[]', ready_at TEXT NOT NULL,
 UNIQUE(workflow_id,name));
CREATE TABLE IF NOT EXISTS ap_edges (
 node_id TEXT NOT NULL REFERENCES ap_nodes(id), parent_id TEXT NOT NULL REFERENCES ap_nodes(id),
 PRIMARY KEY(node_id,parent_id));
CREATE TABLE IF NOT EXISTS ap_candidates (
 id TEXT PRIMARY KEY, node_id TEXT NOT NULL REFERENCES ap_nodes(id), generation INTEGER NOT NULL,
 run_generation INTEGER NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL,
 created_at TEXT NOT NULL, UNIQUE(node_id,generation));
CREATE TABLE IF NOT EXISTS ap_validation (
 id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL REFERENCES ap_candidates(id),
 state TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ap_reviews (
 id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL REFERENCES ap_candidates(id), provider TEXT NOT NULL,
 generation INTEGER NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL,
 created_at TEXT NOT NULL, UNIQUE(candidate_id,generation));
CREATE UNIQUE INDEX IF NOT EXISTS ap_one_active_review ON ap_reviews(candidate_id)
 WHERE state IN ('claimed','running');
CREATE TABLE IF NOT EXISTS ap_acceptances (
 id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL UNIQUE REFERENCES ap_candidates(id),
 payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ap_operations (
 id TEXT PRIMARY KEY, node_id TEXT NOT NULL REFERENCES ap_nodes(id), kind TEXT NOT NULL,
 generation INTEGER NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL,
 created_at TEXT NOT NULL, UNIQUE(node_id,kind,generation));
CREATE TABLE IF NOT EXISTS ap_provider_availability (
 key TEXT PRIMARY KEY, provider TEXT NOT NULL, state TEXT NOT NULL, recheck_at REAL NOT NULL,
 probe_owner TEXT, payload TEXT NOT NULL);
"""


def migrate(db):
    with db._lock:
        db._conn.executescript(SCHEMA)
        have = {r["name"] for r in db._conn.execute("PRAGMA table_info(ap_workflows)")}
        if "request_key" not in have:
            db._conn.execute("ALTER TABLE ap_workflows ADD COLUMN request_key TEXT NOT NULL DEFAULT ''")
        db._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ap_request_key ON ap_workflows(request_key) WHERE request_key!=''"
        )
        db._conn.commit()


def unpack(row):
    if row is None:
        return None
    value = dict(row)
    for k in ("plan", "spec", "payload", "inputs"):
        if k in value:
            value[k] = json.loads(value[k])
    return value


class Ledger:
    def __init__(self, db):
        self.db = db

    @contextmanager
    def transaction(self):
        with self.db._lock, self.db._conn:
            # Explicit BEGIN makes read/check/write a single transaction across connections.
            self.db._conn.execute("BEGIN IMMEDIATE")
            yield self.db._conn

    def rows(self, table, where="1", args=()):
        if table not in {
            "workflows",
            "nodes",
            "edges",
            "candidates",
            "validation",
            "reviews",
            "acceptances",
            "operations",
            "provider_availability",
        }:
            raise ValueError("unknown ledger table")
        with self.db._lock:
            return [
                unpack(r) for r in self.db._conn.execute(f"SELECT * FROM ap_{table} WHERE {where} ORDER BY rowid", args)
            ]

    def get(self, table, identity):
        rows = self.rows(table, "id=?", (identity,))
        return rows[0] if rows else None

    def node_for_task(self, task_id):
        rows = self.rows("nodes", "task_id=?", (task_id,))
        return rows[0] if rows else None

    def register(self, plan):
        identity, stamp = uuid.uuid4().hex, now_iso()
        key = digest(plan)
        with self.transaction() as c:
            if c.execute("SELECT id FROM ap_workflows WHERE input_digest=?", (key,)).fetchone():
                raise ValueError("workflow already registered")
            if (
                plan["idempotency_key"]
                and c.execute("SELECT id FROM ap_workflows WHERE request_key=?", (plan["idempotency_key"],)).fetchone()
            ):
                raise ValueError("idempotency key already registered; inspect the existing workflow")
            task_ids = [n["task_id"] for n in plan["nodes"] if n.get("task_id")]
            if len(task_ids) != len(set(task_ids)):
                raise ValueError("duplicate backlog task")
            for tid in task_ids:
                task = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
                if (
                    not task
                    or task["status"] != "completed"
                    or task["completion_pending"]
                    or task["task_outcome"] != "needs_review"
                ):
                    raise ValueError("backlog import requires idle completed needs_review tasks")
                if c.execute("SELECT 1 FROM ap_nodes WHERE task_id=?", (tid,)).fetchone():
                    raise ValueError("task already belongs to Autopilot")
            c.execute(
                "INSERT INTO ap_workflows(id,input_digest,plan,created_at,request_key) VALUES(?,?,?,?,?)",
                (identity, key, canonical(plan), stamp, plan["idempotency_key"]),
            )
            for n in plan["nodes"]:
                nid = identity + ":" + n["id"]
                c.execute(
                    "INSERT INTO ap_nodes(id,workflow_id,name,spec,task_id,ready_at) VALUES(?,?,?,?,?,?)",
                    (nid, identity, n["id"], canonical(n), n.get("task_id"), stamp),
                )
                for parent in n["depends_on"]:
                    c.execute("INSERT INTO ap_edges VALUES(?,?)", (nid, identity + ":" + parent))
        return self.get("workflows", identity)

    def transition(self, node_id, old, new, **fields):
        allowed = {
            "reason",
            "task_id",
            "candidate_id",
            "generation",
            "fix_rounds",
            "no_progress",
            "last_failure",
            "inputs",
        }
        if set(fields) - allowed:
            raise ValueError("invalid node field")
        fields = {k: canonical(v) if k == "inputs" else v for k, v in fields.items()}
        with self.transaction() as c:
            return (
                c.execute(
                    "UPDATE ap_nodes SET state=?"
                    + "".join(f", {k}=?" for k in fields)
                    + " WHERE id=? AND state=? AND EXISTS(SELECT 1 FROM ap_workflows w WHERE w.id=ap_nodes.workflow_id AND w.state IN ('running','paused'))",
                    (new, *fields.values(), node_id, old),
                ).rowcount
                == 1
            )

    def record(self, table, **fields):
        if table not in {"validation", "reviews", "operations"}:
            raise ValueError("invalid record table")
        fields.setdefault("id", uuid.uuid4().hex)
        fields.setdefault("created_at", now_iso())
        fields["payload"] = canonical(fields.get("payload", {}))
        with self.transaction() as c:
            c.execute(
                f"INSERT INTO ap_{table} ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
                tuple(fields.values()),
            )
        return self.get(table, fields["id"])

    def claim(self, node_id, old, new, limit=None):
        """Resource caps and state claim commit together, including concurrent scheduler callers."""
        with self.transaction() as c:
            if (
                new == "launching"
                and c.execute(
                    "SELECT 1 FROM ap_edges e JOIN ap_nodes p ON p.id=e.parent_id WHERE e.node_id=? AND p.state!='accepted'",
                    (node_id,),
                ).fetchone()
            ):
                return False
            if limit is not None:
                if new == "launching":
                    count = c.execute(
                        "SELECT count(*) FROM ap_nodes WHERE state IN ('launching','integrating') AND json_extract(spec,'$.kind')='integration'"
                    ).fetchone()[0]
                else:
                    count = c.execute("SELECT count(*) FROM ap_nodes WHERE state=?", (new,)).fetchone()[0]
                if count >= limit:
                    return False
            return (
                c.execute(
                    "UPDATE ap_nodes SET state=? WHERE id=? AND state=? AND EXISTS(SELECT 1 FROM ap_workflows w WHERE w.id=ap_nodes.workflow_id AND w.state='running')",
                    (new, node_id, old),
                ).rowcount
                == 1
            )

    def finish(self, table, identity, state, payload):
        if table not in ("reviews", "operations", "validation"):
            raise ValueError("invalid record table")
        with self.transaction() as c:
            c.execute(
                f"UPDATE ap_{table} SET state=?,payload=? WHERE id=? AND state NOT IN ('cancelled','uncertain')",
                (state, canonical(payload), identity),
            )

    def submit(self, node, workflow, payload):
        cid = uuid.uuid4().hex
        with self.transaction() as c:
            changed = c.execute(
                "UPDATE ap_nodes SET state='submitted',generation=generation+1,candidate_id=? WHERE id=? AND state=? AND generation=? AND EXISTS(SELECT 1 FROM ap_workflows w WHERE w.id=ap_nodes.workflow_id AND w.state='running' AND w.generation=?)",
                (cid, node["id"], node["state"], node["generation"], workflow["generation"]),
            ).rowcount
            if not changed:
                return None
            c.execute(
                "INSERT INTO ap_candidates VALUES(?,?,?,?,?,'submitted',?)",
                (cid, node["id"], node["generation"] + 1, workflow["generation"], canonical(payload), now_iso()),
            )
        return self.get("candidates", cid)

    def accept(self, candidate, review, current_scope):
        """AI cannot call this gate. All evidence and task success commit atomically."""
        cid = candidate["id"]
        with self.transaction() as c:
            cand = unpack(c.execute("SELECT * FROM ap_candidates WHERE id=?", (cid,)).fetchone())
            if not cand:
                return False
            n = unpack(c.execute("SELECT * FROM ap_nodes WHERE id=?", (cand["node_id"],)).fetchone())
            if not n:
                return False
            w = unpack(c.execute("SELECT * FROM ap_workflows WHERE id=?", (n["workflow_id"],)).fetchone())
            r = unpack(c.execute("SELECT * FROM ap_reviews WHERE id=?", (review["id"],)).fetchone())
            evidence = [unpack(x) for x in c.execute("SELECT * FROM ap_validation WHERE candidate_id=?", (cid,))]
            task = c.execute("SELECT * FROM tasks WHERE id=?", (n["task_id"],)).fetchone()
            if not w or not task:
                return False
            from app.completion import parse_result

            result = parse_result(task["semantic_result"]) if task else None
            p = cand["payload"]
            owner_instructions = [
                dict(prompt=row["prompt"], kind=row["kind"])
                for row in c.execute(
                    "SELECT prompt,kind FROM task_messages WHERE task_id=? AND kind IN ('instruction','steer','scheduled_instruction') ORDER BY id",
                    (task["id"],),
                )
            ]
            approved = (
                r
                and r["candidate_id"] == cid
                and r["state"] == "completed"
                and r["payload"].get("verdict") == "APPROVE"
            )
            latest_review = c.execute("SELECT max(generation) FROM ap_reviews WHERE candidate_id=?", (cid,)).fetchone()[
                0
            ]
            prior_findings = set(p.get("unresolved", []))
            for previous in c.execute(
                "SELECT payload FROM ap_reviews WHERE candidate_id=? AND id!=?", (cid, review["id"])
            ):
                data = json.loads(previous["payload"])
                prior_findings.update(f["id"] for f in data.get("blocking", []) + data.get("findings", []))
            if (
                w["state"] != "running"
                or w["generation"] != cand["run_generation"]
                or n["candidate_id"] != cid
                or n["generation"] != cand["generation"]
                or n["state"] != "reviewing"
                or not approved
                or r["generation"] != latest_review
                or r["payload"].get("run_generation") != w["generation"]
                or not r["payload"].get("session")
                or r["payload"].get("session") == task["codex_thread_id"]
                or r["payload"].get("scope") != current_scope
                or current_scope != p["scope"]
                or r["payload"].get("blocking")
                or r["payload"].get("unverified")
                or not prior_findings <= set(r["payload"].get("resolved", []))
                or not set(p.get("changed_paths", [])) <= set(r["payload"].get("coverage", []))
                or not any(
                    e["state"] == "PASS"
                    and e["payload"].get("scope") == current_scope
                    and e["payload"].get("profile_digest") == p["profile_digest"]
                    and [x.get("command_digest") for x in e["payload"].get("checks", []) if "command" in x]
                    == [digest(cmd) for cmd in n["spec"]["validation"]["commands"]]
                    and bool(e["payload"].get("checks"))
                    and all(x.get("exit_status") == 0 and not x.get("timed_out") for x in e["payload"]["checks"])
                    for e in evidence
                )
                or not w["plan"]["policy"]["automatic_acceptance"]
                or n["spec"]["manual_gates"]
                or n["spec"]["contract"].get("manual_approval")
                or p["contract"].get("manual_approval")
                or not result
                or result["status"] != "SUCCESS"
                or task["status"] != "completed"
                or task["completion_pending"]
                or digest([task["last_turn_at"], task["semantic_result"]]) != p["execution_key"]
                or digest(task["last_prompt"]) != p["prompt_digest"]
                or digest(owner_instructions) != p["instructions_digest"]
                or c.execute("SELECT count(*) FROM turns WHERE task_id=?", (task["id"],)).fetchone()[0] != p["turn"]
                or digest(w["plan"]["policy"]) != p["policy_digest"]
                or input_digest(n, w) != p["input_digest"]
                or digest(json.loads(task["completion_contract"])) != p["contract_digest"]
            ):
                return False
            c.execute(
                "INSERT OR IGNORE INTO ap_acceptances VALUES(?,?,?,?)",
                (
                    uuid.uuid4().hex,
                    cid,
                    canonical(
                        dict(
                            scope=current_scope,
                            review_id=r["id"],
                            policy_digest=p["policy_digest"],
                            reason="AI review accepted",
                        )
                    ),
                    now_iso(),
                ),
            )
            c.execute("UPDATE ap_candidates SET state='accepted' WHERE id=?", (cid,))
            c.execute("UPDATE ap_nodes SET state='accepted',reason='' WHERE id=?", (n["id"],))
            for operation in c.execute(
                "SELECT * FROM ap_operations WHERE node_id=? AND kind='integration'", (n["id"],)
            ).fetchall():
                data = json.loads(operation["payload"])
                data.update(
                    accepted_candidate_id=cid,
                    accepted_head_sha=current_scope["head_sha"],
                    accepted_review_id=r["id"],
                    validation_ids=[e["id"] for e in evidence if e["state"] == "PASS"],
                )
                c.execute("UPDATE ap_operations SET payload=? WHERE id=?", (canonical(data), operation["id"]))
            c.execute(
                "UPDATE tasks SET task_outcome='success',outcome_source='autopilot',approval_source='ai_review',approved_at=?,outcome_reason='AI review accepted',completion_pending=0 WHERE id=?",
                (now_iso(), n["task_id"]),
            )
        self.db._notify(n["task_id"], "completed", "completed")
        return True

    def view(self, workflow_id):
        w = self.get("workflows", workflow_id)
        if not w:
            raise ValueError("workflow not found")
        nodes = self.rows("nodes", "workflow_id=?", (workflow_id,))
        for n in nodes:
            n["candidates"] = self.rows("candidates", "node_id=?", (n["id"],))
            for cand in n["candidates"]:
                cand["validation"] = self.rows("validation", "candidate_id=?", (cand["id"],))
                cand["reviews"] = self.rows("reviews", "candidate_id=?", (cand["id"],))
                cand["acceptances"] = self.rows("acceptances", "candidate_id=?", (cand["id"],))
            n["operations"] = self.rows("operations", "node_id=?", (n["id"],))
        return dict(**w, nodes=nodes)
