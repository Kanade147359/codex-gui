"""Turning app-server notifications into task-log entries.

Streaming deltas (agent text, command output, reasoning) are not logged: their final text arrives with the
matching `item/completed`, and logging every delta would multiply the log by orders of magnitude.
"""
import json
from typing import Optional

from .codex_runner import _clip

# Notifications that are only progress noise for the log.
SKIP = frozenset({
    "item/agentMessage/delta", "item/plan/delta", "item/commandExecution/outputDelta", "command/exec/outputDelta",
    "process/outputDelta", "item/fileChange/outputDelta", "item/reasoning/summaryTextDelta",
    "item/reasoning/summaryPartAdded", "item/reasoning/textDelta", "item/commandExecution/terminalInteraction",
    "thread/status/changed", "mcpServer/startupStatus/updated", "remoteControl/status/changed", "account/updated",
    "thread/tokenUsage/updated", "account/rateLimits/updated", "turn/diff/updated", "thread/settings/updated",
    "serverRequest/resolved", "thread/goal/cleared", "thread/goal/updated", "item/started",
})


def _item_message(item: dict) -> tuple[str, str]:
    itype = str(item.get("type", "item"))
    if itype == "commandExecution":
        msg = f"$ {item.get('command', '')}"
        if item.get("exitCode") is not None:
            msg += f"\n(exit {item['exitCode']})"
        if item.get("aggregatedOutput"):
            msg += "\n" + str(item["aggregatedOutput"]).rstrip()
        return itype, _clip(msg)
    if itype in ("agentMessage", "plan"):
        return itype, _clip(str(item.get("text") or ""))
    if itype == "fileChange":
        changes = item.get("changes") or []
        lines = [f"{c.get('kind', '?')} {c.get('path', '?')}" if isinstance(c, dict) else str(c) for c in changes]
        return itype, _clip("\n".join(lines))
    if itype == "reasoning":
        summary = item.get("summary") or []
        return itype, _clip("\n".join(str(x) for x in summary))
    if itype == "contextCompaction":
        return itype, "context compacted"
    if itype == "webSearch":
        return itype, _clip(str(item.get("query") or ""))
    if itype == "mcpToolCall":
        return itype, _clip(f"{item.get('server', '')}/{item.get('tool', '')} ({item.get('status', '')})")
    return itype, _clip(json.dumps({k: v for k, v in item.items() if k not in ("type", "id")}, ensure_ascii=False))


def log_entry(method: str, params: dict) -> Optional[tuple[str, str]]:
    """(type label, message) for a notification worth showing, else None. The raw params go in the log as `event`."""
    if method in SKIP:
        return None
    if method == "item/completed" and isinstance(params.get("item"), dict):
        item = params["item"]
        if item.get("type") in ("userMessage", "functionCallOutput"):
            return None  # the instruction itself is logged by the GUI
        itype, msg = _item_message(item)
        return f"{method}/{itype}", msg
    if method == "item/started" and isinstance(params.get("item"), dict):
        return None
    if method == "turn/started":
        return method, ""
    if method == "turn/completed":
        turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
        err = turn.get("error") if isinstance(turn.get("error"), dict) else {}
        msg = str(turn.get("status", ""))
        if turn.get("durationMs") is not None:
            msg += f" in {turn['durationMs'] / 1000:.1f}s"
        if err.get("message"):
            msg += f": {err['message']}"
        return method, _clip(msg)
    if method == "error":
        err = params.get("error") if isinstance(params.get("error"), dict) else {}
        info = err.get("codexErrorInfo")
        label = info if isinstance(info, str) else (next(iter(info)) if isinstance(info, dict) and info else "")
        retry = " (will retry)" if params.get("willRetry") else ""
        return method, _clip(f"{label + ': ' if label else ''}{err.get('message', '')}{retry}")
    msg = params.get("message")
    if isinstance(msg, str) and msg:
        return method, _clip(msg)
    rest = {k: v for k, v in params.items() if k not in ("threadId", "turnId")}
    return method, _clip(json.dumps(rest, ensure_ascii=False)) if rest else ""
