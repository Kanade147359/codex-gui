"""Isolated subprocess fixture using the real CodexRunner exec protocol; no model calls."""

import json
import os
from pathlib import Path
import sys
import subprocess
import re
import uuid
import hashlib

prompt = sys.stdin.read()
argv = sys.argv[1:]
resume = argv[1] if argv and argv[0] == "resume" else None
thread = resume or "implementation-" + uuid.uuid4().hex
state = Path(os.environ["FAKE_CODEX_STATE"])
metadata = state / (thread + "-implementation.json")
bundle_path = Path.cwd() / ".autopilot-inputs/requirements.json"
bundle = json.loads(bundle_path.read_text()) if bundle_path.exists() else None
if bundle:
    for doc in bundle["documents"]:
        actual = (bundle_path.parent / doc["name"]).read_bytes().decode()
        assert actual == doc["body"] and hashlib.sha256(actual.encode()).hexdigest() == doc["digest"]
if resume:
    node = json.loads(metadata.read_text())["node"]
else:
    reference = re.search(r"Read immutable acceptance instructions: (\S+)", prompt)
    original = Path(reference.group(1)).read_text() if reference else prompt
    node = bundle["node"] if bundle else original.splitlines()[0].split()[-1]
    metadata.write_text(json.dumps(dict(node=node)))


def emit(value):
    print(json.dumps(value), flush=True)


emit(dict(type="thread.started", thread_id=thread))
emit(dict(type="turn.started"))
with (state / "autopilot-implementations.jsonl").open("a") as f:
    f.write(
        json.dumps(dict(node=node, thread=thread, resume=resume, cwd=os.getcwd(), prompt=prompt, requirements=bundle))
        + "\n"
    )
root = Path.cwd()
synthetic = bool(
    bundle and any("COMMON-OK" in doc["body"] for doc in bundle["documents"] if doc["name"].endswith("COMMON.md"))
)
if node == "00":
    # Public fixture plan is an ordinary body JSON fenced block, not importer metadata.
    body = next(doc["body"] for doc in bundle["documents"] if doc["name"].endswith("00.md"))
    changes = json.loads(body.split("```json\n", 1)[1].split("```", 1)[0])
    (root / "plan.json").write_text(json.dumps(changes))
    (root / "HANDOFF.md").write_text("Fixed planning handoff: COMMON-OK\n")
if node == "D":
    expected = "good\nCOMMON-OK\n" if synthetic else "good\n"
    assert all((root / (p + ".txt")).read_text() == expected for p in ("A", "B", "C")), (
        "pinned integration input missing"
    )
    if synthetic:
        assert (root / "wiring.py").read_text().startswith("def integrated")
        assert any(doc.get("relative_path") == "HANDOFF.md" for doc in bundle["documents"]) or any(
            doc.get("relative_path") == "wiring.py" for doc in bundle["documents"]
        )
if node in ("I1", "I2"):
    for p in root.glob("*.txt"):
        if "<<<<<<<" in p.read_text():
            p.write_text("good\n")
            subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "add", "--", str(p)], check=True)
    if "Add integration-fixed marker" in prompt:
        (root / "integration-fixed.txt").write_text("good\n")
    if synthetic and (not (root / ".git/MERGE_HEAD").exists()):
        (root / "wiring.py").write_text("def integrated():\n    return 'COMMON-OK'\n")
elif node != "00":
    text = "bad\n" if node == "A" and not resume and not synthetic else "good\n"
    if synthetic and (node != "A" or resume):
        text += "COMMON-OK\n"
    (root / (node + ".txt")).write_text(text)
emit(
    dict(
        type="item.completed",
        item=dict(
            type="agent_message",
            id="final",
            text=json.dumps(dict(status="SUCCESS", reason="Fixture implementation complete")),
        ),
    )
)
emit(
    dict(
        type="turn.completed",
        usage=dict(
            input_tokens=100,
            cached_input_tokens=0,
            cache_write_input_tokens=0,
            output_tokens=10,
            reasoning_output_tokens=0,
        ),
    )
)
