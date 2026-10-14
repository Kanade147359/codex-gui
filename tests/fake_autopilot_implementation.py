"""Isolated subprocess fixture using the real CodexRunner exec protocol; no model calls."""

import json
import os
from pathlib import Path
import sys
import subprocess
import uuid

prompt = sys.stdin.read()
argv = sys.argv[1:]
resume = argv[1] if argv and argv[0] == "resume" else None
thread = resume or "implementation-" + uuid.uuid4().hex
state = Path(os.environ["FAKE_CODEX_STATE"])
metadata = state / (thread + "-implementation.json")
if resume:
    node = json.loads(metadata.read_text())["node"]
else:
    node = prompt.splitlines()[0].split()[-1]
    metadata.write_text(json.dumps(dict(node=node)))


def emit(value):
    print(json.dumps(value), flush=True)


emit(dict(type="thread.started", thread_id=thread))
emit(dict(type="turn.started"))
with (state / "autopilot-implementations.jsonl").open("a") as f:
    f.write(json.dumps(dict(node=node, thread=thread, resume=resume, cwd=os.getcwd(), prompt=prompt)) + "\n")
root = Path.cwd()
if node == "D":
    assert all((root / (p + ".txt")).read_text() == "good\n" for p in ("A", "B", "C")), (
        "pinned integration input missing"
    )
if node in ("I1", "I2"):
    for p in root.glob("*.txt"):
        if "<<<<<<<" in p.read_text():
            p.write_text("good\n")
            subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "add", "--", str(p)], check=True)
else:
    (root / (node + ".txt")).write_text("bad\n" if node == "A" and not resume else "good\n")
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
