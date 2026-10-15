"""Synthetic structured evaluation of the actual received fixed requirements."""

import copy
import hashlib
import json


def goal_fields(candidate, result):
    p = candidate["payload"]
    if p.get("goal_review_version", 1) < 2:
        return {}
    bundle = p["requirements"]
    index = bundle["goal_index"]
    original = dict(bundle)
    original.pop("digest")
    assert (
        hashlib.sha256(
            json.dumps(original, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
        == bundle["digest"]
    )
    axis = dict(
        status="MATCH",
        goal_ids=[g["id"] for g in index["declared"]],
        acceptance_ids=[c["id"] for c in index["acceptance_conditions"]],
        source_refs=[s["ref"] for s in index["sources"]],
        evidence=[
            dict(
                source_ref="task:instruction",
                path="",
                line=0,
                reason="Checks this assigned slice; the workflow goal may remain partially implemented.",
            )
        ],
    )
    assessment = dict(
        version=1,
        requirements_digest=bundle["digest"],
        goal_index_digest=index["digest"],
        **{
            name: copy.deepcopy(axis)
            for name in (
                "task_correctness",
                "goal_alignment",
                "architectural_consistency",
                "evidence_completeness",
            )
        },
    )
    if result["verdict"] == "CHANGES_REQUESTED":
        assessment["task_correctness"]["status"] = "VIOLATION"
    return dict(
        review_version=2,
        goal_assessment=assessment,
        diagnosis=dict(
            version=1,
            classification="IMPLEMENTATION_DEFECT",
            finding_ids=[f["id"] for f in result["blocking"]],
            reproduction="Inspect fixed candidate and run the registered validation profile.",
            observed="The actual candidate and fixed requirement bodies were inspected.",
            expected="Satisfy assigned acceptance without contradicting COMMON.",
            rationale="Ordinary implementation findings stay in the original implementation thread.",
            anchors=[],
        ),
    )
