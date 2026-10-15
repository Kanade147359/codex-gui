"""Public local fixture exercising the real TaskManager/CodexRunner protocol."""

import json
import os
from pathlib import Path
import sys
import uuid

prompt = sys.stdin.read()
args = sys.argv[1:]
resume = args[1] if args and args[0] == "resume" else None
thread = resume or "implementation-" + uuid.uuid4().hex
root = Path.cwd()
bundle = json.loads((root / ".autopilot-inputs/requirements.json").read_text())
for doc in bundle["documents"]:
    assert (root / ".autopilot-inputs" / doc["name"]).read_bytes().decode() == doc["body"]
node = bundle["node"]
with (Path(os.environ["FAKE_CODEX_STATE"]) / "goal-implementations.jsonl").open("a") as f:
    f.write(
        json.dumps(
            dict(
                node=node,
                thread=thread,
                resume=resume,
                prompt=prompt,
                requirements=bundle,
            )
        )
        + "\n"
    )
if node == "C":
    text = (
        "import json\ndef transform(text):\n    return json.loads(text)['value']\n"
        if resume
        else "def transform(text):\n    return 'good'\n"
    )
    (root / "C.py").write_text(text)
    (root / "C.txt").write_text("good\n")
elif node == "R":
    assert bundle["replan_lineage"]["invalidated_assumptions"] == ["A-json"]
    assert "json.loads" in (root / "C.py").read_text(), "missing fixed original candidate input"
    (root / "C.py").write_text("def transform(text):\n    return text.strip().split(',')[0]\n")
    (root / "C.txt").write_text("good\n")
elif node == "D":
    ns = {}
    exec((root / "C.py").read_text(), ns)
    assert ns["transform"]((root / "input.csv").read_text()) == "good"
    assert any(d["name"].startswith("handoff/") for d in bundle["documents"]), (
        "accepted artifact absent inside sandbox"
    )
    (root / "D.txt").write_text("good\n")
elif node == "G":
    (root / "G.py").write_text("MODE = 'local'\n" if resume else "MODE = 'remote'\n")
else:
    (root / (node + ".txt")).write_text("good\n")
for event in [
    dict(type="thread.started", thread_id=thread),
    dict(type="turn.started"),
    dict(
        type="item.completed",
        item=dict(
            type="agent_message",
            id="final",
            text=json.dumps(dict(status="SUCCESS", reason="Synthetic implementation complete")),
        ),
    ),
    dict(
        type="turn.completed",
        usage=dict(input_tokens=100, output_tokens=10, cached_input_tokens=0),
    ),
]:
    print(json.dumps(event), flush=True)
