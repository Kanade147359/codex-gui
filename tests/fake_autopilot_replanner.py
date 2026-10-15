"""Public independent proposal fixture. Validates inputs; never edits the snapshot."""
import hashlib
import json
from pathlib import Path
import sys
import time

request = json.load(sys.stdin)
f = request["fixed"]
unsigned = {k: v for k, v in f.items() if k != "digest"}
assert hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest() == f["digest"]
assert f["requirements"]["version"] == 2 and f["requirements"]["digest"]
assert f["original_task"]["id"] == "A" and f["original_task"]["goal_ids"] == ["G-adapter"]
common = next(d for d in f["requirements"]["documents"] if d["name"].endswith("COMMON.md"))
assert "COMMON-OK" in common["body"] and "adapter-v2" in common["body"]
assert f["plan"]["permissions"] == f["permissions"]
assert f["validation_profiles"][0] == f["original_task"]["validation"]
assert f["causes"][0]["classification"] == "TASK_ASSUMPTION_FALSE"
assert f["evidence"]["snapshot:capability.txt:1"]["text"] == "adapter-v2 only\n"
assert (Path(request["root"]) / "capability.txt").read_text() == "adapter-v2 only\n"
assert f["limits"] == dict(tasks=3, source_replans=1, depth=1, workflow_nodes=200)
assert all(i["candidate_id"] and i["head_sha"] for i in f["accepted_inputs"])
if request.get("delay"):
    time.sleep(request["delay"])
value = dict(version=1, input_digest=f["digest"], candidate_sha=f["scope"]["head_sha"],
             confirmed_causes=["false-adapter"], evidence_refs=["snapshot:capability.txt:1"],
             analysis="Pinned capability.txt disproves the adapter-v1 assumption. Deliver the original contract using adapter-v2.",
             owner_changes=request.get("owner_changes", []), tasks=[dict(
                 task_id="R_adapter", title="Use available adapter", purpose="Resolve the disproven adapter-v1 premise",
                 parent_task="A", finding_id="false-adapter", root_cause="Only adapter-v2 is available",
                 goal_ids=["G-adapter"], implementation="Write the original A deliverable using adapter-v2 and COMMON-OK.",
                 paths=request.get("paths", ["A.txt"]), depends_on=f["original_task"]["depends_on"], validation_profile=0,
                 completion_conditions=["Original A contract and mandatory validation pass; retain COMMON-OK"],
                 relation="replace", rationale="Use the capability confirmed in the fixed snapshot without expanding permissions")])
print(json.dumps(dict(value, session=request["session"], provider="codex", model="fake", provider_version="synthetic fixture")))
