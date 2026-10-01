"""Stand-in for `codex exec --json [resume <thread>] -`: reads the prompt from stdin and behaves per its first word.

ok | fail | sleep | stubborn | nothread | newthread | meet <mine> <theirs> <dir>

Like the real CLI (codex-cli 0.159.2): thread.started carries the session id (a resume reports the id it was
given), and turn.completed.usage is the running total of the thread. Per-thread totals are kept in
$FAKE_CODEX_STATE/<thread>.json. Each turn adds input 100 / cached 0 (first turn of a thread) or 80 / output 10.
"""
import json
import os
import signal
import sys
import time
import uuid

argv = sys.argv[1:]
resume = argv[1] if argv[:1] == ["resume"] else None
prompt = sys.stdin.read().split()
mode = prompt[0] if prompt else "ok"


def emit(obj):
    print(json.dumps(obj), flush=True)


def report_usage():
    state = os.path.join(os.environ["FAKE_CODEX_STATE"], thread + ".json")
    total = {"input_tokens": 0, "cached_input_tokens": 0, "cache_write_input_tokens": 0,
             "output_tokens": 0, "reasoning_output_tokens": 0}
    first = not os.path.exists(state)
    if not first:
        total = json.load(open(state))
    total["input_tokens"] += 100
    total["cached_input_tokens"] += 0 if first else 80
    total["output_tokens"] += 10
    json.dump(total, open(state, "w"))
    emit({"type": "turn.completed", "usage": total})


# Evidence for the tests of what the process was started with.
with open(os.path.join(os.environ["FAKE_CODEX_STATE"], "invocations.jsonl"), "a") as f:
    f.write(json.dumps({"argv": argv, "cwd": os.getcwd(), "prompt": " ".join(prompt)}) + "\n")

thread = resume or "thread-" + uuid.uuid4().hex[:8]
if mode == "newthread":  # a CLI that ignores the resume request
    thread = "thread-" + uuid.uuid4().hex[:8]
if mode != "nothread":
    emit({"type": "thread.started", "thread_id": thread})
if mode in ("ok", "nothread", "newthread"):
    open("out.txt", "a").write("hello\n")
    print("this line is not json", flush=True)
    sys.stderr.write("a warning\n")
    emit({"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": "done"}})
    report_usage()
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
