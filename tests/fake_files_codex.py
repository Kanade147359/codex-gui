"""Isolated file-reading Codex stand-in for browser/API lifecycle tests, never production."""
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid

raw = sys.stdin.read()
thread = sys.argv[2] if sys.argv[1:2] == ['resume'] else 'files-' + uuid.uuid4().hex
print(json.dumps({'type': 'thread.started', 'thread_id': thread}), flush=True)
print(json.dumps({'type': 'turn.started'}), flush=True)
if raw.startswith('gate '):
    gate = raw.split()[1]
    deadline = time.monotonic() + 20
    while not Path(gate).exists() and time.monotonic() < deadline:
        time.sleep(.02)
marker = 'Inputs are stored outside the Git worktree; do not add them to commits unless explicitly requested:\n'
if marker in raw:
    manifest = json.JSONDecoder().raw_decode(raw.split(marker, 1)[1])[0]
    for index, item in enumerate(manifest):
        data = Path(item['path']).read_bytes()
        assert hashlib.sha256(data).hexdigest() == item['sha256']
        Path(f'received-{index}.bin').write_bytes(data)
        Path('verification.json').write_bytes(data)
print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '{"status":"SUCCESS","reason":"Files read and copied"}'}}), flush=True)
print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 100, 'cached_input_tokens': 0, 'output_tokens': 10}}), flush=True)
