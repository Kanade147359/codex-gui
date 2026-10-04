"""Semantic completion, separate from process/turn completion. Checks never mutate Git."""
import asyncio
import json
import os
from pathlib import Path
import shlex
import shutil
import signal

from . import git_manager as git
from .models import now_iso

SCHEMA_PATH = Path(__file__).with_name("completion_schema.json")
SCHEMA = json.loads(SCHEMA_PATH.read_text())
RESULT_INSTRUCTION = ('At the end return only JSON {"status":"SUCCESS|BLOCKED|NEEDS_INPUT|PARTIAL",'
                      '"reason":"short reason"}. SUCCESS means the entire request is fulfilled; '
                      'missing required information is NEEDS_INPUT, obstacles are BLOCKED, unfinished work is PARTIAL.')
OUTCOMES = {"SUCCESS": "success", "BLOCKED": "blocked", "NEEDS_INPUT": "needs_input", "PARTIAL": "incomplete"}
BWRAP_MISSING_REASON = ("bubblewrap (bwrap) is not installed, so validation commands could not be safely executed.")


class ValidationUnavailable(RuntimeError):
    """Required validation infrastructure is absent, rather than a failing task/check."""


def validation_capability():
    available = shutil.which("bwrap") is not None
    return {"bwrap_available": available,
            "message": "" if available else "Validation commands requiring sandboxing will require manual review."}


def contract(value):
    """Validate at the API boundary, before creating a worktree or running any command."""
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - {
        "required_paths", "required_changed_paths", "require_any_change", "require_commit",
        "validation_commands", "manual_approval",
    }:
        raise ValueError("invalid completion contract")
    result = dict(value)
    for key in ("required_paths", "required_changed_paths"):
        paths = result.get(key, [])
        if not isinstance(paths, list):
            raise ValueError(f"{key} must be a list")
        for path in paths:
            if (not isinstance(path, str) or not path or "\x00" in path or
                    Path(path).is_absolute() or ".." in Path(path).parts or ".git" in Path(path).parts):
                raise ValueError(f"{key} must contain relative worktree paths outside .git")
        if key in result:
            result[key] = [str(Path(path)) for path in paths]
    for key in ("require_any_change", "require_commit", "manual_approval"):
        if key in result and not isinstance(result[key], bool):
            raise ValueError(f"{key} must be boolean")
    commands = result.get("validation_commands", [])
    if not isinstance(commands, list) or len(commands) > 20:
        raise ValueError("validation_commands must be a list of at most 20 commands")
    for command in commands:
        argv = shlex.split(command) if isinstance(command, str) else command
        if (not isinstance(argv, list) or not argv or
                any(not isinstance(a, str) or "\x00" in a for a in argv)):
            raise ValueError("validation commands must be command strings or argv lists")
    return result


def parse_result(text):
    """Only a complete final assistant JSON object is evidence; tool output/prose is never scored."""
    if not isinstance(text, str) or len(text) > 16384:
        return None
    try:
        def unique_pairs(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate result field")
                result[key] = value
            return result
        value = json.loads(text, object_pairs_hook=unique_pairs)
    except (ValueError, TypeError, RecursionError):
        return None
    if (not isinstance(value, dict) or set(value) != {"status", "reason"} or
            not isinstance(value.get("status"), str) or value["status"] not in OUTCOMES or not isinstance(value.get("reason"), str) or
            not value["reason"].strip()):
        return None
    return {"status": value["status"], "reason": value["reason"][:1000]}


async def validate_command(root, command, protected_home):
    """Run user checks read-only, with temporary scratch space and no network. Never unsandboxed."""
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        raise ValidationUnavailable(BWRAP_MISSING_REASON)
    argv = shlex.split(command) if isinstance(command, str) else command
    sandbox = [bwrap, "--die-with-parent", "--unshare-net", "--unshare-pid", "--unshare-ipc",
               "--unshare-uts", "--new-session", "--ro-bind", "/", "/",
               "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
    # Hide GUI state, then expose only this task's worktree (also works for worktrees under /tmp).
    sandbox += ["--tmpfs", str(protected_home), "--ro-bind", str(root), str(root),
                "--chdir", str(root), "--setenv", "HOME", "/tmp", "--setenv", "TMPDIR", "/tmp",
                "--setenv", "PYTHONDONTWRITEBYTECODE", "1", "--", *argv]
    try:
        proc = await asyncio.create_subprocess_exec(*sandbox, stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
        try:
            await asyncio.wait_for(proc.wait(), 60)
        finally:
            # Kill grandchildren too, including a validator that timed out or was cancelled.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()
        return proc.returncode == 0, f"validation exited {proc.returncode}"
    except asyncio.TimeoutError:
        return False, "validation timed out (60s)"
    except FileNotFoundError as e:
        raise ValidationUnavailable(BWRAP_MISSING_REASON) from e
    except OSError as e:
        return False, f"validation unavailable: {e}"


async def check(task, final_text, protected_home):
    rules = contract(json.loads(task["completion_contract"] or "{}"))
    result = parse_result(final_text)
    checks = [{"name": "process exited normally", "status": "pass", "reason": ""}]
    def add(name, ok, reason=""):
        checks.append({"name": name, "status": "pass" if ok else "fail", "reason": reason})

    root = Path(task["worktree"]).resolve()
    for relative in rules.get("required_paths", []):
        path = (root / relative).resolve()
        ok = path.is_relative_to(root) and path.exists() and ".git" not in path.relative_to(root).parts
        add(f"required artifact: {relative}", ok, "" if ok else "required artifact missing or outside worktree")
    try:
        if rules.get("required_changed_paths") or rules.get("require_any_change") or rules.get("require_commit"):
            _, tracked, _ = await git.run_git(root, "diff", "--name-only", "-z", task["base_sha"], "--")
            _, untracked, _ = await git.run_git(root, "ls-files", "--others", "--exclude-standard", "-z")
            changed = set((tracked + untracked).split("\x00")) - {""}
            for relative in rules.get("required_changed_paths", []):
                add(f"required change: {relative}", relative in changed, "path must differ from task base")
            if rules.get("require_any_change"):
                add("any change", bool(changed), "no changes relative to task base" if not changed else "")
            if rules.get("require_commit"):
                add("commit", await git.commit_count(root, task["base_sha"]) > 0, "commit required since task base")
    except git.GitError as e:
        add("Git checks", False, str(e)[:500])
    for command in rules.get("validation_commands", []):
        name = "validation: " + (command if isinstance(command, str) else shlex.join(command))
        if any(c["status"] == "fail" for c in checks):
            checks.append({"name": name, "status": "skipped", "reason": "earlier completion check failed"})
        elif any(c["status"] == "unavailable" for c in checks):
            checks.append({"name": name, "status": "skipped", "reason": "validation sandbox unavailable"})
        else:
            try:
                ok, reason = await validate_command(root, command, protected_home)
                add(name, ok, reason)
            except ValidationUnavailable as e:
                checks.append({"name": "validation unavailable: bwrap missing", "status": "unavailable", "reason": str(e)})
                checks.append({"name": name, "status": "skipped", "reason": "validation sandbox unavailable"})
    if not rules.get("validation_commands"):
        checks.append({"name": "validation", "status": "skipped", "reason": "not configured"})
    # Classify the existing checks; this does not infer requirements or run new checks.
    failed = next((c for c in checks if c["status"] == "fail"), None)
    unavailable = next((c for c in checks if c["status"] == "unavailable"), None)
    if failed:
        evidence, evidence_reason = "FAIL", failed["name"] + ": " + failed["reason"]
    elif unavailable or result is None:
        evidence, evidence_reason = "UNKNOWN", unavailable["reason"] if unavailable else "No valid structured semantic result was received."
    else:
        evidence, evidence_reason = "PASS", "Existing completion checks passed; no local contradiction was found."
    add("structured semantic result", result is not None, "" if result else "missing or invalid final result")
    # Explicit obstacles/input take priority. A SUCCESS claim cannot override failed artifacts/validation.
    if result and result["status"] in ("BLOCKED", "NEEDS_INPUT", "PARTIAL"):
        outcome, reason, source = OUTCOMES[result["status"]], result["reason"], "codex"
    elif any(c["status"] == "fail" for c in checks[:-1]):
        outcome, source = "incomplete", "contract"
        reason = next(c["name"] + ": " + c["reason"] for c in checks[:-1] if c["status"] == "fail")
    elif any(c["status"] == "unavailable" for c in checks):
        outcome, source = "needs_review", "gate"
        reason = next(c["reason"] for c in checks if c["status"] == "unavailable")
    elif result is None:
        outcome, reason, source = "needs_review", "No valid structured semantic result was received.", "gate"
    elif rules.get("manual_approval"):
        outcome, reason, source = "needs_review", "Completion checks passed; manual approval required.", "contract"
    else:
        outcome, reason, source = "success", result["reason"], "codex+contract" if rules else "codex"
    if rules.get("manual_approval"):
        checks.append({"name": "manual approval", "status": "pending", "reason": "requires a recorded manual override"})
    return dict(task_outcome=outcome, outcome_reason=reason, outcome_source=source,
                evidence_result=evidence, evidence_reason=evidence_reason,
                completion_checked_at=now_iso(), completion_checks=json.dumps(checks),
                semantic_result=json.dumps(result) if result else "", manual_override=0, manual_override_at=None,
                manual_override_by="", completion_pending=0)


def approve_verified(fields, auto_approve):
    """Approval follows verification. A Custom Contract requiring manual approval keeps priority."""
    fields = dict(fields, approval_source="", approved_at=None)
    result = parse_result(fields.get("semantic_result"))
    if fields["task_outcome"] != "success":
        return fields
    if not result or result["status"] != "SUCCESS" or fields.get("evidence_result") != "PASS":
        fields.update(task_outcome="needs_review", outcome_source="gate",
                      outcome_reason=fields.get("evidence_reason") or "Local evidence could not be verified.")
    elif auto_approve:
        fields.update(approval_source="auto_evidence", approved_at=now_iso())
    else:
        fields.update(task_outcome="needs_review", outcome_source="gate",
                      outcome_reason="Completion checks passed; automatic completion approval is disabled.")
    return fields
