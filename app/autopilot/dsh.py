"""Local, bounded advisory dispatch. The existing Controller owns every side effect."""

import asyncio
import json
import os
import re
import time
import uuid
import urllib.request
import urllib.error
from urllib.parse import urlsplit

from app.models import now_iso
from app.procinfo import process_identity
from .policy import canonical, digest
from .providers import structured_json
from .runner import redact

MAX_RESPONSE = 65536


def normalize(raw):
    if not isinstance(raw, dict) or set(raw) - {"model", "endpoint", "timeout", "num_thread"}:
        raise ValueError("invalid DSH settings")
    value = dict(raw)
    model = value.get("model", "")
    if (not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,160}", model)
        or "cloud" in model.lower() or "://" in model or model.startswith(("-", "/"))):
        raise ValueError("DSH requires an explicitly installed local model")
    endpoint = value.setdefault("endpoint", "http://127.0.0.1:11434")
    if not isinstance(endpoint, str):
        raise ValueError("invalid DSH endpoint")
    url = urlsplit(endpoint)
    if (url.scheme != "http" or url.hostname not in ("127.0.0.1", "::1")
        or url.username or url.password or url.query or url.fragment or url.path not in ("", "/")):
        raise ValueError("DSH endpoint must be an explicit loopback HTTP address")
    if not url.port or not 1 <= url.port <= 65535:
        raise ValueError("DSH endpoint requires a port")
    value["endpoint"] = endpoint.rstrip("/")
    for key, default, maximum in (("timeout", 90, 300), ("num_thread", 2, 16)):
        value.setdefault(key, default)
        if type(value[key]) is not int or not 1 <= value[key] <= maximum:
            raise ValueError("invalid DSH " + key)
    return value


def schema(names):
    return dict(type="object", additionalProperties=False, required=["node_id", "reason"], properties={
        "node_id": dict(type="string", enum=names),
        "reason": dict(type="string", minLength=1, maxLength=2000),
    })


def validate(value, names):
    if (not isinstance(value, dict) or set(value) != {"node_id", "reason"}
        or not isinstance(value["node_id"], str) or value["node_id"] not in names
        or not isinstance(value["reason"], str) or not value["reason"].strip()
        or len(value["reason"]) > 2000):
        raise ValueError("DSH returned an invalid or ineligible dispatch decision")
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("DSH redirects are forbidden")


class OllamaDispatcher:
    def _request(self, config, path, body):
        # No inherited proxy, credentials, OpenAI compatibility layer or remote fallback.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        request = urllib.request.Request(config["endpoint"] + path, data=canonical(body).encode(),
                                         headers={"Content-Type": "application/json"})
        with opener.open(request, timeout=config["timeout"]) as response:
            raw = response.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise ValueError("DSH response exceeded 64 KiB")
        value = structured_json(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("DSH response must be an object")
        return value

    async def generate(self, config, context, output_schema, *, planning=False):
        config = normalize(config)
        # /show does not pull a model. Reject cloud-backed aliases even with local names.
        info = await asyncio.to_thread(self._request, config, "/api/show", {"model": config["model"]})
        if info.get("remote_host") or info.get("remote_model"):
            raise ValueError("DSH cloud model routing is forbidden")
        prompt = canonical(dict(context=context, output_schema=output_schema))
        if len(prompt.encode()) > 128 * 1024:
            raise ValueError("DSH context exceeds 128 KiB")
        value = await asyncio.to_thread(self._request, config, "/api/generate", dict(
            model=config["model"], stream=False, format=output_schema, keep_alive=0,
            system=("Decompose the goal into at most eight executable tasks within the supplied edit paths. "
                    "Use only supplied validation profiles; dependencies must form an acyclic graph. "
                    if planning else "Select one eligible task from the supplied data. ")
                   + "Return only the specified JSON. Do not implement code or execute commands.",
            prompt=prompt, options=dict(temperature=0, num_predict=4096 if planning else 768, num_ctx=8192,
                                       num_thread=config["num_thread"], num_gpu=0),
        ))
        if value.get("done") is not True or value.get("done_reason") != "stop":
            raise ValueError("DSH generation did not finish normally")
        usage = {k: value[k] for k in ("prompt_eval_count", "eval_count", "total_duration")
                 if type(value.get(k)) is int and value[k] >= 0}
        return structured_json(value.get("response", "")), dict(usage=usage, model=config["model"])

    async def choose(self, config, context):
        names = [n["id"] for n in context["ready"]]
        result, telemetry = await self.generate(config, context, schema(names))
        return dict(validate(result, names), **telemetry)


async def draft_plan(ap, envelope):
    """Propose a manifest within user-supplied ownership and verification profiles.

    This does not start work. The existing dry-run digest and registration path
    apply exactly as for a handwritten manifest.
    """
    from .policy import manifest, owns, safe_path, profile

    if (not isinstance(envelope, dict) or set(envelope) - {
        "goal", "repository", "base_ref", "paths", "validation_profiles", "policy", "permissions"
    }):
        raise ValueError("invalid DSH planning envelope")
    goal = envelope.get("goal")
    paths = envelope.get("paths")
    profiles = envelope.get("validation_profiles")
    if not isinstance(goal, str) or not 1 <= len(goal.strip()) <= 16000:
        raise ValueError("DSH planning requires a bounded goal")
    if not isinstance(paths, list) or not 1 <= len(paths) <= 100:
        raise ValueError("DSH planning requires authorized paths")
    paths = [safe_path(p) for p in paths]
    if (not isinstance(profiles, dict) or not 1 <= len(profiles) <= 10
        or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", key) for key in profiles)):
        raise ValueError("DSH planning requires named verification profiles")
    profiles = {key: profile(value, "code") for key, value in profiles.items()}
    policy = dict(envelope.get("policy", {}))
    config = normalize(policy.get("dsh"))
    policy.update(dsh=config, review_provider_policy="claude_only")
    # Validate permissions/policy/repository before making a local model request.
    base = dict(version=1, name=goal[:120], repository=envelope.get("repository"),
                base_ref=envelope.get("base_ref", "main"), policy=policy,
                permissions=envelope.get("permissions", {}),
                inputs={"MISSION.md": goal},
                nodes=[dict(id="draft", prompt=goal, paths=paths, validation=next(iter(profiles.values())))])
    await ap.dry_run(base)
    item = dict(type="object", additionalProperties=False,
                required=["id", "prompt", "paths", "depends_on", "validation_profile"], properties={
                    "id": dict(type="string", pattern="^[A-Za-z0-9_-]{1,64}$"),
                    "prompt": dict(type="string", minLength=1, maxLength=8000),
                    "paths": dict(type="array", minItems=1, maxItems=100, items=dict(type="string")),
                    "depends_on": dict(type="array", maxItems=7, items=dict(type="string")),
                    "validation_profile": dict(type="string", enum=list(profiles)),
                })
    output_schema = dict(type="object", additionalProperties=False, required=["tasks"],
                         properties=dict(tasks=dict(type="array", minItems=1, maxItems=8, items=item)))
    context = dict(goal=goal, authorized_paths=paths, validation_profiles=profiles)
    result, telemetry = await asyncio.wait_for(
        ap.dispatcher.generate(config, context, output_schema, planning=True), config["timeout"])
    if (not isinstance(result, dict) or set(result) != {"tasks"} or not isinstance(result["tasks"], list)
        or not 1 <= len(result["tasks"]) <= 8):
        raise ValueError("invalid DSH task decomposition")
    nodes = []
    for task in result["tasks"]:
        if (not isinstance(task, dict) or set(task) != set(item["required"])
            or not isinstance(task["validation_profile"], str) or task["validation_profile"] not in profiles
            or not isinstance(task["prompt"], str) or not 1 <= len(task["prompt"].strip()) <= 8000
            or not isinstance(task["paths"], list) or not 1 <= len(task["paths"]) <= 100
            or any(not owns(safe_path(p), paths) for p in task["paths"])):
            raise ValueError("DSH task exceeds the authorized planning envelope")
        nodes.append(dict(id=task["id"], prompt=task["prompt"], paths=task["paths"],
                          depends_on=task["depends_on"], validation=profiles[task["validation_profile"]],
                          input_files=["MISSION.md"]))
    base["nodes"] = nodes
    normalized = manifest(base)  # DAG, duplicate IDs, ownership and profile checks remain authoritative.
    preview = await ap.dry_run(normalized)
    return dict(manifest=normalized, preview=preview, planner=telemetry)


def selection(ap, workflow, nodes):
    """Return a durable choice or schedule one bounded local planning call.

    Claims are stored before requests. No selection can mutate a DAG or bypass
    launch/acceptance gates, and scheduler races cannot duplicate a planning call.
    """
    by_name = {n["name"]: n for n in nodes}
    ready = [n for n in nodes if n["state"] == "pending"
             and all(by_name[p]["state"] == "accepted" for p in n["spec"]["depends_on"])]
    if not ready:
        return None
    context = dict(goal=workflow["plan"]["name"], goals=workflow["plan"].get("goals", []),
                   ready=[dict(id=n["name"], prompt=n["spec"]["prompt"],
                               depends_on=n["spec"]["depends_on"]) for n in ready],
                   outcomes=[dict(id=n["name"], state=n["state"], reason=n["reason"][:1000])
                             for n in nodes if n["state"] != "pending"])
    key = "dsh:" + digest([workflow["generation"], workflow["input_digest"],
                            [(n["id"], n["generation"], digest(n["spec"])) for n in ready]])
    rows = ap.ledger.rows("workflow_events", "workflow_id=? AND request_key=?", (workflow["id"], key))
    if rows:
        event = rows[0]
        if event["state"] == "completed":
            return event["payload"]["decision"]["node_id"]
        if event["state"] != "retry" or event["payload"].get("retry_at", 0) > time.time():
            return None
    identity = rows[0]["id"] if rows else uuid.uuid4().hex
    previous = rows[0]["payload"] if rows else {}
    history = previous.get("history", []) + ([{k: v for k, v in previous.items() if k != "history"}] if rows else [])
    payload = dict(context_digest=digest(context), started_at=time.time(), history=history,
                   attempt=previous["attempt"] + 1 if rows else 1,
                   owner=ap.owner, pid=os.getpid(), proc_identity=process_identity(os.getpid()))
    with ap.ledger.transaction() as c:
        if c.execute("SELECT 1 FROM ap_workflow_events WHERE kind='dsh' AND state='running'").fetchone():
            return None  # One local generation across workflows, including concurrent Controllers.
        if not c.execute("SELECT 1 FROM ap_workflows WHERE id=? AND state='running' AND generation=?",
                         (workflow["id"], workflow["generation"])).fetchone():
            return None
        if rows:
            claimed = c.execute("UPDATE ap_workflow_events SET state='running',payload=? WHERE id=? AND state='retry'",
                                (canonical(payload), identity)).rowcount
        else:
            claimed = c.execute("INSERT OR IGNORE INTO ap_workflow_events VALUES(?,?,'dsh','running',?,?,?)",
                                (identity, workflow["id"], canonical(payload), key, now_iso())).rowcount
    if not claimed:
        return None
    job_key = "dsh:" + workflow["id"]
    job = asyncio.create_task(run(ap, workflow, identity, payload, context))
    ap.jobs[job_key] = job

    def done(task):
        if ap.jobs.get(job_key) is task:
            ap.jobs.pop(job_key, None)
        ap.manager.scheduler.wake()

    job.add_done_callback(done)
    return None


async def run(ap, workflow, identity, payload, context):
    policy = workflow["plan"]["policy"]
    state = "blocked"
    try:
        result = await asyncio.wait_for(ap.dispatcher.choose(policy["dsh"], context), policy["dsh"]["timeout"])
        validate({k: result.get(k) for k in ("node_id", "reason")}, [n["id"] for n in context["ready"]])
        payload.update(decision=result)
        state = "completed"
    except asyncio.CancelledError:
        payload["reason"] = "DSH interrupted; explicit Resume required before a bounded retry"
        state = "interrupted"
    except urllib.error.HTTPError as error:
        payload["reason"] = "Local Ollama HTTP status " + str(error.code)
        if error.code in (408, 429, 500, 502, 503, 504) and payload["attempt"] <= policy["infra_retries"]:
            state = "retry"
            payload["retry_at"] = time.time() + min(3600, policy["provider_backoff"] * 2 ** (payload["attempt"] - 1))
    except (OSError, asyncio.TimeoutError) as error:
        payload["reason"] = redact(str(error))[:1000] or "DSH request timed out"
        if payload["attempt"] <= policy["infra_retries"]:
            state = "retry"
            payload["retry_at"] = time.time() + min(3600, policy["provider_backoff"] * 2 ** (payload["attempt"] - 1))
    except Exception as error:
        # Invalid output/configuration never automatically replays a prompt.
        payload["reason"] = redact(str(error))[:1000]
    payload["finished_at"] = time.time()
    with ap.ledger.transaction() as c:
        c.execute("UPDATE ap_workflow_events SET state=?,payload=? WHERE id=? AND state='running'",
                  (state, canonical(payload), identity))


def recover(ap, workflow_id=None):
    where, args = (" AND workflow_id=?", (workflow_id,)) if workflow_id else ("", ())
    for event in ap.ledger.rows("workflow_events", "kind='dsh' AND state='running'" + where, args):
        if "dsh:" + event["workflow_id"] in ap.jobs:
            continue
        identity = event["payload"].get("proc_identity")
        if (event["payload"].get("owner") != ap.owner and identity
            and process_identity(event["payload"].get("pid")) == identity):
            continue  # Another live Controller owns this request; recovery cannot steal it.
        with ap.ledger.transaction() as c:
            payload = dict(event["payload"], reason="Controller restarted during DSH planning; Resume required")
            c.execute("UPDATE ap_workflow_events SET state='interrupted',payload=? WHERE id=? AND state='running'",
                      (canonical(payload), event["id"]))


def resume(ap, workflow):
    for event in ap.ledger.rows("workflow_events", "workflow_id=? AND kind='dsh' AND state='interrupted'",
                                (workflow["id"],)):
        if event["payload"].get("attempt", 1) > workflow["plan"]["policy"]["infra_retries"]:
            continue
        with ap.ledger.transaction() as c:
            c.execute("UPDATE ap_workflow_events SET state='retry' WHERE id=? AND state='interrupted'", (event["id"],))
