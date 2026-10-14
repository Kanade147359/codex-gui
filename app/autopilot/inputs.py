"""Controller-owned requirement bodies and accepted artifact materialization."""

import hashlib
import json
from pathlib import Path

from . import git
from .policy import canonical, digest, safe_path, owns

DIRECTORY = ".autopilot-inputs"


def bytes_digest(data):
    return hashlib.sha256(data).hexdigest()


async def accepted_text(repo, head, name):
    data = bytearray()

    def collect(chunk):
        data.extend(chunk)
        if len(data) > 262144:
            raise ValueError("fixed text artifact exceeds 256 KiB; split it explicitly")

    await git.git(repo, "show", head + ":" + safe_path(name), _stdout_consumer=collect)
    try:
        text = data.decode("utf-8")
    except UnicodeError as error:
        raise ValueError("fixed requirement/handoff must be UTF-8 text") from error
    if "\x00" in text:
        raise ValueError("binary handoff requires an explicit text summary")
    return text


async def freeze_rules(repo, base):
    _, names, _ = await git.git(repo, "ls-tree", "-r", "--name-only", "-z", base)
    rules = {}
    for name in names.split("\0"):
        if Path(name).name not in ("AGENTS.md", "CLAUDE.md"):
            continue
        _, entry, _ = await git.git(repo, "ls-tree", base, "--", name)
        if not entry.startswith(("100644 ", "100755 ")):
            raise ValueError("applicable rules must be regular committed files")
        body = await accepted_text(repo, base, name)
        if len(body.encode()) > 262144:
            raise ValueError("repository rules require explicit partitioning")
        rules[name] = body
    return rules


async def requirements(ap, node, workflow):
    spec, plan = node["spec"], workflow["plan"]
    docs = []
    names = set(spec["input_files"]) | {n for n in plan["inputs"] if Path(n).name.upper() == "COMMON.MD"}
    for name in sorted(names):
        docs.append(dict(name="documents/" + name, body=plan["inputs"][name], origin="owner input"))
    for name, body in plan.get("rules", {}).items():
        parent = str(Path(name).parent)
        if parent == "." or owns(parent, spec["paths"]) or any(owns(p, [parent]) for p in spec["paths"]):
            docs.append(dict(name="rules/" + name, body=body, origin="base rules; candidate edits cannot waive"))
    for item in node["inputs"]:
        candidate = ap.ledger.get("candidates", item["candidate_id"])
        parent = ap.ledger.get("nodes", candidate["node_id"])
        paths = set(parent["spec"].get("handoff", [])) | set(parent["spec"]["validation"]["artifacts"])
        for name in sorted(paths):
            name = safe_path(name)
            _, tree_entry, _ = await git.git(plan["repository"], "ls-tree", item["head_sha"], "--", name)
            if not tree_entry.startswith(("100644 ", "100755 ")):
                raise ValueError("handoff must be a regular accepted file: " + name)
            body = await accepted_text(plan["repository"], item["head_sha"], name)
            if len(body.encode()) > 262144:
                raise ValueError("handoff too large; split the registered artifact")
            docs.append(
                dict(
                    name="handoff/" + item["node"] + "/" + name,
                    body=body,
                    origin="accepted artifact",
                    candidate_id=item["candidate_id"],
                    head_sha=item["head_sha"],
                    relative_path=name,
                )
            )
    for doc in docs:
        doc["digest"] = bytes_digest(doc["body"].encode())
    result = dict(version=1, node=node["name"], instruction=spec["prompt"], documents=docs)
    result["digest"] = digest(result)
    return result


def materialize(root, bundle):
    root = Path(root)
    target = root / DIRECTORY
    if target.is_symlink():
        raise ValueError("input directory is a symlink")
    target.mkdir(exist_ok=True)
    files = {doc["name"]: doc["body"] for doc in bundle["documents"]}
    files["requirements.json"] = canonical(bundle)
    for name, body in files.items():
        path = target / safe_path(name)
        if any(p.is_symlink() for p in [path, *path.parents] if p.is_relative_to(target)):
            raise ValueError("immutable input traverses a symlink")
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.read_bytes() != body.encode():
                raise ValueError("immutable input was changed: " + name)
        else:
            path.write_bytes(body.encode())
            path.chmod(0o444)
    if {str(p.relative_to(target)) for p in target.rglob("*") if p.is_file()} != set(files):
        raise ValueError("unexpected file in immutable input bundle")
    return DIRECTORY + "/requirements.json"


def verify(root, bundle):
    target = Path(root) / DIRECTORY
    for name in ["requirements.json", *[doc["name"] for doc in bundle["documents"]]]:
        path = target / safe_path(name)
        if not path.is_file() or any(p.is_symlink() for p in [path, *path.parents] if p.is_relative_to(target)):
            raise ValueError("fixed requirements missing or replaced with symlink")
    if json.loads((target / "requirements.json").read_text()) != bundle:
        raise ValueError("fixed requirements changed")
    materialize(root, bundle)
