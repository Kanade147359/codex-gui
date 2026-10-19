"""Versioned goal review contract. Free-form requirements remain exact source text."""

from .policy import safe_path

AXES = ("task_correctness", "goal_alignment", "architectural_consistency", "evidence_completeness")
CLASSIFICATIONS = ("IMPLEMENTATION_DEFECT", "TASK_ASSUMPTION_FALSE", "TASK_CONTRACT_CONFLICT",
                   "MISSING_PREREQUISITE", "ENVIRONMENT_FAILURE", "INCONCLUSIVE")
REPLAN_CLASSES = CLASSIFICATIONS[1:4]


def normalize_goals(goals, inputs):
    if not isinstance(goals, list) or len(goals) > 200:
        raise ValueError("explicit goals require a bounded list")
    seen = set()
    for g in goals:
        if (not isinstance(g, dict) or set(g) != {"id", "kind", "text", "source"}
            or any(not isinstance(g[k], str) or not g[k].strip() for k in g)
            or g["kind"] not in ("goal", "invariant", "prohibition") or g["id"] in seen
            or safe_path(g["source"]) not in inputs or g["text"] not in inputs[g["source"]]):
            raise ValueError("goal metadata must reference exact owner text and a unique explicit ID")
        seen.add(g["id"])
    return goals


def required(candidate):
    return candidate["payload"].get("requirements", {}).get("version", 0) >= 2


def obj(properties):
    return dict(type="object", additionalProperties=False, properties=properties, required=list(properties))


def strings():
    return dict(type="array", items=dict(type="string"))


CAUSE_SCHEMA = obj(dict(
    finding_id=dict(type="string"), classification=dict(type="string", enum=list(CLASSIFICATIONS)),
    reproduction=dict(type="string"), observed=dict(type="string"), expected=dict(type="string"),
    requirement_refs=strings(), counterexample=dict(type="string"), evidence_refs=strings()))
ASSESSMENT_SCHEMA = obj(dict(status=dict(type="string", enum=["ALIGNED", "VIOLATION", "UNKNOWN"]),
                             goal_ids=strings(), evidence=dict(type="string")))
GOAL_SCHEMA = obj(dict(version=dict(type="integer", enum=[2]), requirements_digest=dict(type="string"),
                       assessments=obj({k: ASSESSMENT_SCHEMA for k in AXES}),
                       root_causes=dict(type="array", items=CAUSE_SCHEMA)))


def validate_schema(value, schema):
    """Small closed JSON-schema subset shared with provider output schemas."""
    kind = schema["type"]
    if kind == "object":
        if not isinstance(value, dict) or set(value) != set(schema["required"]):
            raise ValueError("invalid versioned structured fields")
        for key, field in schema["properties"].items():
            validate_schema(value[key], field)
    elif kind == "array":
        if not isinstance(value, list) or len(value) > 200:
            raise ValueError("invalid bounded structured list")
        for item in value:
            validate_schema(item, schema["items"])
    elif kind == "string":
        if not isinstance(value, str) or len(value.encode()) > 65536:
            raise ValueError("invalid structured text")
    elif kind == "integer" and type(value) is not int:
        raise ValueError("invalid structured integer")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError("invalid structured enum")


def validate(value, candidate):
    report = value.get("goal_review")
    validate_schema(report, GOAL_SCHEMA)
    bundle = candidate["payload"]["requirements"]
    if report["requirements_digest"] != bundle["digest"]:
        raise ValueError("goal review fixed requirements digest mismatch")
    known = {g["id"] for g in bundle.get("goals", [])}
    for assessment in report["assessments"].values():
        if set(assessment["goal_ids"]) - known or not assessment["evidence"].strip():
            raise ValueError("goal review needs source-backed IDs and evidence")
    if value["verdict"] == "APPROVE" and any(a["status"] != "ALIGNED" for a in report["assessments"].values()):
        raise ValueError("goal violation or unknown evidence cannot approve")
    if value["verdict"] == "APPROVE" and not known <= {g for a in report["assessments"].values() for g in a["goal_ids"]}:
        raise ValueError("explicit global goals are missing from review")
    finding_ids = {f["id"] for f in value["blocking"]}
    if {c["finding_id"] for c in report["root_causes"]} != finding_ids:
        raise ValueError("each blocking finding requires an evidence-backed classification")
    refs = known | {d["name"] for d in bundle["documents"]} | {"task:instruction", "task:contract"}
    for cause in report["root_causes"]:
        if cause["finding_id"] not in finding_ids:
            raise ValueError("root cause must identify a blocking finding")
        if (not set(cause["requirement_refs"]) <= refs or not cause["requirement_refs"]
            or any(not cause[k].strip() for k in ("reproduction", "observed", "expected", "counterexample"))
            or not cause["evidence_refs"]):
            raise ValueError("root cause requires reproduction, counterexample and fixed requirement evidence")
    return report


def fields(candidate):
    from .providers import REVIEW_SCHEMA
    return REVIEW_SCHEMA["required"] + (["goal_review"] if required(candidate) else [])


def terminal_fields(result, candidate):
    return {k: result[k] for k in fields(candidate)}


REVIEW_INSTRUCTIONS = """
For requirements.version >= 2 return goal_review version 2 with the exact requirements digest.
Evaluate task_correctness, goal_alignment, architectural_consistency and evidence_completeness independently.
A task may implement only part of a workflow goal: incompleteness of that overall goal alone is not a violation.
Reject prohibited assumptions or architectural contradictions even when individual tests pass.
Use only supplied explicit goal IDs. Free-form COMMON remains binding; do not invent owner-approved goals.
Each axis needs source-specific evidence and ALIGNED, VIOLATION or UNKNOWN. UNKNOWN cannot APPROVE.
Classify each blocking cause using IMPLEMENTATION_DEFECT, TASK_ASSUMPTION_FALSE, TASK_CONTRACT_CONFLICT,
MISSING_PREREQUISITE, ENVIRONMENT_FAILURE or INCONCLUSIVE. Supply reproduction, observed/expected results,
fixed requirement references, a counterexample and evidence_refs (validation ID or snapshot:path:line).
An assertion by an implementer is not proof. A test/transport/quota failure alone is not a faulty task premise.
"""
