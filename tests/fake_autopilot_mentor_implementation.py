"""Fixed subprocess fixture: every corrective turn changes C.txt and preserves its thread."""

import json
import os
from pathlib import Path
import sys
import uuid

prompt = sys.stdin.read()
resume = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "resume" else None
thread = resume or "implementation-" + uuid.uuid4().hex
root = Path.cwd()
target = root / "C.txt"
round_number = int(target.read_text().strip().split()[-1]) + 1 if target.exists() else 1
target.write_text(f"fixture revision {round_number}\n")
record = dict(node="C", thread=thread, resume=resume, cwd=str(root), prompt=prompt)
with (Path(os.environ["FAKE_CODEX_STATE"]) / "autopilot-implementations.jsonl").open("a") as stream:
    stream.write(json.dumps(record) + "\n")
for event in (
    dict(type="thread.started", thread_id=thread),
    dict(type="turn.started"),
    dict(type="item.completed", item=dict(type="agent_message", id="final",
         text=json.dumps(dict(status="SUCCESS", reason="Synthetic implementation revision complete")))),
    dict(type="turn.completed", usage=dict(input_tokens=100, cached_input_tokens=0, output_tokens=10)),
):
    print(json.dumps(event), flush=True)
