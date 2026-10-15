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
from goal_fixtures import goal_fields
result.update(goal_fields(candidate,result))
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
        result.update(goal_fields(candidate, result))
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
