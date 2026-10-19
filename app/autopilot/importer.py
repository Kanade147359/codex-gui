"""Bounded, non-executing Markdown/ZIP import. Never extracts archives to disk."""

import io
import re
import stat
import zipfile
from pathlib import PurePosixPath

import yaml

from .policy import digest, safe_path

MAX_FILES = 200
MAX_FILE = 256 * 1024
MAX_TOTAL = 4 * 1024 * 1024


def documents(uploads):
    result, total = {}, 0

    def add(name, data):
        nonlocal total
        name = safe_path(name)
        if name == "." or not name.lower().endswith(".md") or name in result:
            raise ValueError("only unique relative Markdown paths are accepted")
        total += len(data)
        if len(data) > MAX_FILE or total > MAX_TOTAL or len(result) >= MAX_FILES:
            raise ValueError("Markdown import size/file limit exceeded")
        text = data.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
        if "\x00" in text:
            raise ValueError("binary Markdown is not accepted")
        result[name] = text

    for name, data in uploads:
        if len(data) > MAX_TOTAL:
            raise ValueError("upload exceeds size limit")
        safe_path(name)
        if not name.lower().endswith(".zip"):
            add(name, data)
            continue
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                entries = archive.infolist()
                if len(entries) > MAX_FILES:
                    raise ValueError("archive file count exceeded")
                seen, expanded = set(), 0
                for entry in entries:
                    path = safe_path(entry.filename)
                    mode = entry.external_attr >> 16
                    if (
                        path in seen
                        or stat.S_ISLNK(mode)
                        or (mode and stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR))
                    ):
                        raise ValueError("duplicate or special archive entry")
                    seen.add(path)
                    expanded += entry.file_size
                    if entry.flag_bits & 1 or expanded > MAX_TOTAL or entry.file_size > MAX_FILE:
                        raise ValueError("encrypted or oversized archive")
                    if entry.file_size > max(4096, entry.compress_size * 100):
                        raise ValueError("archive expansion ratio exceeded")
                    if not entry.is_dir():
                        with archive.open(entry) as stream:
                            body = stream.read(MAX_FILE + 1)
                        if len(body) != entry.file_size:
                            raise ValueError("invalid archive size")
                        add(path, body)
        except (zipfile.BadZipFile, RuntimeError) as e:
            raise ValueError("invalid ZIP archive") from e
    if not result:
        raise ValueError("select Markdown files or a ZIP")
    return result


def prepare(inputs):
    """Optional YAML front matter describes metadata; Markdown body stays in inputs."""
    common = [p for p in inputs if PurePosixPath(p).name.upper() == "COMMON.MD"]
    goals = []
    for name in common:
        body = inputs[name]
        if body.startswith("---\n"):
            match = re.match(r"\A---\n(.*?)\n---(?:\n|$)", body, re.S)
            if not match:
                raise ValueError("unclosed COMMON front matter")
            try:
                metadata = yaml.safe_load(match[1]) or {}
            except yaml.YAMLError as error:
                raise ValueError("invalid COMMON metadata") from error
            if not isinstance(metadata, dict) or set(metadata) - {"goals", "model", "reasoning_effort"}:
                raise ValueError("COMMON metadata supports explicit goals and execution settings only")
            explicit = metadata.get("goals", [])
            if not isinstance(explicit, list) or any(not isinstance(g, dict) or "source" in g for g in explicit):
                raise ValueError("invalid explicit COMMON goals")
            goals.extend(dict(g, source=name) for g in explicit)
    from .goals import normalize_goals
    normalize_goals(goals, inputs)
    nodes = []
    for name, body in inputs.items():
        if name in common:
            continue
        meta = {}
        if body.startswith("---\n"):
            match = re.match(r"\A---\n(.*?)\n---(?:\n|$)", body, re.S)
            if not match:
                raise ValueError("unclosed Markdown front matter")
            try:
                meta = yaml.safe_load(match[1]) or {}
            except yaml.YAMLError as error:
                raise ValueError("invalid task YAML front matter") from error
            if not isinstance(meta, dict):
                raise ValueError("task metadata must be a mapping")
        allowed = {
            "id",
            "kind",
            "depends_on",
            "paths",
            "validation",
            "manual_gates",
            "contract",
            "draft",
            "envelope",
            "integration_mode",
            "plan_artifact",
            "handoff",
            "review_files",
            "goal_ids",
            "acceptance_ids",
            "model",
            "reasoning_effort",
        }
        if set(meta) - allowed:
            raise ValueError("unknown task metadata")
        node = dict(meta)
        node.setdefault("id", PurePosixPath(name).stem)
        node.setdefault("paths", [])
        node.setdefault("validation", {})
        node.setdefault("depends_on", [])
        node.setdefault("manual_gates", [])
        node.setdefault("draft", not node["paths"] or not node["validation"])
        node["prompt"] = "Read and implement the fixed assigned instructions: " + name
        node["input_files"] = [*common, name]
        from .execution import resolve_execution

        resolve_execution(node, dict(inputs=inputs))
        nodes.append(node)
    if not nodes:
        raise ValueError("at least one task instruction is required")
    return dict(version=1, inputs=inputs, nodes=nodes, document_digest=digest(inputs), common=common, goals=goals)


def convert(body):
    inputs = body["inputs"]
    if not isinstance(inputs, dict) or any(
        not isinstance(name, str) or not isinstance(text, str) for name, text in inputs.items()
    ):
        raise ValueError("edited inputs must contain Markdown text")
    # Revalidate edited documents with the exact same bounds as uploaded files.
    inputs = documents([(name, text.encode("utf-8")) for name, text in inputs.items()])
    nodes = body["nodes"]
    return dict(
        version=1,
        name=body.get("name", "Markdown workflow"),
        repository=body["repository"],
        base_ref=body.get("base_ref", "main"),
        inputs=inputs,
        goals=body.get("goals", []),
        nodes=nodes,
        policy=body.get("policy", {}),
        permissions=body.get("permissions", {}),
    )
