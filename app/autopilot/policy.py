"""Trusted, versioned manifest and validation contracts (never read from candidate code)."""

import hashlib
import json
from pathlib import PurePosixPath
import re


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def input_digest(node, workflow):
    return digest(
        dict(dependencies=node["inputs"], bundle=workflow["plan"]["inputs"], references=node["spec"]["input_files"])
    )


def safe_path(value):
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or "\\" in value
        or PurePosixPath(value).is_absolute()
        or any(p in ("..", ".git") for p in value.split("/"))
    ):
        raise ValueError("paths must be relative and outside .git")
    return str(PurePosixPath(value))


def owns(path, paths):
    return any(p == "." or path == p or path.startswith(p.rstrip("/") + "/") for p in paths)


def profile(value, kind):
    if not isinstance(value, dict) or set(value) - {"commands", "artifacts", "timeout", "outputs", "network"}:
        raise ValueError("invalid validation profile")
    for key in ("commands", "artifacts", "outputs"):
        if key in value and not isinstance(value[key], list):
            raise ValueError(f"{key} must be a list")
    commands = value.get("commands", [])
    artifacts = [safe_path(p) for p in value.get("artifacts", [])]
    if not isinstance(commands, list) or len(commands) > 20:
        raise ValueError("commands must be an argv list of lists (at most 20)")
    for argv in commands:
        if not isinstance(argv, list) or not argv or any(not isinstance(a, str) or not a or "\x00" in a for a in argv):
            raise ValueError("validation commands must use explicit argv")
    if kind != "report" and not commands:
        raise ValueError("code and integration nodes require validation commands")
    if not commands and not artifacts:
        raise ValueError("document profiles require explicit file checks")
    timeout = value.get("timeout", 300)
    if not isinstance(timeout, int) or not 1 <= timeout <= 3600:
        raise ValueError("validation timeout must be 1..3600 seconds")
    outputs = [safe_path(p) for p in value.get("outputs", [])]
    if any(p == "." or p in artifacts for p in outputs):
        raise ValueError("build outputs cannot replace the source root or required artifacts")
    if value.get("network", False):
        raise ValueError("v1 validation has no tool network; preinstall dependencies in the host runtime")
    return dict(commands=commands, artifacts=artifacts, timeout=timeout, outputs=outputs, network=False)


def manifest(value):
    if isinstance(value, str):
        if len(value) > 1024 * 1024:
            raise ValueError("manifest too large")
        try:
            value = json.loads(value)
        except ValueError:
            import yaml

            try:
                value = yaml.safe_load(value)
            except yaml.YAMLError:
                raise ValueError("invalid safe YAML manifest") from None
    if not isinstance(value, dict) or set(value) - {
        "version",
        "name",
        "repository",
        "base_ref",
        "nodes",
        "policy",
        "permissions",
        "inputs",
        "idempotency_key",
    }:
        raise ValueError("invalid workflow manifest")
    if value.get("version") != 1:
        raise ValueError("workflow version must be 1")
    policy = dict(value.get("policy", {}))
    allowed = {
        "max_fix_rounds",
        "no_progress_limit",
        "infra_retries",
        "implementation_slots",
        "review_slots",
        "validator_slots",
        "integration_slots",
        "claude_model",
        "codex_model",
        "account",
        "bucket",
        "extra_usage_disabled_confirmed",
        "automatic_acceptance",
        "review_timeout",
        "provider_backoff",
    }
    if set(policy) - allowed:
        raise ValueError("unknown policy fields")
    for key, default, maximum in (
        ("max_fix_rounds", 3, 10),
        ("no_progress_limit", 2, 10),
        ("infra_retries", 2, 5),
        ("implementation_slots", 3, 32),
        ("review_slots", 1, 8),
        ("validator_slots", 1, 8),
        ("integration_slots", 1, 1),
        ("review_timeout", 600, 3600),
        ("provider_backoff", 60, 3600),
    ):
        policy.setdefault(key, default)
        if (
            type(policy[key]) is not int
            or not (0 if key in ("max_fix_rounds", "infra_retries") else 1) <= policy[key] <= maximum
        ):
            raise ValueError(f"invalid {key}")
    for key, default in (("account", "personal"), ("bucket", "included"), ("claude_model", ""), ("codex_model", "")):
        policy.setdefault(key, default)
        if not isinstance(policy[key], str) or policy[key].startswith("-"):
            raise ValueError(f"invalid {key}")
    for key, default in (("extra_usage_disabled_confirmed", False), ("automatic_acceptance", True)):
        policy.setdefault(key, default)
        if type(policy[key]) is not bool:
            raise ValueError(f"{key} must be boolean")
    permissions = dict(value.get("permissions", {}))
    if set(permissions) - {"commit_owned_changes", "publish_main"}:
        raise ValueError("deploy, restart and billing permissions are outside Autopilot")
    for key in ("commit_owned_changes", "publish_main"):
        permissions.setdefault(key, False)
        if type(permissions[key]) is not bool:
            raise ValueError(f"{key} must be boolean")
    inputs = value.get("inputs", {})
    if not isinstance(inputs, dict) or any(not isinstance(s, str) or len(s) > 262144 for s in inputs.values()):
        raise ValueError("inputs must be a map of immutable Markdown texts")
    inputs = {safe_path(p): s for p, s in inputs.items()}
    nodes = value.get("nodes", [])
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= 200:
        raise ValueError("workflow requires 1..200 nodes")
    by_id = {}
    for raw in nodes:
        if not isinstance(raw, dict) or set(raw) - {
            "id",
            "kind",
            "prompt",
            "paths",
            "depends_on",
            "validation",
            "contract",
            "manual_gates",
            "task_id",
            "input_files",
        }:
            raise ValueError("invalid node fields")
        n = dict(raw)
        nid = n.get("id", "")
        if not isinstance(nid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", nid) or nid in by_id:
            raise ValueError("invalid or duplicate node id")
        n.setdefault("kind", "code")
        if n["kind"] not in ("code", "report", "integration"):
            raise ValueError("unknown artifact kind")
        n.setdefault("prompt", "Integrate the pinned accepted inputs and verify the acceptance contract.")
        if not isinstance(n["prompt"], str) or not n["prompt"].strip():
            raise ValueError("node prompt is required")
        for key in ("paths", "depends_on", "manual_gates", "input_files"):
            if key in n and not isinstance(n[key], list):
                raise ValueError(f"{key} must be a list")
        n["paths"] = [safe_path(p) for p in n.get("paths", [])]
        if not n["paths"]:
            raise ValueError("explicit edit ownership is required")
        n["validation"] = profile(n.get("validation", {}), n["kind"])
        from app.completion import contract

        n["contract"] = contract(n.get("contract", {}))
        n.setdefault("depends_on", [])
        n.setdefault("manual_gates", [])
        n.setdefault("input_files", [])
        if any(not isinstance(s, str) or not s for s in n["manual_gates"]):
            raise ValueError("manual gates require named owner decisions")
        if any(p not in inputs for p in n["input_files"]):
            raise ValueError("missing input bundle reference")
        by_id[nid] = n
    order, visiting = [], set()

    def visit(nid):
        if nid not in by_id:
            raise ValueError("unknown dependency")
        if nid in visiting:
            raise ValueError("workflow dependency cycle")
        if nid in order:
            return
        visiting.add(nid)
        n = by_id[nid]
        if not isinstance(n["depends_on"], list) or len(set(n["depends_on"])) != len(n["depends_on"]):
            raise ValueError("invalid dependencies")
        for parent in n["depends_on"]:
            visit(parent)
        visiting.remove(nid)
        order.append(nid)

    for nid in by_id:
        visit(nid)
    ancestors = {}
    for nid in order:
        n = by_id[nid]
        ancestors[nid] = set(n["depends_on"])
        for p in n["depends_on"]:
            ancestors[nid].update(ancestors[p])
        code_parents = [p for p in n["depends_on"] if by_id[p]["kind"] != "report"]
        if n["kind"] != "integration" and len(code_parents) > 1:
            raise ValueError("multiple code inputs require an explicit integration node")
        if n["kind"] == "integration" and not code_parents:
            raise ValueError("integration requires accepted code inputs")
        for other in order[: order.index(nid)]:
            if other in ancestors[nid] or by_id[other]["kind"] == "integration" or n["kind"] == "integration":
                continue
            if any(
                owns(p, by_id[other]["paths"]) or owns(q, n["paths"]) for p in n["paths"] for q in by_id[other]["paths"]
            ):
                raise ValueError(f"concurrent path ownership conflict: {other}/{nid}")
    if permissions["publish_main"]:
        final = order[-1]
        if by_id[final]["kind"] != "integration" or any(
            nid != final and by_id[nid]["kind"] != "report" and nid not in ancestors[final] for nid in order
        ):
            raise ValueError("main publication requires a final integration covering all code inputs")
    repository = value.get("repository")
    if not isinstance(repository, str) or not repository or "\x00" in repository:
        raise ValueError("repository is required")
    base_ref = value.get("base_ref", "main")
    if not isinstance(base_ref, str) or not base_ref or base_ref.startswith("-") or "\x00" in base_ref:
        raise ValueError("invalid base ref")
    request_key = value.get("idempotency_key", "")
    if not isinstance(request_key, str) or len(request_key) > 128:
        raise ValueError("invalid idempotency key")
    return dict(
        version=1,
        name=value.get("name", "Autopilot"),
        repository=repository,
        base_ref=base_ref,
        nodes=[by_id[n] for n in order],
        policy=policy,
        permissions=permissions,
        inputs=inputs,
        idempotency_key=request_key,
    )
