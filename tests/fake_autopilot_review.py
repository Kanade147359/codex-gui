"""Synthetic Claude/Codex events, not real quota observations or model quality evidence."""

import json
from pathlib import Path
import sys
import time
import uuid
import subprocess

request = json.load(sys.stdin)
candidate, provider, scenario = request["candidate"], request["provider"], request["scenario"]
payload = candidate["payload"]
received = request["received"]
assert received["candidate_id"] == candidate["id"] and received["scope"] == payload["scope"]
assert received["requirements"] == payload.get("requirements", {})
assert received["changed_paths"] == payload["changed_paths"]
name = payload.get("requirements", {}).get("node") or payload["acceptance"].splitlines()[0].split()[-1]
root = Path(request["root"])
context = received["context"]
head = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
assert head == received["scope"]["head_sha"]
if context.get("phase") == "partition" and context["partition"]["path"]:
    part = context["partition"]
    actual_diff = subprocess.check_output(
        [
            "git",
            "-C",
            str(root),
            "diff",
            "--binary",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            received["scope"]["base_sha"],
            received["scope"]["head_sha"],
            "--",
            part["path"],
        ],
    ).decode()
    assert part["diff"] in actual_diff and part["path"] in received["changed_paths"]
if context.get("phase") == "overall":
    assert context["partitions"] and all(
        entry["result"]["scope"] == received["scope"] for entry in context["partitions"]
    )
verdict, blocking = "APPROVE", []
if scenario.get("delay"):
    time.sleep(scenario["delay"])
if name == "A" and (root / "A.txt").read_text() == "bad\n":
    verdict = "CHANGES_REQUESTED"
    blocking = [
        dict(id="A-value", path="A.txt", line=1, reason="Acceptance requires good, fixture currently writes bad")
    ]
common = [doc for doc in received["requirements"].get("documents", []) if doc["name"].endswith("COMMON.md")]
if common and "COMMON-OK" in common[0]["body"] and name in ("A", "B", "C", "D"):
    if "COMMON-OK" not in (root / (name + ".txt")).read_text():
        verdict = "CHANGES_REQUESTED"
        blocking = [
            dict(
                id="COMMON-required",
                path=name + ".txt",
                line=1,
                reason="COMMON.md alone requires COMMON-OK; implementation omitted it",
            )
        ]
for doc in received["requirements"].get("documents", []):
    assert (root / ".autopilot-inputs" / doc["name"]).read_bytes().decode() == doc["body"]
if scenario.get("always_changes") and (not scenario.get("changes_only") or name in scenario["changes_only"]):
    verdict = "CHANGES_REQUESTED"
    blocking = [dict(id="persistent", path=name + ".txt", line=1, reason="Persistent fixture finding")]
if scenario.get("integration_changes") and name == "I1" and not (root / "integration-fixed.txt").exists():
    verdict = "CHANGES_REQUESTED"
    blocking = [
        dict(id="integration-marker", path="integration-fixed.txt", line=1, reason="Add integration-fixed marker")
    ]
result = dict(
    candidate_id=candidate["id"],
    run_generation=candidate["run_generation"],
    scope=payload["scope"],
    verdict=verdict,
    blocking=blocking,
    nonblocking=[],
    coverage=payload["changed_paths"] or ["README.md"],
    unverified=[],
    resolved=payload.get("unresolved", []) if not blocking else [],
)
if received["requirements"].get("version", 0) >= 2:
    import hashlib
    bundle = received["requirements"]
    unsigned = {k:v for k,v in bundle.items() if k != "digest"}
    assert hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest() == bundle["digest"]
    axes = ("task_correctness", "goal_alignment", "architectural_consistency", "evidence_completeness")
    result["goal_review"] = dict(version=2, requirements_digest=bundle["digest"],
        assessments={axis:dict(status="ALIGNED", goal_ids=[g["id"] for g in bundle.get("goals", [])],
            evidence="Inspected fixed COMMON/rules and partition against task contract; partial goal delivery is allowed.") for axis in axes}, root_causes=[])
    if blocking:
        axis = "goal_alignment" if blocking[0]["id"] == "COMMON-required" else "task_correctness"
        result["goal_review"]["assessments"][axis].update(status="VIOLATION", evidence=blocking[0]["reason"])
        result["goal_review"]["root_causes"] = [dict(finding_id=f["id"], classification="IMPLEMENTATION_DEFECT",
            reproduction="Inspect the pinned diff and deliverable against the original contract", observed=f["reason"],
            expected="Deliverable satisfies the fixed task and COMMON requirements", requirement_refs=["task:contract"],
            counterexample=f["reason"], evidence_refs=["snapshot:" + f["path"] + ":1"]) for f in blocking]
    if scenario.get("premise") and name == "A" and candidate["generation"] >= scenario.get("premise_generation", 2):
        assert common and "adapter-v2" in common[0]["body"]
        instruction = next(d for d in bundle["documents"] if d["name"].endswith("A.md"))
        assert "adapter-v1" in instruction["body"]
        assert (root / "capability.txt").read_text() == "adapter-v2 only\n"
        result.update(verdict="CHANGES_REQUESTED", blocking=[dict(id="false-adapter", path="capability.txt", line=1,
            reason="Task assumes adapter-v1; the pinned repository supports only adapter-v2")])
        result["goal_review"]["assessments"]["task_correctness"].update(status="VIOLATION", evidence="capability.txt:1 contradicts task assumption")
        result["goal_review"]["root_causes"] = [dict(finding_id="false-adapter", classification="TASK_ASSUMPTION_FALSE",
            reproduction="Read capability.txt in the pinned candidate snapshot", observed="adapter-v2 only",
            expected="The original task assumes adapter-v1 is available", requirement_refs=[instruction["name"]],
            counterexample="capability.txt:1 declares adapter-v2 only", evidence_refs=["snapshot:capability.txt:1"])]
if payload.get("read_protocol"):
    result["requested_reads"] = []
if scenario.get("ignore_prior_findings"):
    result["resolved"] = []
if scenario.get("error"):
    print(json.dumps(dict(fixture_error=scenario["error"])))
elif provider == "claude" and (scenario.get("quota") or name == "B"):
    print(
        json.dumps(
            dict(
                type="rate_limit_event",
                rate_limit_info=dict(
                    status="rejected", isUsingOverage=False, rateLimitType="five_hour", resetsAt=time.time() + 3600
                ),
            )
        )
    )
    if scenario.get("partial_findings"):
        result["verdict"] = "CHANGES_REQUESTED"
        result["blocking"] = [
            dict(id="carry-forward", path=name + ".txt", line=1, reason="Retain finding before quota interruption")
        ]
        if "goal_review" in result:
            result["goal_review"]["root_causes"] = [dict(finding_id="carry-forward", classification="IMPLEMENTATION_DEFECT",
                reproduction="Inspect the fixture finding before quota", observed="Unresolved carry-forward finding",
                expected="Finding resolved with evidence", requirement_refs=["task:contract"],
                counterexample="carry-forward remains unresolved", evidence_refs=["snapshot:" + name + ".txt:1"])]
    print(json.dumps(dict(type="result", subtype="error_during_execution", is_error=True, structured_output=result)))
else:
    print(
        json.dumps(
            dict(
                type="result",
                subtype="success",
                is_error=False,
                session_id="review-" + uuid.uuid4().hex,
                structured_output=result,
            )
        )
    )
