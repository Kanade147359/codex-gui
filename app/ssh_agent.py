"""SSH agent plumbing: environment inheritance for child processes and startup diagnostics.

run.sh owns the agent lifecycle (reuse, start, ssh-add) and exports SSH_AUTH_SOCK before
launching uvicorn. This module makes sure every child process (codex, git) sees it too, and
logs what the agent / remote look like so authentication problems are visible at startup.
"""
import asyncio
import logging
import os
import re
import shlex
from pathlib import Path
from typing import Optional

log = logging.getLogger("uvicorn.error")

AGENT_ENV_PATH = Path("~/.ssh/agent.env")
SSH_VARS = ("SSH_AUTH_SOCK", "SSH_AGENT_PID")
SSH_TEST_TIMEOUT = 15.0

_GITHUB_SSH_URL = re.compile(r"^(?:ssh://)?(?:[\w.-]+@)?github\.com[:/]")


def read_agent_env(path: Optional[Path] = None) -> dict:
    """Parse the `VAR=value; export VAR;` lines of an ssh-agent -s dump. Never raises."""
    path = (path or AGENT_ENV_PATH).expanduser()
    found = {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return found
    for line in lines:
        name, sep, rest = line.partition("=")
        if sep and name in SSH_VARS:
            try:
                found[name] = shlex.split(rest.split(";", 1)[0])[0]
            except (ValueError, IndexError):
                pass
    return found


def child_env(extra: Optional[dict] = None, agent_env_path: Optional[Path] = None) -> dict:
    """Environment for codex/git children: os.environ plus SSH_AUTH_SOCK.

    Normally run.sh already exported it. If the app was started some other way (plain uvicorn),
    fall back to the agent saved in ~/.ssh/agent.env, but only if its socket still exists.
    """
    env = {**os.environ, **(extra or {})}
    if not env.get("SSH_AUTH_SOCK"):
        saved = read_agent_env(agent_env_path)
        sock = saved.get("SSH_AUTH_SOCK")
        if sock and Path(sock).exists():
            env.update(saved)
    return env


async def _run(*argv: str, cwd=None, timeout: float = 10.0) -> tuple[Optional[int], str]:
    """Run a command, returning (exit code or None on spawn failure/timeout, combined output)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd, env=child_env(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except OSError as e:
        return None, str(e)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return None, f"timed out after {timeout:.0f}s"
    return proc.returncode, out.decode(errors="replace").strip()


def is_github_ssh_url(url: str) -> bool:
    return bool(_GITHUB_SSH_URL.match(url.strip()))


def classify_github_ssh(code: Optional[int], output: str) -> tuple[bool, str]:
    """Decide whether `ssh -T git@github.com` authenticated.

    GitHub exits with 1 even on success (it offers no shell), so the exit code alone says
    nothing. Success is the greeting text; everything else is reported with its output.
    """
    if re.search(r"successfully authenticated", output, re.I):
        return True, output
    if code is None:
        return False, output
    if "Permission denied" in output:
        return False, output + "\n(agent has no key GitHub accepts; check `ssh-add -l` and the key registered on GitHub)"
    return False, output or f"exit {code} with no output"


async def check_github_ssh() -> tuple[bool, str]:
    # BatchMode: never prompt for a passphrase/host key from a background task.
    code, out = await _run(
        "ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "git@github.com",
        timeout=SSH_TEST_TIMEOUT)
    return classify_github_ssh(code, out)


def _fmt(out: str) -> str:
    return "\n    ".join(out.splitlines()) if out else "(no output)"


async def log_diagnostics(repos: list[str], github_test: bool = True) -> None:
    """Log ssh-add -l and each repo's remote.origin.url; test GitHub over SSH if applicable."""
    sock = child_env().get("SSH_AUTH_SOCK")
    log.info("ssh diagnostics: SSH_AUTH_SOCK=%s", sock or "(not set)")
    code, out = await _run("ssh-add", "-l")
    log.info("$ ssh-add -l  (exit %s)\n    %s", code, _fmt(out))
    if code == 2:
        log.warning("ssh-agent is unreachable; git over SSH will fail. Start via ./run.sh (see README)")
    elif code == 1:
        log.warning("ssh-agent has no keys loaded; run ssh-add")

    github_remote = False
    for repo in repos:
        code, out = await _run("git", "-C", repo, "config", "--get", "remote.origin.url")
        log.info("$ git -C %s config --get remote.origin.url  (exit %s)\n    %s", repo, code, _fmt(out))
        github_remote = github_remote or (code == 0 and is_github_ssh_url(out))

    if github_test and github_remote:
        ok, out = await check_github_ssh()
        log.log(logging.INFO if ok else logging.WARNING,
                "$ ssh -T git@github.com  -> %s\n    %s", "OK" if ok else "FAILED", _fmt(out))


def diagnostics_enabled() -> bool:
    return os.environ.get("CODEX_GUI_SSH_DIAGNOSTICS", "1") != "0"


def github_test_enabled() -> bool:
    return os.environ.get("CODEX_GUI_SSH_GITHUB_TEST", "1") != "0"
