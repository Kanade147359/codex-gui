"""Watches the app-server notifications of ONE turn and collects what Context Efficiency needs: the usage of every model
request (input / cache read / cache write / output), tool calls and the size of their outputs, compactions Codex ran
by itself, and the conditions the retry guard must stop for. It decides nothing about the task: `feed()` returns an
interrupt request only for a stop the guard owns (an error Codex would keep retrying, or the same tool failing again
and again); everything else is recorded for the UI.
"""
import json
from datetime import datetime, timezone
from typing import Optional

from .cache_health import Thresholds
from .ctx_guard import STOP_MESSAGES, RepeatFailureDetector, context_jump, stop_reason_for_error, tool_output_record

TOOL_ITEMS = frozenset({"commandExecution", "mcpToolCall", "fileChange", "webSearch", "dynamicToolCall", "collabAgentToolCall",
                        "imageGeneration", "imageView"})
MAX_LISTED_OUTPUTS = 50


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _int(v) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


def error_kind_of(error) -> str:
    """The name of a TurnError's codexErrorInfo ("contextWindowExceeded", "httpConnectionFailed", ...), or ""."""
    info = error.get("codexErrorInfo") if isinstance(error, dict) else None
    if isinstance(info, str):
        return info
    return next(iter(info)) if isinstance(info, dict) and info else ""


class TurnObserver:
    def __init__(self, th: Thresholds, cap_tokens: Optional[int] = None, prev_request_input: Optional[int] = None, now=_now):
        self.th, self.cap, self._now = th, cap_tokens, now
        self.requests: list[dict] = []
        self.tool_calls = 0
        self.large_outputs = 0          # tool outputs at or above the "warn" size
        self.tool_output_tokens_est = 0  # what the model got back, summed (after the cap)
        self.outputs: list[dict] = []   # the big ones, for Task Detail
        self.compactions = 0
        self.events: list[dict] = []    # {"kind", "severity", "message", "data"}; the manager adds ts and turn
        self.cache_activity_at: Optional[str] = None
        self.stop: Optional[dict] = None  # {"reason", "message"}: a stop the guard asked for
        self.error_reason = ""          # retry-guard reason of the last Codex error, if it was one that must not be retried
        self._prev_input = prev_request_input
        self._pending_large: Optional[dict] = None
        self._last_total: Optional[int] = None
        self._repeat = RepeatFailureDetector(th.repeat_failure_limit)

    # ---------------------------------------------------------------- feed

    def feed(self, method: str, params: dict) -> Optional[dict]:
        """One notification of the turn. Returns {"interrupt": reason, "message": text} when the turn must be stopped."""
        if method == "thread/tokenUsage/updated":
            self._usage(params.get("tokenUsage"))
        elif method == "item/completed":
            return self._item(params.get("item"))
        elif method == "error" and isinstance(params.get("error"), dict):
            return self._error(params)
        return None

    def _usage(self, usage) -> None:
        if not isinstance(usage, dict) or not isinstance(usage.get("last"), dict) or not isinstance(usage.get("total"), dict):
            return
        total = usage["total"].get("totalTokens")
        if total is not None and total == self._last_total:
            return  # the same update twice (or compaction bookkeeping): one model request is counted once
        self._last_total = total
        last = usage["last"]
        req = {"i": _int(last.get("inputTokens")), "c": _int(last.get("cachedInputTokens")),
               "w": _int(last.get("cacheWriteInputTokens")), "o": _int(last.get("outputTokens"))}
        self.requests.append(req)
        if req["c"] or req["w"]:
            self.cache_activity_at = self._now()  # the last successful cache write or reuse
        jump = context_jump(self._prev_input, req["i"], self._pending_large, self.th)
        if jump:
            self.events.append({"kind": jump["kind"], "severity": jump["severity"], "message": jump["message"],
                                "data": {"grew_tokens": jump["grew_tokens"], "input": req["i"]}})
        self._prev_input, self._pending_large = req["i"], None

    def _item(self, item) -> Optional[dict]:
        if not isinstance(item, dict):
            return None
        kind = item.get("type")
        if kind == "contextCompaction":
            self.compactions += 1
            return None
        if kind not in TOOL_ITEMS:
            return None
        self.tool_calls += 1
        rec = tool_output_record(item, self.cap, self.th)
        if rec is None:
            return None
        self.tool_output_tokens_est += rec["model_tokens_est"]
        if rec["size"] != "normal":
            self.large_outputs += 1
            if len(self.outputs) < MAX_LISTED_OUTPUTS:
                self.outputs.append({k: rec[k] for k in ("tool", "label", "raw_tokens_est", "model_tokens_est", "truncated_for_model", "size")})
            if not self._pending_large or rec["model_tokens_est"] >= self._pending_large["model_tokens_est"]:
                self._pending_large = rec
            self.events.append({
                "kind": "large_tool_output", "severity": "warning" if rec["size"] == "large" else "info",
                "message": f"Tool output of ~{rec['raw_tokens_est']:,} tokens ({rec['label'][:80]})" +
                           (f"; the model saw ~{rec['model_tokens_est']:,}" if rec["truncated_for_model"] else ""),
                "data": {k: rec[k] for k in ("label", "raw_tokens_est", "model_tokens_est", "truncated_for_model", "size")}})
        stop = self._repeat.feed(rec)
        if stop and not self.stop:
            self.stop = {"reason": stop["reason"], "message": stop["message"]}
            self.events.append({"kind": "retry_guard", "severity": "critical", "message": stop["message"],
                                "data": {"reason": stop["reason"], "count": stop["count"]}})
            return {"interrupt": stop["reason"], "message": stop["message"]}
        return None

    def _error(self, params: dict) -> Optional[dict]:
        reason = stop_reason_for_error(error_kind_of(params["error"]))
        if not reason:
            return None
        if not params.get("willRetry"):
            self.error_reason = reason  # the turn ends with this error by itself
            return None
        if self.stop:
            return None
        # Codex announced that it will retry something a retry cannot fix (resending the same context, a failed login,
        # no quota left): stop the turn instead of letting it loop.
        self.stop = {"reason": reason, "message": STOP_MESSAGES[reason]}
        self.error_reason = reason
        self.events.append({"kind": "retry_guard", "severity": "critical", "message": STOP_MESSAGES[reason], "data": {"reason": reason}})
        return {"interrupt": reason, "message": STOP_MESSAGES[reason]}

    # ---------------------------------------------------------------- result

    @property
    def stop_reason(self) -> str:
        return (self.stop or {}).get("reason") or self.error_reason

    def turn_fields(self) -> dict:
        """Columns of the turns row."""
        return {"requests": len(self.requests), "max_request_input": max((r["i"] for r in self.requests), default=None),
                "requests_json": json.dumps(self.requests, separators=(",", ":")) if self.requests else None,
                "tool_calls": self.tool_calls, "large_tool_outputs": self.large_outputs,
                "tool_output_tokens_est": self.tool_output_tokens_est, "compactions": self.compactions}
