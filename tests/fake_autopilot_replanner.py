"""Independent process fixture. Checks the actual frozen packet, never resumes implementation."""

import hashlib
import json
from pathlib import Path
import sys
import uuid
import subprocess

r = json.load(sys.stdin)
p = r["packet"]
root = Path(r["root"])
scenario = r["scenario"]
assert (
    p["digest"]
    == hashlib.sha256(
        json.dumps(
            {k: v for k, v in p.items() if k != "digest"},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
)
assert p["requirements"]["digest"] == p["candidate"]["requirements_digest"]
assert p["plan"]["nodes"] and p["authority"]["policy"]["max_per_node"] == 1
for doc in p["requirements"]["documents"]:
    assert (root / ".autopilot-inputs" / doc["name"]).read_bytes().decode() == doc["body"]
if scenario.get("replan_error"):
    print(json.dumps(dict(fixture_error=scenario["replan_error"])))
    sys.exit(0)
finding = p["findings"][0]["id"]
assert (
    subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    == p["candidate"]["scope"]["head_sha"]
)
for view in p["source_context"]["related"] + p["source_context"]["requested_code"]:
    lines = (root / view["path"]).read_text().splitlines(keepends=True)
    assert "".join(lines[view["start"] - 1 : view["end"]]) == view["text"]
diag = dict(
    version=1,
    classification="IMPLEMENTATION_DEFECT",
    finding_ids=[finding],
    reproduction="Repeat registered validation on the pinned candidate.",
    observed="Implementation still fails.",
    expected="Satisfy original acceptance.",
    rationale="No evidence of an invalid premise; continue bounded repair.",
    anchors=[],
)
tasks = []
replacement = ""
if scenario.get("request_file") and not p["source_context"]["requested_code"]:
    diag["classification"] = "INCONCLUSIVE"
    print(
        json.dumps(
            dict(
                version=1,
                packet_digest=p["digest"],
                source_candidate=p["candidate"]["id"],
                scope=p["candidate"]["scope"],
                diagnosis=diag,
                tasks=[],
                replacement_task="",
                requested_changes=[],
                requested_reads=[dict(path=scenario["request_file"], start=1, end=10)],
                session="independent-read-request-" + uuid.uuid4().hex,
            )
        )
    )
    sys.exit(0)
if (root / "input.csv").exists() and p["source_node"]["name"] == "C":
    spec = p["source_node"]["spec"]
    assert spec["assumptions"][0]["id"] == "A-json"
    assert p["fix_history"] and p["validation"][-1]["state"] == "FAIL"
    assert "JSONDecodeError" in json.dumps(p["validation"][-1])
    assert (root / "input.csv").read_text() == "good,data\n"
    assert any(
        view["path"] == "input.csv" and "good,data" in view["text"]
        for view in p["source_context"]["related"] + p["source_context"]["requested_code"]
    ), "real replanner must receive the counterexample body without filesystem tools"
    diag.update(
        classification="TASK_ASSUMPTION_FALSE",
        reproduction="Run the frozen validator against input.csv after the JSON decoder repair.",
        observed="JSONDecodeError on the accepted CSV input.",
        expected="Transform the input to good while remaining local.",
        rationale="The task assumed JSON, but the immutable accepted input is CSV.",
        anchors=[
            dict(kind="source", ref="input.csv", quote="good,data"),
            dict(kind="requirement", ref="assumption:A-json", quote="Input is JSON"),
            dict(
                kind="validation",
                ref=p["validation"][-1]["id"],
                quote="JSONDecodeError",
            ),
        ],
    )
    replacement = "R"
    tasks = [
        dict(
            task_id="R",
            title="Decode the actual CSV prerequisite",
            purpose="Implement the assigned transform using the actual local CSV format.",
            parent_task="C",
            finding_ids=[finding],
            root_cause="False JSON assumption",
            goal_ids=spec["goal_ids"],
            implementation="Repair C.py to parse CSV without adding external access. Preserve the original completion and validation contract.",
            paths=spec["paths"],
            depends_on=spec["depends_on"],
            validation=spec["validation"],
            completion_contract=spec["contract"],
            acceptance_conditions=spec["acceptance_conditions"],
            relationship="replace",
            invalidated_assumptions=["A-json"],
            manual_gates=spec["manual_gates"],
            rationale="Smallest correction satisfying the same goal, acceptance and validator.",
        )
    ]
    if scenario.get("outside_envelope"):
        tasks[0]["paths"] = ["C.py", "C.txt", "outside.txt"]
    if scenario.get("weaken_validation"):
        tasks[0]["validation"] = dict(commands=[["python3", "-c", "pass"]], artifacts=["C.txt"])
    if scenario.get("drop_acceptance"):
        tasks[0]["acceptance_conditions"] = []
    if scenario.get("remove_goal"):
        tasks[0]["goal_ids"] = []
    if scenario.get("fake_quote"):
        diag["anchors"][1]["quote"] = "an invented owner permission"
value = dict(
    version=1,
    packet_digest=p["digest"],
    source_candidate=p["candidate"]["id"],
    scope=p["candidate"]["scope"],
    diagnosis=diag,
    tasks=tasks,
    replacement_task=replacement,
    requested_changes=[],
    requested_reads=[],
    session="independent-replanner-" + uuid.uuid4().hex,
)
print(json.dumps(value))
