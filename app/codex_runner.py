"""Launching `codex exec --json` and turning its JSONL output into log entries.

Tests replace CodexRunner.build_command to run a fake program instead of codex.
"""
import asyncio
import json
import os
import signal
from typing import Optional

from .appserver import subscription_env

# One JSONL line from codex can contain a lot of command output.
STREAM_LIMIT = 32 * 1024 * 1024
MESSAGE_MAX_CHARS = 2000


def task_config(task: dict) -> dict:
    """The Codex config keys (dotted paths) a task pins, shared by both backends so they behave alike.

    Everything here is fixed per task on purpose: changing model, effort, tier, sandbox, web search or tools inside a
    task would change the prompt prefix and lower cache reuse. Auto-approval and the sandbox are not here: the
    app-server takes them as parameters and `codex exec` as flags.
    """
    cfg = {
        "model_verbosity": task["model_verbosity"],
        "web_search": "live" if task["web_search_enabled"] else "disabled",
    }
    if task["reasoning_effort"] not in ("", "default"):
        cfg["model_reasoning_effort"] = task["reasoning_effort"]
    dirs = [d.strip() for d in (task.get("writable_dirs") or "").splitlines() if d.strip()]
    if dirs:
        cfg["sandbox_workspace_write.writable_roots"] = dirs
    for flag in (task.get("feature_flags") or "").replace(",", " ").split():
        cfg[f"features.{flag}"] = True
    return cfg


def nested(flat: dict) -> dict:
    """{"a.b": 1} -> {"a": {"b": 1}} (the app-server takes config as an object, `codex -c` as dotted keys)."""
    out: dict = {}
    for key, value in flat.items():
        node = out
        *parents, leaf = key.split(".")
        for part in parents:
            node = node.setdefault(part, {})
        node[leaf] = value
    return out


def approval_params(task: dict) -> dict:
    """approvalPolicy / approvalsReviewer / sandbox of thread/start|resume for a task.

    Auto approval = `codex exec --approve-for-me`: requests go to the automatic reviewer inside the sandbox
    ("on-request" + "auto_review"). Without it nothing may ask a human (this GUI has no prompt), so the policy is
    "never": the sandbox still applies and commands that need an escalation simply fail.
    The sandbox is always one of the two safe modes; danger-full-access is never requested.
    """
    if task["auto_approval"]:
        return {"approvalPolicy": "on-request", "approvalsReviewer": "auto_review", "sandbox": task["sandbox"]}
    return {"approvalPolicy": "never", "approvalsReviewer": "user", "sandbox": task["sandbox"]}


class CodexRunner:
    def __init__(self, codex_bin: str = "codex", subscription_only: bool = True):
        self.codex_bin = codex_bin
        self.subscription_only = subscription_only
        # Common working instructions, given to a NEW session only (a resumed one already has them).
        self.instructions = ""

    def build_command(self, task: dict, resume_thread: Optional[str] = None) -> list[str]:
        """argv for one turn of a task. The prompt is NOT in argv: it is written to stdin ("-").

        A first turn is `codex exec ...`; a later turn is `codex exec ... resume <thread> -`, which continues
        the same Codex thread so its prompt cache stays warm. Verified with codex-cli 0.159.2: `resume` has no
        -C / --approve-for-me of its own, but accepts them in front of the subcommand, so both turn kinds get
        the identical options (model, effort, approval and cwd must not drift inside one task).
        No daemon flag is passed: Codex decides how it reaches its shared app-server.
        """
        cmd = [self.codex_bin, "exec", "--json", "-C", task["worktree"]]
        if task["auto_approval"] and task["sandbox"] == "workspace-write":
            # Automatic review inside the workspace-write sandbox. Never the dangerous bypass flag.
            cmd.append("--approve-for-me")
        else:
            cmd += ["-s", task["sandbox"]]
        if task["model"]:
            cmd += ["--model", task["model"]]
        config = {"service_tier": task["service_tier"], **task_config(task)}
        if self.instructions and not resume_thread:
            config["developer_instructions"] = self.instructions
        for key, value in config.items():
            cmd += ["-c", f"{key}={json.dumps(value)}"]  # a JSON scalar/list is valid TOML
        if resume_thread:
            cmd += ["resume", resume_thread]
        cmd.append("-")
        return cmd

    async def spawn(self, task: dict, resume_thread: Optional[str] = None) -> asyncio.subprocess.Process:
        """Start the process in its own session so the whole group can be signalled."""
        return await asyncio.create_subprocess_exec(
            *self.build_command(task, resume_thread),
            cwd=task["worktree"],
            env=subscription_env(self.subscription_only),  # SSH_AUTH_SOCK, minus API keys; the agent is shared
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
