"""Launching `codex exec --json` and turning its JSONL output into log entries.

Tests replace CodexRunner.build_command to run a fake program instead of codex.
"""
import asyncio
import json
import os
import signal
from typing import Optional

# One JSONL line from codex can contain a lot of command output.
STREAM_LIMIT = 32 * 1024 * 1024
MESSAGE_MAX_CHARS = 2000


class CodexRunner:
    def __init__(self, codex_bin: str = "codex"):
        self.codex_bin = codex_bin

    def build_command(self, task: dict) -> list[str]:
        """argv for one task. The prompt is NOT in argv: it is written to stdin ("-")."""
        cmd = [self.codex_bin, "exec", "--json", "-C", task["worktree"]]
        if task["auto_approval"]:
            # Automatic review inside the workspace-write sandbox. Never the dangerous bypass flag.
            cmd.append("--approve-for-me")
        if task["model"]:
            cmd += ["--model", task["model"]]
        if task["reasoning_effort"] not in ("", "default"):
            cmd += ["-c", f'model_reasoning_effort="{task["reasoning_effort"]}"']
        cmd.append("-")
        return cmd

    async def spawn(self, task: dict) -> asyncio.subprocess.Process:
        """Start the process in its own session so the whole group can be signalled."""
        return await asyncio.create_subprocess_exec(
            *self.build_command(task),
            cwd=task["worktree"],
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            limit=STREAM_LIMIT,
        )


async def terminate_process(proc: asyncio.subprocess.Process, grace: float) -> None:
    """SIGTERM the process group, then SIGKILL it if it is still alive after `grace` seconds."""
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), grace)
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _clip(text: str, limit: int = MESSAGE_MAX_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + f"… (+{len(text) - limit} chars)"


def summarize_event(event: dict) -> tuple[str, str]:
    """Return (type label, one-line-ish human message) for a parsed codex JSON event."""
    etype = str(event.get("type", "event"))
    item = event.get("item")
    if isinstance(item, dict):
        itype = str(item.get("type", "item"))
        label = f"{etype}/{itype}"
        if itype == "command_execution":
            msg = f"$ {item.get('command', '')}"
            if item.get("exit_code") is not None:
                msg += f"\n(exit {item['exit_code']})"
            if item.get("status") == "completed" and item.get("aggregated_output"):
                msg += "\n" + item["aggregated_output"].rstrip()
            return label, _clip(msg)
        text = item.get("text") or item.get("message")
        if text:
            return label, _clip(str(text))
        if itype == "file_change":
            changes = item.get("changes") or []
            return label, _clip("\n".join(f"{c.get('kind', '?')} {c.get('path', '?')}" for c in changes if isinstance(c, dict)))
        return label, _clip(json.dumps(item, ensure_ascii=False))
    if etype == "thread.started":
        return etype, f"thread {event.get('thread_id', '')}"
    if etype == "turn.completed":
        usage = event.get("usage") or {}
        return etype, ", ".join(f"{k}={v}" for k, v in usage.items())
    msg = event.get("message")
    if not msg and isinstance(event.get("error"), dict):
        msg = event["error"].get("message")
    if msg:
        return etype, _clip(str(msg))
    rest = {k: v for k, v in event.items() if k != "type"}
    return etype, _clip(json.dumps(rest, ensure_ascii=False)) if rest else ""


def parse_line(line: str) -> dict:
    """Parse one stdout line. Never raises and never drops data.

    Returns {"type", "message", "event"}; "event" is the parsed JSON object, or None
    when the line is not a JSON object (then type is "raw" and message is the line).
    """
    stripped = line.strip()
    if stripped.startswith("{"):
        try:
            event = json.loads(stripped)
        except ValueError:
            event = None
        if isinstance(event, dict):
            etype, message = summarize_event(event)
            return {"type": etype, "message": message, "event": event}
    return {"type": "raw", "message": line.rstrip("\n"), "event": None}


def pid_is_codex(pid: Optional[int]) -> bool:
    """Best-effort check (Linux /proc) that a pid is alive and still looks like a codex process."""
    if not pid:
        return False
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return b"codex" in f.read()
    except OSError:
        return False
