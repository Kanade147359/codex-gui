"""Versioned, source-bound goal evaluation; never infer owner-approved goals from prose."""

import copy
import re

from .policy import digest

AXES = (
    "task_correctness",
    "goal_alignment",
    "architectural_consistency",
    "evidence_completeness",
)
CLASSIFICATIONS = (
    "IMPLEMENTATION_DEFECT",
    "TASK_ASSUMPTION_FALSE",
    "TASK_CONTRACT_CONFLICT",
    "MISSING_PREREQUISITE",
    "ENVIRONMENT_FAILURE",
    "INCONCLUSIVE",
)
ROOT_CAUSES = set(CLASSIFICATIONS[1:4])


def declarations(value, inputs, *, kinds=True):
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError("requirements must be a bounded list")
    seen, result = set(), []
    for raw in value:
        allowed = {"id", "text", "source", "quote", "kind"} if kinds else {"id", "text", "goal_ids"}
        if not isinstance(raw, dict) or set(raw) - allowed:
            raise ValueError("invalid explicit requirement")
        item = copy.deepcopy(raw)
        identity = item.get("id")
        if (
            not isinstance(identity, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", identity)
            or identity in seen
        ):
            raise ValueError("invalid or duplicate requirement id")
        if not isinstance(item.get("text"), str) or not item["text"].strip() or len(item["text"]) > 8192:
            raise ValueError("explicit requirements need text")
        if kinds:
            item.setdefault("kind", "goal")
            if item["kind"] not in ("goal", "invariant", "prohibition", "architecture"):
                raise ValueError("invalid requirement kind")
            if item.get("source") and (
                item["source"] not in inputs
                or not item.get("quote")
                or item["quote"] not in inputs[item["source"]]
            ):
                raise ValueError("goal source quote is absent from fixed owner input")
        else:
            item.setdefault("goal_ids", [])
            if not isinstance(item["goal_ids"], list) or any(
                not isinstance(s, str) for s in item["goal_ids"]
            ):
                raise ValueError("invalid goal association")
        seen.add(identity)
        result.append(item)
    return result


def index(plan, spec, documents):
    sources = [dict(ref="task:instruction", role="task_purpose", digest=digest(spec["prompt"]))]
    for doc in documents:
        role = (
            "global_requirements"
            if doc["name"].split("/")[-1].upper() == "COMMON.MD"
            else "applicable_rules"
            if doc["name"].startswith("rules/")
            else "accepted_artifact"
            if doc["name"].startswith("handoff/")
            else "assigned_instructions"
        )
        sources.append(dict(ref=doc["name"], role=role, digest=doc["digest"]))
    value = dict(
        version=1,
        declared=plan.get("goals", []),
        task_goal_ids=spec.get("goal_ids", []),
        acceptance_conditions=spec.get("acceptance_conditions", []),
        assumptions=spec.get("assumptions", []),
        sources=sources,
        interpretation="Source prose remains binding data; no inferred goal has owner approval.",
    )
    value["digest"] = digest(value)
    return value


def schema():
    strings = dict(type="array", items=dict(type="string"))
    evidence = dict(
        type="object",
        additionalProperties=False,
        properties=dict(
            source_ref=dict(type="string"),
            path=dict(type="string"),
            line=dict(type="integer"),
            reason=dict(type="string"),
        ),
        required=["source_ref", "path", "line", "reason"],
    )
    axis = dict(
        type="object",
        additionalProperties=False,
        properties=dict(
            status=dict(type="string", enum=["MATCH", "VIOLATION", "UNKNOWN"]),
            goal_ids=strings,
            acceptance_ids=strings,
            source_refs=strings,
            evidence=dict(type="array", items=evidence),
        ),
        required=["status", "goal_ids", "acceptance_ids", "source_refs", "evidence"],
    )
    return dict(
        type="object",
        additionalProperties=False,
        properties=dict(
            version=dict(type="integer", enum=[1]),
            requirements_digest=dict(type="string"),
            goal_index_digest=dict(type="string"),
            **{axis_name: copy.deepcopy(axis) for axis_name in AXES},
        ),
        required=["version", "requirements_digest", "goal_index_digest", *AXES],
    )


def diagnosis_schema():
    anchor = dict(
        type="object",
        additionalProperties=False,
        properties=dict(
            kind=dict(type="string", enum=["source", "requirement", "validation"]),
            ref=dict(type="string"),
            quote=dict(type="string"),
        ),
        required=["kind", "ref", "quote"],
    )
    return dict(
        type="object",
        additionalProperties=False,
        properties=dict(
            version=dict(type="integer", enum=[1]),
            classification=dict(type="string", enum=list(CLASSIFICATIONS)),
            finding_ids=dict(type="array", items=dict(type="string")),
            reproduction=dict(type="string"),
            observed=dict(type="string"),
            expected=dict(type="string"),
            rationale=dict(type="string"),
            anchors=dict(type="array", items=anchor),
        ),
        required=[
            "version",
            "classification",
            "finding_ids",
            "reproduction",
            "observed",
            "expected",
            "rationale",
            "anchors",
        ],
    )


def validate_diagnosis(value):
    if (
        not isinstance(value, dict)
        or set(value) != set(diagnosis_schema()["required"])
        or value["version"] != 1
        or value["classification"] not in CLASSIFICATIONS
    ):
        raise ValueError("invalid root-cause classification contract")
    if any(
        not isinstance(value[key], str) or not value[key].strip() or len(value[key]) > 16384
        for key in ("reproduction", "observed", "expected", "rationale")
    ):
        raise ValueError("classification requires reproduction, expected/observed result and rationale")
    if not isinstance(value["finding_ids"], list) or any(
        not isinstance(s, str) for s in value["finding_ids"]
    ):
        raise ValueError("classification requires finding references")
    if not isinstance(value["anchors"], list) or len(value["anchors"]) > 20:
        raise ValueError("invalid root-cause evidence anchors")
    for a in value["anchors"]:
        if (
            not isinstance(a, dict)
            or set(a) != {"kind", "ref", "quote"}
            or a["kind"] not in ("source", "requirement", "validation")
            or any(not isinstance(a[k], str) or not a[k].strip() for k in ("ref", "quote"))
        ):
            raise ValueError("invalid root-cause evidence anchor")
    if value["classification"] in ROOT_CAUSES and (not value["finding_ids"] or not value["anchors"]):
        raise ValueError("premise errors require findings and anchored counterexamples")
    return value


def validate_review(value, candidate):
    if candidate["payload"].get("goal_review_version", 1) < 2:
        return
    if value.get("review_version") != 2:
        raise ValueError("legacy review cannot satisfy goal-aware review v2")
    bundle = candidate["payload"]["requirements"]
    assessment = value.get("goal_assessment")
    if (
        not isinstance(assessment, dict)
        or set(assessment) != set(schema()["required"])
        or assessment["version"] != 1
        or assessment["requirements_digest"] != bundle["digest"]
        or assessment["goal_index_digest"] != bundle["goal_index"]["digest"]
    ):
        raise ValueError("goal evaluation does not match fixed requirements")
    goal_ids = {g["id"] for g in bundle["goal_index"]["declared"]}
    acceptance_ids = {g["id"] for g in bundle["goal_index"]["acceptance_conditions"]}
    source_refs = {s["ref"] for s in bundle["goal_index"]["sources"]}
    for name in AXES:
        a = assessment[name]
        if (
            not isinstance(a, dict)
            or set(a) != {"status", "goal_ids", "acceptance_ids", "source_refs", "evidence"}
            or a["status"] not in ("MATCH", "VIOLATION", "UNKNOWN")
        ):
            raise ValueError("missing independent review axis")
        for key, allowed in (
            ("goal_ids", goal_ids),
            ("acceptance_ids", acceptance_ids),
            ("source_refs", source_refs),
        ):
            if not isinstance(a[key], list) or any(
                not isinstance(ref, str) or ref not in allowed for ref in a[key]
            ):
                raise ValueError("unknown requirement/goal reference")
        if name in ("goal_alignment", "architectural_consistency"):
            required_sources = {
                s["ref"]
                for s in bundle["goal_index"]["sources"]
                if s["role"] in ("global_requirements", "applicable_rules")
            }
            if not goal_ids.issubset(a["goal_ids"]) or not required_sources.issubset(a["source_refs"]):
                raise ValueError("global goals and applicable source requirements must all be assessed")
        if name == "task_correctness" and not acceptance_ids.issubset(a["acceptance_ids"]):
            raise ValueError("task acceptance conditions must all be assessed")
        if not isinstance(a["evidence"], list) or not a["evidence"]:
            raise ValueError("each axis requires evidence")
        for e in a["evidence"]:
            if (
                not isinstance(e, dict)
                or set(e) != {"source_ref", "path", "line", "reason"}
                or e["source_ref"] not in source_refs
                or not isinstance(e["path"], str)
                or type(e["line"]) is not int
                or e["line"] < 0
                or not isinstance(e["reason"], str)
                or not e["reason"].strip()
            ):
                raise ValueError("invalid goal evaluation evidence")
        if value["verdict"] == "APPROVE" and a["status"] != "MATCH":
            raise ValueError("goal violation/unknown cannot be approved")
    validate_diagnosis(value.get("diagnosis"))
    if value["verdict"] == "APPROVE" and value["diagnosis"]["classification"] in ROOT_CAUSES | {
        "ENVIRONMENT_FAILURE",
        "INCONCLUSIVE",
    }:
        raise ValueError("unresolved premise/environment diagnosis cannot approve a candidate")


def fields(value):
    return {k: value[k] for k in ("review_version", "goal_assessment", "diagnosis") if k in value}
