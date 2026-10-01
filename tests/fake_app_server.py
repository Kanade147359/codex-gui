"""Stand-in for `codex app-server` (JSON-RPC lines over stdio), with the message shapes seen from codex-cli 0.159.2.

A turn's behaviour is chosen by the first word of the prompt:

  ok       (default) one agent message, two token-usage updates, completes
  fail     an `error` notification and a failed turn
  quota    like fail, but codexErrorInfo is usageLimitExceeded
  sleep    runs until interrupted (-> interrupted) or steered (-> completes, recording the steered text)
  hang     like sleep, but ignores interrupts (the GUI must give up on its own)
  die      the process exits in the middle of the turn
  dieearly the process exits right after turn/start was answered, before turn/started -- the first time per test only

State lives in $FAKE_CODEX_STATE: threads/<id>.json (running totals, so a resume continues them) and
invocations.jsonl (every request the server received, for the tests). A test may write rate.json (the body of
account/rateLimits/read) to control the limits. Each turn adds input 1000 (cached 0 on a thread's first turn, 900
after), output 20; the context size reported is 1000 x turns of the thread; the window is 10000.
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
ACTIVE = {}  # thread id -> {"turn", "interrupted", "steered"}


def emit(obj):
    with OUT:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def once(name):
    """True the first time `name` is asked for in this test (a marker file in the state dir)."""
    marker = os.path.join(STATE, "once-" + name)
    if os.path.exists(marker):
        return False
    open(marker, "w").close()
    return True


def note(method, params):
    with open(os.path.join(STATE, "invocations.jsonl"), "a") as f:
        f.write(json.dumps({"method": method, "params": params}) + "\n")


def thread_path(tid):
    return os.path.join(STATE, "threads", tid + ".json")


def load(tid):
    with open(thread_path(tid)) as f:
        return json.load(f)


def save(tid, data):
    with open(thread_path(tid), "w") as f:
        json.dump(data, f)


def notify(method, **params):
    emit({"method": method, "params": params})


def usage_update(tid, turn_id, add_input, add_cached):
    t = load(tid)
    for k, v in (("inputTokens", add_input), ("cachedInputTokens", add_cached), ("outputTokens", 10)):
        t["total"][k] += v
    t["total"]["totalTokens"] = t["total"]["inputTokens"] + t["total"]["outputTokens"]
    t["total"].setdefault("cacheWriteInputTokens", 0)
    t["total"].setdefault("reasoningOutputTokens", 0)
    save(tid, t)
    notify("thread/tokenUsage/updated", threadId=tid, turnId=turn_id, tokenUsage={
        "total": t["total"],
        "last": {"totalTokens": 1000 * (t["turns"] + 1), "inputTokens": add_input, "cachedInputTokens": add_cached,
                 "outputTokens": 10, "cacheWriteInputTokens": 0, "reasoningOutputTokens": 0},
        "modelContextWindow": 10000})


def run_turn(tid, turn_id, prompt, compact=False):
    words = prompt.split()
    mode = words[0] if words else "ok"
    st = ACTIVE[tid]
    t = load(tid)
    first = t["turns"] == 0
    notify("thread/status/changed", threadId=tid, status={"type": "active", "activeFlags": []})
    notify("turn/started", threadId=tid, turn={"id": turn_id, "items": [], "status": "inProgress"})
    status, error = "completed", None
    if compact:
        notify("item/started", threadId=tid, turnId=turn_id, item={"type": "contextCompaction", "id": "c1"})
        usage_update(tid, turn_id, 300, 300)
        notify("item/completed", threadId=tid, turnId=turn_id, item={"type": "contextCompaction", "id": "c1"})
    elif mode in ("sleep", "hang"):
        for _ in range(600):
            if st["steered"]:
                break
            if st["interrupted"] and mode == "sleep":
                status = "interrupted"
                break
            time.sleep(0.1)
        else:
            status = "failed"
        if status == "completed" or st["steered"]:
            notify("item/completed", threadId=tid, turnId=turn_id,
                   item={"type": "agentMessage", "id": "m1", "text": "steered: " + " ".join(st["steered"])})
            usage_update(tid, turn_id, 1000, 0 if first else 900)
    elif mode == "die":
        os._exit(7)
    elif mode in ("fail", "quota"):
        info = "usageLimitExceeded" if mode == "quota" else "other"
        error = {"message": "You've hit your usage limit." if mode == "quota" else "boom", "codexErrorInfo": info,
                 "additionalDetails": None}
        notify("error", threadId=tid, turnId=turn_id, error=error, willRetry=False)
        status = "failed"
    else:
        notify("item/started", threadId=tid, turnId=turn_id, item={"type": "agentMessage", "id": "m1", "text": ""})
        notify("item/agentMessage/delta", threadId=tid, turnId=turn_id, itemId="m1", delta="do")
        usage_update(tid, turn_id, 500, 0 if first else 450)
        notify("item/completed", threadId=tid, turnId=turn_id, item={"type": "agentMessage", "id": "m1", "text": "done"})
        usage_update(tid, turn_id, 500, 0 if first else 450)
    t = load(tid)
    t["turns"] += 1
    save(tid, t)
    notify("thread/status/changed", threadId=tid, status={"type": "idle"})
    notify("turn/completed", threadId=tid,
           turn={"id": turn_id, "items": [], "status": status, "error": error, "durationMs": 5})
    ACTIVE.pop(tid, None)


def rate_limits():
    path = os.path.join(STATE, "rate.json")
    if os.path.exists(path):
        return json.load(open(path))
    n = sum(1 for _ in os.listdir(os.path.join(STATE, "threads")))
    turns = sum(load(f[:-5])["turns"] for f in os.listdir(os.path.join(STATE, "threads")))
    window = lambda used, mins: {"usedPercent": used, "windowDurationMins": mins, "resetsAt": 1900000000}  # noqa: E731
    return {"ordinaryUsageAllowed": True, "rateLimitResetCredits": {"availableCount": 2, "credits": []},
            "rateLimits": {"limitId": "codex", "limitName": None, "planType": "pro", "rateLimitReachedType": None,
                           "primary": window(30 + turns, 300), "secondary": window(12 + n, 10080)}}


def handle(msg):
    method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method == "initialized":
        return
    note(method, params)
    reply = lambda result: emit({"id": rid, "result": result})  # noqa: E731
    fail = lambda text: emit({"id": rid, "error": {"code": -32000, "message": text}})  # noqa: E731
    if method == "initialize":
        reply({"userAgent": "fake-codex/0", "codexHome": STATE})
    elif method == "account/read":
        kind = os.environ.get("FAKE_ACCOUNT", "chatgpt")
        if os.path.exists(os.path.join(STATE, "signed_in")):  # a completed account/login/start
            kind = "chatgpt"
        reply({"account": None if kind == "none" else {"type": kind, "email": "me@example.com", "planType": "pro"},
               "requiresOpenaiAuth": True})
    elif method == "account/login/start":
        # FAKE_LOGIN: ok (default) | fail. The browser flow completes when a test creates $STATE/browser_done.
        login_id = "login-" + uuid.uuid4().hex[:8]
        if params.get("type") == "chatgptDeviceCode":
            reply({"type": "chatgptDeviceCode", "loginId": login_id, "userCode": "ABCD-1234",
                   "verificationUrl": "https://auth.example.com/device"})
        elif params.get("type") == "chatgpt":
            reply({"type": "chatgpt", "loginId": login_id, "authUrl": "https://auth.example.com/authorize?x=1"})
        else:
            return fail("unsupported login type")
        if os.environ.get("FAKE_LOGIN") == "fail":
            notify("account/login/completed", loginId=login_id, success=False, error="access_denied")
        elif os.environ.get("FAKE_LOGIN") != "wait":
            open(os.path.join(STATE, "signed_in"), "w").close()
            notify("account/login/completed", loginId=login_id, success=True, error=None)
    elif method == "account/login/cancel":
        reply({"status": "canceled"})
    elif method == "account/rateLimits/read":
        reply(rate_limits())
    elif method == "thread/start":
        tid = "thr-" + uuid.uuid4().hex[:8]
        save(tid, {"turns": 0, "total": {"inputTokens": 0, "cachedInputTokens": 0, "outputTokens": 0, "totalTokens": 0}})
        reply({"thread": {"id": tid}, "model": params.get("model") or "fake-default", "serviceTier": params.get("serviceTier")})
    elif method == "thread/resume":
        tid = params.get("threadId", "")
        if not os.path.exists(thread_path(tid)):
            return fail(f"no rollout found for thread id {tid}")
        reply({"thread": {"id": tid}, "model": params.get("model") or "fake-default"})
    elif method == "turn/start":
        tid = params["threadId"]
        if tid in ACTIVE:
            return fail("a turn is already active")
        turn_id = "turn-" + uuid.uuid4().hex[:8]
        ACTIVE[tid] = {"turn": turn_id, "interrupted": False, "steered": []}
        prompt = "".join(i.get("text", "") for i in params["input"])
        reply({"turn": {"id": turn_id, "items": [], "status": "inProgress"}})
        if prompt.split()[:1] == ["dieearly"] and once("dieearly"):
            os._exit(7)  # the turn was acknowledged but never started: its instruction is not in the thread
        threading.Thread(target=run_turn, args=(tid, turn_id, prompt), daemon=True).start()
    elif method == "thread/compact/start":
        tid = params["threadId"]
        turn_id = "turn-" + uuid.uuid4().hex[:8]
        ACTIVE[tid] = {"turn": turn_id, "interrupted": False, "steered": []}
        reply({})
        threading.Thread(target=run_turn, args=(tid, turn_id, "", True), daemon=True).start()
    elif method == "turn/steer":
        st = ACTIVE.get(params["threadId"])
        if not st or st["turn"] != params["expectedTurnId"]:
            return fail("expected turn id does not match the active turn")
        st["steered"].append("".join(i.get("text", "") for i in params["input"]))
        reply({"turnId": st["turn"]})
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
