"""Resolve implementation settings from immutable instructions, independently of reviewers."""

from pathlib import PurePosixPath
import re

import yaml

MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z")
EFFORT = re.compile(r"[a-z]{1,16}\Z")
KEYS = {"model", "reasoning_effort"}


def validate_settings(value, *, allow_empty_model=False):
    result = {}
    for key in KEYS & value.keys():
        raw = value[key]
        if not isinstance(raw, str):
            raise ValueError(f"{key} must be a string")
        raw = raw.strip()
        if key == "model":
            if not ((not raw and allow_empty_model) or MODEL.fullmatch(raw)):
                raise ValueError("invalid implementation model")
        elif not EFFORT.fullmatch(raw):
            raise ValueError("invalid implementation reasoning_effort")
        result[key] = raw
    return result


def markdown_settings(text, name):
    """Recognize explicit metadata/directive lines, never infer a model from prose or code."""
    result = {}
    if text.startswith("---\n"):
        match = re.match(r"\A---\n(.*?)\n---(?:\n|$)", text, re.S)
        if not match:
            raise ValueError(f"unclosed Markdown front matter: {name}")
        try:
            meta = yaml.safe_load(match[1]) or {}
        except yaml.YAMLError as error:
            raise ValueError(f"invalid Markdown front matter: {name}") from error
        if not isinstance(meta, dict):
            raise ValueError(f"Markdown front matter must be a mapping: {name}")
        result.update(validate_settings(meta))
        text = text[match.end():]
    # Also accept `Model: ...` / `Reasoning effort: High` in ordinary Markdown.
    # Fenced examples and quoted material do not silently change execution settings.
    fence = None
    aliases = {"model": "model", "モデル": "model", "reasoning_effort": "reasoning_effort",
               "reasoning effort": "reasoning_effort", "推論強度": "reasoning_effort"}
    for line in text.splitlines():
        stripped = line.strip()
        marker = re.match(r"^(`{3,}|~{3,})", stripped)
        if marker:
            token = marker[1]
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            continue
        if fence or line.startswith(("    ", "\t")) or stripped.startswith(">"):
            continue
        directive = re.fullmatch(
            r"(?:[-*+]\s+)?(?:\*\*)?(model|モデル|reasoning_effort|reasoning effort|推論強度)(?:\*\*)?\s*[:：]\s*(?:\*\*)?(.*?)(?:\*\*)?",
            stripped, re.I,
        )
        if not directive:
            continue
        key = aliases[directive[1].lower()]
        raw = directive[2].strip("`\"'")
        if key == "reasoning_effort":
            raw = raw.lower()
        value = validate_settings({key: raw})[key]
        if key in result and result[key] != value:
            raise ValueError(f"conflicting {key} directives in {name}")
        result[key] = value
    return result


def _document_layer(names, inputs):
    values, sources = {}, {}
    for name in names:
        for key, value in markdown_settings(inputs[name], name).items():
            if key in values and values[key] != value:
                raise ValueError(f"conflicting {key} directives: {sources[key]} and {name}")
            values[key], sources[key] = value, name
    return values, sources


def resolve_execution(spec, plan):
    policy = plan.get("policy", {})
    result = dict(model=policy.get("implementation_model", ""),
                  reasoning_effort=policy.get("reasoning_effort", "high"),
                  model_source="workflow" if policy.get("implementation_model") else "CLI configuration",
                  reasoning_source="workflow" if "reasoning_effort" in policy else "default high")
    inputs = plan.get("inputs", {})
    files = spec.get("input_files", [])
    # requirements() automatically binds every COMMON document, even for JSON manifests.
    common = [p for p in inputs if PurePosixPath(p).name.upper() == "COMMON.MD"]
    assigned = [p for p in files if p in inputs and p not in common]
    for names, explicit in ((common, None), (None, validate_settings(spec, allow_empty_model=True)),
                            (assigned, None)):
        if names is not None:
            values, sources = _document_layer(names, inputs)
        else:
            values, sources = explicit, {key: "task" for key in explicit}
        for key, value in values.items():
            result[key] = value
            result["model_source" if key == "model" else "reasoning_source"] = (
                "markdown: " + sources[key] if names is not None else sources[key]
            )
    return result


def task_fields(spec, plan):
    settings = resolve_execution(spec, plan)
    return {key: settings[key] for key in ("model", "reasoning_effort")}
