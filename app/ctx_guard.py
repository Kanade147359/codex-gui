"""Context guards: the long-context zone, tool-output records, and the retry guard. Pure logic; the TaskManager feeds
it what it sees and decides what to show. None of this ever compacts, retries, or changes a Codex setting by itself.
"""
import hashlib
import json
from typing import Optional

from . import efficiency
from .cache_health import Thresholds
from .tokens import est_tokens

# ---------------------------------------------------------------- long-context zone

ZONE_ACTIONS = ("continue", "compact", "new_session")  # what the user is offered; the GUI chooses none of them itself
# The user's bands for a 272K threshold (220K / 250K); other thresholds use the same proportions.
WARN_FRACTION, STRONG_FRACTION = 220 / 272, 250 / 272
MODEL_NAMES = {"gpt-6.1-sol": "GPT-6.1 Sol"}


def context_zone(context_tokens: Optional[int], model: Optional[str], context_window: Optional[int] = None) -> dict:
    """Where the model-visible context size is relative to the model's long-context pricing threshold.

    `context_tokens` must be the size of the latest model request's context as Codex reports it (tokenUsage.last), NEVER
    the thread's accumulated usage: a long thread has used millions of tokens in total while each request stays small.
    Unknown size -> zone "unknown" (no guess). A model without a threshold in the pricing table -> zone "n/a"."""
    threshold = efficiency.long_context_threshold(model)
    out = {"zone": "n/a", "tokens": context_tokens, "threshold": threshold, "warn_at": None, "strong_at": None,
           "message": "", "actions": [], "model": model or "", "window": context_window, "reachable": None}
    if not threshold:
        return out
    out["warn_at"], out["strong_at"] = round(threshold * WARN_FRACTION), round(threshold * STRONG_FRACTION)
    out["reachable"] = None if not context_window else context_window >= threshold
    name = MODEL_NAMES.get((model or "").lower(), model)
    if context_tokens is None:
        out.update(zone="unknown", message="context size not reported yet (measured from the next request)")
        return out
    if context_tokens >= threshold:
        zone, msg = "long", f"{name} long-context pricing zone"
    elif context_tokens >= out["strong_at"]:
        zone, msg = "strong", f"Strong warning: close to the {threshold:,}-token long-context pricing threshold"
    elif context_tokens >= out["warn_at"]:
        zone, msg = "warning", f"Warning: approaching the {threshold:,}-token long-context pricing threshold"
    else:
        zone, msg = "normal", "normal"
    out.update(zone=zone, message=msg, actions=list(ZONE_ACTIONS) if zone != "normal" else [])
    if zone != "normal" and context_window and context_window < threshold:
        out["note"] = (f"This thread's model window is {context_window:,} tokens, below the {threshold:,} threshold, so the "
                       "pricing zone cannot be reached unless model_context_window is raised (the GUI never raises it).")
    return out


# ---------------------------------------------------------------- tool output records

def tool_output_record(item: dict, cap_tokens: Optional[int], th: Thresholds) -> Optional[dict]:
    """One finished tool item of an app-server notification -> a record, or None when it carries no output.

    `raw_tokens_est` is the size of what the tool produced (Codex hands the GUI up to 1 MiB of it); what the model
    sees is cut to `cap_tokens` (the lower of the task's tool_output_token_limit and the model's own policy), so
    `model_tokens_est` is that capped figure. Both are estimates (UTF-8 bytes / 4)."""
    if not isinstance(item, dict):
        return None
    kind = item.get("type")
    if kind == "commandExecution":
        text, label = item.get("aggregatedOutput") or "", str(item.get("command") or "")
        failed = item.get("exitCode") not in (None, 0)
        sig = [item.get("command"), item.get("exitCode")]
    elif kind == "mcpToolCall":
        result = item.get("result")
        text = json.dumps(result, ensure_ascii=False) if result else ""
        label = f"{item.get('server', '')}/{item.get('tool', '')}"
        failed = item.get("status") == "failed" or bool(item.get("error"))
        sig = [label, json.dumps(item.get("arguments"), sort_keys=True, default=str), item.get("status")]
    else:
        return None
    raw = est_tokens(text)
    seen = min(raw, cap_tokens) if cap_tokens else raw
    size = "large" if raw >= th.tool_output_large_tokens else "warn" if raw >= th.tool_output_warn_tokens else "normal"
    return {"tool": "command" if kind == "commandExecution" else "mcp", "label": label[:200], "raw_tokens_est": raw,
            "model_tokens_est": seen, "truncated_for_model": bool(cap_tokens and raw > cap_tokens), "size": size,
            "failed": failed, "signature": hashlib.sha1(json.dumps(sig, default=str).encode()).hexdigest()[:12]
                                         + hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:8]}


def context_jump(prev_input: Optional[int], new_input: Optional[int], pending_large_output: Optional[dict],
                 th: Thresholds) -> Optional[dict]:
    """A warning when the request input grew by `context_jump_tokens` or more right after a big tool output."""
    if prev_input is None or new_input is None or not pending_large_output:
        return None
    grew = new_input - prev_input
    if grew < th.context_jump_tokens:
        return None
    return {"kind": "context_jump", "severity": "warning", "grew_tokens": grew,
            "message": f"Input grew by {grew:,} tokens right after a ~{pending_large_output['model_tokens_est']:,}-token tool output "
                       f"({pending_large_output['label'][:80]}). Narrow the command (rg, sed -n, head) to keep the context small."}


# ---------------------------------------------------------------- retry guard

NON_RETRYABLE_ERRORS = {
    "contextWindowExceeded": "context_window_exceeded",
    "usageLimitExceeded": "quota_exhausted",
    "rateLimitExceeded": "quota_exhausted",
    "sessionBudgetExceeded": "session_budget_exceeded",
    "unauthorized": "authentication_error",
}
STOP_MESSAGES = {
    "context_window_exceeded": "The context window was exceeded. Retrying would resend the same huge context, so nothing is retried: "
                               "Compact the thread or Start New Session in the same worktree.",
    "quota_exhausted": "Codex usage is exhausted. Nothing is retried; run the instruction again when your included usage is available.",
    "session_budget_exceeded": "The session budget was exceeded. Nothing is retried.",
    "authentication_error": "Codex authentication failed. Nothing is retried; run `codex login` and send the instruction again.",
    "repeated_tool_failure": "The same tool call failed repeatedly with an identical result, so the turn was stopped to avoid "
                             "looping. Change the approach or the instruction, then send again.",
}


def stop_reason_for_error(error_kind: str) -> str:
    """The retry-guard stop reason for a Codex error kind ("" when the error is not one that must never be retried)."""
    return NON_RETRYABLE_ERRORS.get(error_kind or "", "")


class RepeatFailureDetector:
    """Counts consecutive identical failing tool calls (same command/arguments AND same output). Any success or a
    different failure resets the count. `limit` identical failures in a row -> the turn should be stopped."""

    def __init__(self, limit: int = 3):
        self.limit = limit
        self._sig: Optional[str] = None
        self._n = 0

    def feed(self, record: Optional[dict]) -> Optional[dict]:
        if not record:
            return None
        if not record["failed"]:
            self._sig, self._n = None, 0
            return None
        if record["signature"] == self._sig:
            self._n += 1
        else:
            self._sig, self._n = record["signature"], 1
        if self._n >= self.limit:
            return {"kind": "retry_guard", "severity": "critical", "reason": "repeated_tool_failure", "count": self._n,
                    "message": f"{STOP_MESSAGES['repeated_tool_failure']} ({record['label'][:100]} failed {self._n} times)"}
        return None


def blocks_resend(stop_reason: str) -> bool:
    """Sending the same thread another instruction right after this stop would just resend the same context."""
    return stop_reason == "context_window_exceeded"
