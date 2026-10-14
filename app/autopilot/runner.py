"""Managed arbitrary-code validation with an allowlisted filesystem and writable build outputs."""

import asyncio
import os
from pathlib import Path
import re
import shutil
import signal
import tempfile
import time

from .policy import digest


class InfrastructureUnavailable(RuntimeError):
    pass


def redact(text):
    text = re.sub(
        r"(?i)(bearer\s+|(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password)\s*[=:]\s*)[^\s,\"}]+",
        r"\1[REDACTED]",
        text,
    )
    return re.sub(r"(?:sk-[A-Za-z0-9_-]{12,}|gh[opusr]_[A-Za-z0-9_]{12,})", "[REDACTED]", text)


def sandbox(root, scratch, outputs=(), network=False, cwd="/source"):
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise InfrastructureUnavailable("bubblewrap is required; no unsandboxed fallback")
    args = [bwrap, "--die-with-parent", "--new-session", "--unshare-pid", "--unshare-ipc", "--unshare-uts"]
    if not network:
        args.append("--unshare-net")
    # No bind of /, /home, the GUI database, SSH sockets, or other worktrees.
    for path in ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc/ssl", "/etc/alternatives", "/etc/ld.so.cache"):
        if Path(path).exists():
            args += ["--ro-bind", path, path]
    if network:
        for path in ("/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf"):
            if Path(path).exists():
                args += ["--ro-bind", path, path]
    args += [
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--ro-bind",
        str(root),
        "/source",
        "--bind",
        str(scratch),
        "/scratch",
        "--chdir",
        cwd,
    ]
    for output in outputs:
        # An output mount may overlay existing code: reject that before executing the candidate.
        target = Path(root) / output
        if not target.resolve().is_relative_to(Path(root).resolve()) or any(
            parent.is_symlink() for parent in [target, *target.parents] if parent != Path(root).parent
        ):
            raise InfrastructureUnavailable("build output traverses a symlink")
        if target.exists() and (target.is_symlink() or target.is_file() or any(target.iterdir())):
            raise InfrastructureUnavailable("build output must be an absent or empty directory: " + output)
        target.mkdir(parents=True, exist_ok=True)  # Empty mountpoints do not change the Git source tree.
        private = Path(scratch) / "outputs" / output
        if not private.resolve().is_relative_to(Path(scratch).resolve()) or any(
            p.is_symlink() for p in [private, *private.parents] if p.is_relative_to(Path(scratch))
        ):
            raise InfrastructureUnavailable("build scratch output traverses a symlink")
        private.mkdir(parents=True, exist_ok=True)
        # Preserve outputs between required commands, without writing the source tree.
        args += ["--dir", "/source/" + output, "--bind", str(private), "/source/" + output]
    return args


def minimal_env():
    return dict(
        PATH="/usr/local/bin:/usr/bin:/bin",
        HOME="/scratch",
        TMPDIR="/scratch",
        LANG="C.UTF-8",
        PYTHONDONTWRITEBYTECODE="1",
        PYTEST_ADDOPTS="-p no:cacheprovider",
        npm_config_cache="/scratch/npm",
        CARGO_HOME="/scratch/cargo",
        CARGO_TARGET_DIR="/scratch/target",
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_CONFIG_SYSTEM="/dev/null",
    )


async def run_bounded(argv, *, cwd=None, env=None, timeout=300, stdin=None, on_start=None, log_path=None):
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    if on_start:
        on_start(proc)
    collected = bytearray()

    async def consume():
        if stdin is not None:
            proc.stdin.write(stdin.encode())
            await proc.stdin.drain()
            proc.stdin.close()
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                break
            if len(collected) + len(chunk) > 2 * 1024 * 1024:
                raise InfrastructureUnavailable("output limit exceeded")
            collected.extend(chunk)
        await proc.wait()

    timed_out = False
    try:
        await asyncio.wait_for(consume(), timeout)
    except asyncio.TimeoutError:
        timed_out = True
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()
        output = redact(collected.decode(errors="replace"))
        if log_path:
            Path(log_path).write_text(output)
    return proc.returncode, output, timed_out


class Validator:
    async def validate(self, candidate, root, profile, log_dir, on_start=None):
        from .environment import preflight, mounts, env

        preflight_evidence = await preflight(root, profile)
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        began = time.time()
        checks = []
        for p in profile["artifacts"]:
            target = Path(root) / p
            valid = (
                target.exists() and not target.is_symlink() and target.resolve().is_relative_to(Path(root).resolve())
            )
            checks.append(dict(artifact=p, exit_status=0 if valid else 1))
        with tempfile.TemporaryDirectory(prefix="ap-validator-") as scratch:
            for i, command in enumerate(profile["commands"]):
                command_started = time.time()
                argv = (
                    sandbox(root, scratch, profile["outputs"])
                    + mounts(profile.get("environment", {}))
                    + ["--", *command]
                )
                rc, output, timeout = await run_bounded(
                    argv,
                    env=env(profile.get("environment", {})),
                    timeout=profile["timeout"],
                    on_start=on_start,
                    log_path=log_dir / f"{i}.log",
                )
                if "bwrap:" in output and rc != 0:
                    raise InfrastructureUnavailable(output[-1000:])
                checks.append(
                    dict(
                        command=command,
                        command_digest=digest(command),
                        exit_status=rc,
                        timed_out=timeout,
                        started_at=command_started,
                        finished_at=time.time(),
                        log=str(log_dir / f"{i}.log"),
                        excerpt=output[-4000:] if rc or timeout else "",
                    )
                )
        passed = bool(checks) and all(c["exit_status"] == 0 and not c.get("timed_out") for c in checks)
        return dict(
            scope=candidate["payload"]["scope"],
            profile_digest=digest(profile),
            environment="bwrap:allowlisted-runtime;network=off;source=read-only",
            environment_preflight=preflight_evidence,
            started_at=began,
            finished_at=time.time(),
            checks=checks,
            state="PASS" if passed else "FAIL",
        )
