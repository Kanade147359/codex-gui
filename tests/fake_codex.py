"""Stand-in for `codex exec --json [resume <thread>] -`: reads the prompt from stdin and behaves per its first word.

ok | fail | sleep | stubborn | nothread | newthread | meet <mine> <theirs> <dir>
crash        thread.started, then the process kills itself (SIGKILL) -- the first time per test only
crashloop    like crash, but every time (a task that can never be recovered)
crashearly   like crash but before thread.started (no thread id exists) -- first time per test only
crashunstarted  thread.started, then kills itself before any turn.started -- the first time per test only
wipcrash     writes an uncommitted wip.txt, thread.started, then kills itself -- first time per test only
gate <file>  waits until <file> exists (a test opens the gate), then behaves like ok
err <code> <text...>   writes <text> to stderr and exits with <code> (thread.started first)

$FAKE_CODEX_FORCE_MODE (e.g. "crash", "err 1 401 Unauthorized") replaces the mode of EVERY invocation, so that a retry
(whose prompt is the fixed recovery instruction, a plain "ok" here) can be made to fail too.

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
raw_prompt = sys.stdin.read().split("\n\nAt the end return only JSON", 1)[0]
prompt = raw_prompt.split()
logged_prompt = list(prompt)
if os.environ.get("FAKE_CODEX_FORCE_MODE"):
    prompt = os.environ["FAKE_CODEX_FORCE_MODE"].split()
mode = prompt[0] if prompt else "ok"


def once(name):
    """True the first time `name` is asked for in this test (a marker file in the state dir)."""
    marker = os.path.join(os.environ["FAKE_CODEX_STATE"], "once-" + name)
    if os.path.exists(marker):
        return False
    open(marker, "w").close()
    return True


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
    f.write(json.dumps({"argv": argv, "cwd": os.getcwd(), "prompt": " ".join(logged_prompt)}) + "\n")

thread = resume or "thread-" + uuid.uuid4().hex[:8]
if mode == "newthread":  # a CLI that ignores the resume request
    thread = "thread-" + uuid.uuid4().hex[:8]
if mode == "crashearly" and once("crashearly"):
    os.kill(os.getpid(), signal.SIGKILL)
if mode != "nothread":
    emit({"type": "thread.started", "thread_id": thread})
if mode == "wipcrash" and once("wipcrash"):
    emit({"type": "turn.started"})
    open("wip.txt", "w").write("work in progress\n")
    os.kill(os.getpid(), signal.SIGKILL)
if (mode == "crash" and once("crash")) or mode == "crashloop":
    emit({"type": "turn.started"})  # the turn was under way when the process died
    os.kill(os.getpid(), signal.SIGKILL)
if mode == "crashunstarted" and once("crashunstarted"):  # thread.started only: the instruction may never have reached the thread
    os.kill(os.getpid(), signal.SIGKILL)
if mode == "err":
    emit({"type": "turn.started"})
    sys.stderr.write(" ".join(prompt[2:]) + "\n")
    sys.exit(int(prompt[1]))
if mode == "gate":
    for _ in range(300):
        if os.path.exists(prompt[1]):
            break
        time.sleep(0.1)
    mode = "ok"
# "Previous ..." is the fixed recovery instruction of a retry; the other crash modes behave normally after their first crash.
if mode == "semantic":
    outcome = prompt[1]
    reason = " ".join(prompt[2:]) or "semantic test outcome"
    if outcome == "SUCCESS":
        open("out.txt", "a").write("hello\n")
    text = json.dumps({"status": outcome, "reason": reason}) if outcome != "MISSING" else "done"
    emit({"type": "item.completed", "item": {"id": "final", "type": "agent_message", "text": text}})
    report_usage()
elif mode in ("ok", "nothread", "newthread", "Previous", "crash", "crashearly", "wipcrash", "crashunstarted"):
    open("out.txt", "a").write("hello\n")
    print("this line is not json", flush=True)
    sys.stderr.write("a warning\n")
    emit({"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": "done"}})
    emit({"type": "item.completed", "item": {"id": "final", "type": "agent_message",
          "text": json.dumps({"status": "SUCCESS", "reason": "Requested work completed."})}})
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
