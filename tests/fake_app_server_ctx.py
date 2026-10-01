"""A scriptable stand-in for `codex app-server` for the Context Efficiency tests (message shapes of codex-cli 0.159.2).

Each turn plays the next entry of $FAKE_CODEX_STATE/script.json ({"turns": [[action, ...], ...]}); when the script is
exhausted a turn is one small request (input 500, cached 450). Actions:

  {"a": "usage", "input": n, "cached": n, "write": n, "output": n}   one model request (thread/tokenUsage/updated)
  {"a": "item", "item": {...}}                                         item/completed
  {"a": "error", "error": {...}, "willRetry": bool}                    an `error` notification
  {"a": "wait_interrupt", "max": seconds}                              run until turn/interrupt (then the turn is interrupted)
  {"a": "complete", "status": "completed|failed|interrupted", "error": {...}}   the final turn/completed (default: completed)

Every request is logged to invocations.jsonl; config.json (optional) is what config/read returns as `config`.
"""
import json
import os
import sys
import threading
import time
import uuid

STATE = os.environ["FAKE_CODEX_STATE"]
os.makedirs(os.path.join(STATE, "threads"), exist_ok=True)
OUT = threading.Lock()
ACTIVE = {}
SCRIPT_LOCK = threading.Lock()


def emit(obj):
    with OUT:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def note(method, params):
    with open(os.path.join(STATE, "invocations.jsonl"), "a") as f:
        f.write(json.dumps({"method": method, "params": params}) + "\n")


def tpath(tid):
    return os.path.join(STATE, "threads", tid + ".json")


def load(tid):
    with open(tpath(tid)) as f:
        return json.load(f)


def save(tid, data):
    with open(tpath(tid), "w") as f:
        json.dump(data, f)


def notify(method, **params):
    emit({"method": method, "params": params})


def next_script(compact):
    with SCRIPT_LOCK:
        path = os.path.join(STATE, "script.json")
        data = json.load(open(path)) if os.path.exists(path) else {"turns": []}
        turns = data.get("turns", [])
        script = turns.pop(0) if turns else None
        json.dump({"turns": turns}, open(path, "w"))
    if script is not None:
        return script
    if compact:
        return [{"a": "item", "item": {"type": "contextCompaction", "id": "c1"}}, {"a": "usage", "input": 300, "cached": 300, "output": 10}]
    return [{"a": "usage", "input": 500, "cached": 450, "output": 10}]


def usage(tid, turn_id, act):
    t = load(tid)
    inp, cached, write, out = act.get("input", 500), act.get("cached", 0), act.get("write", 0), act.get("output", 10)
    for k, v in (("inputTokens", inp), ("cachedInputTokens", cached), ("cacheWriteInputTokens", write), ("outputTokens", out)):
        t["total"][k] = t["total"].get(k, 0) + v
    t["total"]["totalTokens"] = t["total"]["inputTokens"] + t["total"]["outputTokens"]
    t["total"].setdefault("reasoningOutputTokens", 0)
    save(tid, t)
    notify("thread/tokenUsage/updated", threadId=tid, turnId=turn_id, tokenUsage={
        "total": t["total"],
        "last": {"totalTokens": act.get("context", inp + out), "inputTokens": inp, "cachedInputTokens": cached,
                 "cacheWriteInputTokens": write, "outputTokens": out, "reasoningOutputTokens": 0},
        "modelContextWindow": act.get("window", 258400)})


def run_turn(tid, turn_id, compact):
    st = ACTIVE[tid]
    notify("turn/started", threadId=tid, turn={"id": turn_id, "items": [], "status": "inProgress"})
    status, error = "completed", None
    for act in next_script(compact):
        kind = act["a"]
        if kind == "usage":
            usage(tid, turn_id, act)
        elif kind == "item":
            notify("item/completed", threadId=tid, turnId=turn_id, item=act["item"])
        elif kind == "error":
            notify("error", threadId=tid, turnId=turn_id, error=act["error"], willRetry=act.get("willRetry", False))
        elif kind == "wait_interrupt":
            deadline = time.time() + act.get("max", 10)
            while time.time() < deadline and not st["interrupted"]:
                time.sleep(0.05)
            if st["interrupted"]:
                status = "interrupted"
                break
        elif kind == "complete":
            status, error = act.get("status", "completed"), act.get("error")
        if st["interrupted"] and kind != "wait_interrupt":
            status = "interrupted"
            break
        time.sleep(act.get("delay", 0.01))
    notify("thread/status/changed", threadId=tid, status={"type": "idle"})
    notify("turn/completed", threadId=tid, turn={"id": turn_id, "items": [], "status": status, "error": error, "durationMs": 5})
    ACTIVE.pop(tid, None)


def handle(msg):
    method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method == "initialized":
        return
    note(method, params)
    reply = lambda result: emit({"id": rid, "result": result})  # noqa: E731
    fail = lambda text: emit({"id": rid, "error": {"code": -32000, "message": text}})  # noqa: E731
    if method == "initialize":
        reply({"userAgent": "fake-codex-ctx/0", "codexHome": STATE})
    elif method == "account/read":
        reply({"account": {"type": "chatgpt"}, "requiresOpenaiAuth": True})
    elif method == "account/rateLimits/read":
        reply({"ordinaryUsageAllowed": True, "rateLimits": {"limitId": "codex", "planType": "pro", "rateLimitReachedType": None,
               "primary": {"usedPercent": 10, "windowDurationMins": 300, "resetsAt": 1900000000}, "secondary": None}})
    elif method == "config/read":
        path = os.path.join(STATE, "config.json")
        reply({"config": json.load(open(path)) if os.path.exists(path) else {"mcp_servers": {}}, "origins": {}})
    elif method == "thread/start":
        tid = "thr-" + uuid.uuid4().hex[:8]
        save(tid, {"turns": 0, "total": {"inputTokens": 0, "cachedInputTokens": 0, "outputTokens": 0, "totalTokens": 0}})
        reply({"thread": {"id": tid}, "model": params.get("model") or "gpt-6.1-sol"})
    elif method == "thread/resume":
        tid = params.get("threadId", "")
        if not os.path.exists(tpath(tid)):
            return fail(f"no rollout found for thread id {tid}")
        reply({"thread": {"id": tid}, "model": params.get("model") or "gpt-6.1-sol"})
    elif method in ("turn/start", "thread/compact/start"):
        tid = params["threadId"]
        if tid in ACTIVE:
            return fail("a turn is already active")
        turn_id = "turn-" + uuid.uuid4().hex[:8]
        ACTIVE[tid] = {"turn": turn_id, "interrupted": False}
        if method == "turn/start":
            reply({"turn": {"id": turn_id, "items": [], "status": "inProgress"}})
        else:
            reply({})
        threading.Thread(target=run_turn, args=(tid, turn_id, method != "turn/start"), daemon=True).start()
    elif method == "turn/interrupt":
        st = ACTIVE.get(params["threadId"])
        if st:
            st["interrupted"] = True
        reply({})
    else:
        fail(f"unsupported in the fake: {method}")


for line in sys.stdin:
    if line.strip():
        handle(json.loads(line))
