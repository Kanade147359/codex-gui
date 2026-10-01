"""Stand-in for `codex exec --json -`: reads the prompt from stdin and behaves per its first word.

ok | fail | sleep | stubborn | meet <mine> <theirs> <dir>
"""
import json
import os
import signal
import sys
import time

prompt = sys.stdin.read().split()
mode = prompt[0] if prompt else "ok"


def emit(obj):
    print(json.dumps(obj), flush=True)


emit({"type": "thread.started", "thread_id": "t-1"})
if mode == "ok":
    open("out.txt", "w").write("hello\n")
    print("this line is not json", flush=True)
    sys.stderr.write("a warning\n")
    emit({"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": "done"}})
    emit({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 2}})
elif mode == "fail":
    emit({"type": "turn.failed", "error": {"message": "boom"}})
    sys.exit(3)
elif mode in ("sleep", "stubborn"):
    if mode == "stubborn":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    emit({"type": "turn.started"})
    time.sleep(60)
elif mode == "meet":
    # Proves parallelism: each side waits for the other's flag file, so serial execution deadlocks.
    mine, theirs, d = prompt[1:4]
    open(os.path.join(d, mine), "w").close()
    for _ in range(100):
        if os.path.exists(os.path.join(d, theirs)):
            emit({"type": "turn.completed", "usage": {}})
            sys.exit(0)
        time.sleep(0.1)
    sys.exit(1)
