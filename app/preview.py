"""Corriedale Console preview lifecycle. Linux, Python stdlib, npm, Cargo, bubblewrap."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import http.client
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from contextlib import contextmanager
from pathlib import Path

from app.procinfo import cmdline, process_identity

CODE = Path(__file__).resolve().parent.parent
OWNER = "codex-gui-console-preview-v1"
EXCLUDED = {
    ".git",
    "node_modules",
    "dist",
    "target",
    ".preview",
    ".venv",
    "__pycache__",
}


class PreviewError(Exception):
    pass


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + f".{secrets.token_hex(6)}.tmp")
    with temporary.open("x") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
    temporary.replace(path)


def root_path(value: str | Path, create: bool = False) -> Path:
    raw = Path(value).expanduser().absolute()
    if raw.is_symlink():
        raise PreviewError("preview root must not be a symlink")
    root = raw.resolve()
    marker = root / ".owner"
    if create and not marker.exists():
        if root.exists() and any(root.iterdir()):
            raise PreviewError(
                "refusing nonempty directory without preview ownership marker"
            )
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        marker.write_text(OWNER)
        os.chmod(root, 0o700)
    if marker.is_symlink() or not marker.is_file() or marker.read_text() != OWNER:
        raise PreviewError("not an owned preview root")
    if root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise PreviewError("preview root must belong to this user and have mode 0700")
    return root


def entry_path(root: Path, ident: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{16}", ident):
        raise PreviewError("invalid preview ID")
    entry = root / ident
    if entry.is_symlink() or not entry.is_dir():
        raise PreviewError("preview does not exist or is a symlink")
    manifest = read_manifest(entry)
    if manifest.get("owner") != OWNER or manifest.get("id") != ident:
        raise PreviewError("preview ownership mismatch")
    return entry


def read_manifest(entry: Path) -> dict:
    path = entry / "manifest.json"
    if path.is_symlink():
        raise PreviewError("manifest must not be a symlink")
    return json.loads(path.read_text())


def pid_namespace() -> str:
    return os.readlink("/proc/self/ns/pid")


def control(entry: Path, command: str) -> dict:
    auth = json.loads((entry / "auth.json").read_text())
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(2)
        client.connect(str(entry / "runtime" / "ctl"))
        client.sendall(
            json.dumps({"command": command, "token": auth["session"]}).encode() + b"\n"
        )
        reply = client.makefile("rb").readline(8192)
    result = json.loads(reply)
    if result.get("id") != entry.name or not result.get("authorized"):
        raise PreviewError("preview control ownership check failed")
    return result


@contextmanager
def entry_lock(entry: Path):
    path = entry / ".lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def git(worktree: Path, *args: str) -> str:
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    result = subprocess.run(
        ["git", "-C", str(worktree), *args],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.rstrip("\n")


def source_files(worktree: Path) -> list[Path]:
    names = git(
        worktree, "ls-files", "--cached", "--others", "--exclude-standard", "-z"
    ).split("\0")
    files = []
    for name in sorted(set(names)):
        if not name:
            continue
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise PreviewError("unsafe source path")
        if any(part in EXCLUDED for part in relative.parts):
            continue
        path = worktree / relative
        if path.is_symlink() or any(
            p.is_symlink() for p in path.parents if p != worktree
        ):
            raise PreviewError(f"source symlinks are unsupported: {name}")
        if path.is_file():
            files.append(relative)
    return files


def digest_files(base: Path, files: list[Path]) -> str:
    digest = hashlib.sha256()
    for relative in sorted(files):
        path = base / relative
        digest.update(os.fsencode(relative) + b"\0")
        digest.update(str(path.stat().st_mode & 0o777).encode() + b"\0")
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def snapshot(worktree: Path, destination: Path) -> dict:
    if Path(git(worktree, "rev-parse", "--show-toplevel")).resolve() != worktree:
        raise PreviewError("--worktree must be the Git worktree root")
    sha = git(worktree, "rev-parse", "HEAD")
    branch = git(worktree, "branch", "--show-current") or "(detached)"
    source_status = git(worktree, "status", "--porcelain", "--untracked-files=all")
    dirty = bool(source_status)
    files = source_files(worktree)
    before = digest_files(worktree, files)
    destination.mkdir(mode=0o700)
    for relative in files:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(worktree / relative, target)
    copied = digest_files(destination, files)
    after_files = source_files(worktree)
    if (
        before != copied
        or files != after_files
        or before != digest_files(worktree, after_files)
        or sha != git(worktree, "rev-parse", "HEAD")
        or source_status
        != git(worktree, "status", "--porcelain", "--untracked-files=all")
        or branch != (git(worktree, "branch", "--show-current") or "(detached)")
    ):
        raise PreviewError("worktree changed during snapshot; start a new preview")
    return dict(
        worktree=str(worktree),
        branch=branch,
        commit=sha,
        dirty=dirty,
        snapshot_sha256=copied,
        file_count=len(files),
    )


def build_env(entry: Path) -> dict:
    # No inherited API target, VITE_*, Corriedale credentials or production data paths.
    env = {
        key: os.environ[key]
        for key in ("PATH", "LANG", "LC_ALL", "RUSTUP_HOME")
        if key in os.environ
    }
    env.update(
        HOME=str(entry / "home"),
        CARGO_HOME=str(entry / "cargo-home"),
        CARGO_TARGET_DIR=str(entry / "artifacts" / "cargo"),
        npm_config_cache=str(entry / "npm-cache"),
        VITE_API_BASE_URL="",
        CORRIEDALE_API_PROXY_TARGET="http://127.0.0.1:1",
        CARGO_BUILD_JOBS="2",
        CARGO_PROFILE_DEV_DEBUG="0",
    )
    env.setdefault("RUSTUP_HOME", str(Path.home() / ".rustup"))
    (entry / "home").mkdir(exist_ok=True)
    return env


def run_checked(
    argv: list[str], cwd: Path, entry: Path, label: str, timeout: int = 1800
) -> None:
    sandbox = shutil.which("bwrap")
    if not sandbox:
        raise PreviewError("preview builds require bubblewrap; no unsandboxed fallback")
    # Build tools can download locked dependencies, but only this preview is writable.
    # The private PID namespace is destroyed even if the invoking CLI crashes.
    command = [
        sandbox,
        "--unshare-user",
        "--unshare-pid",
        "--die-with-parent",
        "--cap-drop",
        "ALL",
        "--ro-bind",
        "/",
        "/",
        "--tmpfs",
        "/run",
        "--tmpfs",
        "/tmp",
        "--bind",
        str(entry),
        str(entry),
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--chdir",
        str(cwd),
        "--",
        *argv,
    ]
    with (entry / "logs" / "build.log").open("a") as log:
        log.write(f"\n[{label}] {json.dumps(argv)}\n")
        log.flush()
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=build_env(entry),
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            code = process.wait(timeout=timeout)
        except BaseException:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise
    if code:
        raise PreviewError(
            f"{label} failed (exit {code}); see preview logs --component build"
        )


def build(entry: Path, mode: str, verify: bool) -> dict:
    source = entry / "source"
    console = source / "apps" / "console"
    for path in (console / "package-lock.json", source / "Cargo.lock"):
        if not path.is_file():
            raise PreviewError(f"required locked build input missing: {path.name}")
    run_checked(["npm", "ci", "--no-audit", "--no-fund"], console, entry, "npm ci")
    checks = []
    if verify:
        for script in ("typecheck", "lint", "test"):
            argv = ["npm", "run", script]
            if script == "lint":
                # Explicit roots work even under the tool worktree's ignored .preview directory.
                argv += ["--", "--no-ignore", "src", "tests", "vite.config.ts"]
            run_checked(argv, console, entry, f"Console {script}")
            checks.append(f"Console {script}")
    run_checked(["npm", "run", "build"], console, entry, "Console build")
    dist = entry / "artifacts" / "console"
    shutil.copytree(console / "dist", dist)
    if not (dist / "index.html").is_file():
        raise PreviewError("Console build did not produce index.html")
    if mode == "integrated":
        if verify:
            for label, argv in (
                ("Rust format", ["cargo", "fmt", "--all", "--", "--check"]),
                (
                    "Rust clippy",
                    [
                        "cargo",
                        "clippy",
                        "--locked",
                        "--all-targets",
                        "--",
                        "-D",
                        "warnings",
                    ],
                ),
                ("Rust tests", ["cargo", "test", "--locked", "--lib"]),
                (
                    "Security inventory validation",
                    [sys.executable, "tools/check-security-release", "--validate"],
                ),
            ):
                run_checked(argv, source, entry, label)
                checks.append(label)
        run_checked(
            ["cargo", "build", "--locked", "--bin", "corriedale"],
            source,
            entry,
            "Agent build",
        )
        shutil.copy2(
            entry / "artifacts" / "cargo" / "debug" / "corriedale",
            entry / "artifacts" / "corriedale",
        )
    artifact_files = [
        p.relative_to(entry / "artifacts")
        for p in (entry / "artifacts").rglob("*")
        if p.is_file() and "cargo" not in p.relative_to(entry / "artifacts").parts
    ]
    artifacts = {
        str(p): hashlib.sha256((entry / "artifacts" / p).read_bytes()).hexdigest()
        for p in artifact_files
    }
    write_json(entry / "artifact-hashes.json", artifacts)
    return dict(
        result="PASS",
        artifact_sha256=digest_files(entry / "artifacts", artifact_files),
        artifacts="artifact-hashes.json",
        checks=checks,
        tests="PASS" if verify else "NOT_RUN",
        production_release_gate="NOT_RUN",
    )


def probe(url: str, ident: str, timeout: float = 2) -> dict:
    # Disable inherited HTTP proxies, including for localhost.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        response = opener.open(url + "/_preview/health", timeout=timeout)
    except urllib.error.HTTPError as error:
        if error.code != 503:
            raise
        response = error
    with response:
        result = json.load(response)
    if result.get("id") != ident:
        raise PreviewError("health response belongs to another preview")
    return result


def owns_process(entry: Path, record: dict) -> bool:
    pid = record.get("pid")
    identity = record.get("identity")
    if record.get("pid_namespace") and record["pid_namespace"] != pid_namespace():
        try:
            return control(entry, "status").get("running", False)
        except (OSError, ValueError, PreviewError):
            return False
    try:
        return bool(
            pid
            and identity
            and process_identity(pid) == identity
            and str(entry) in cmdline(pid)
            and os.getpgid(pid) == pid
        )
    except ProcessLookupError:
        return False


def status(entry: Path) -> dict:
    state = read_manifest(entry)
    record = state.get("supervisor", {})
    alive = owns_process(entry, record)
    state["frontend"] = "RUNNING" if alive else "STOPPED"
    state["backend"] = "MOCK" if state["mode"] == "ui" and alive else "STOPPED"
    state["health"] = "NOT_RUN"
    state["visual_review"] = "NOT_RUN"
    state["remote_phone"] = "NOT_RUN"
    evidence = entry / "smoke.json"
    if evidence.is_file() and not evidence.is_symlink():
        state["automated_smoke"] = json.loads(evidence.read_text())
    if alive:
        try:
            health = probe(state["url"], state["id"])
            state["health"] = "PASS" if health["ok"] else "FAIL"
            state["backend"] = health["backend"]
            state["api_smoke"] = health.get("api_smoke", "NOT_RUN")
            state["last_error"] = health.get("last_error") or state.get("last_error")
        except (OSError, ValueError, PreviewError) as error:
            state["health"] = "FAIL"
            state["last_error"] = str(error)
    elif state.get("lifecycle") == "running":
        state["last_error"] = "preview supervisor exited unexpectedly"
    return state


def start(
    root: Path,
    worktree: Path,
    mode: str,
    verify: bool = False,
    remote_host: str | None = None,
    wait_seconds: float = 30,
) -> dict:
    if remote_host and not re.fullmatch(
        r"[a-zA-Z0-9.-]+\.ts\.net(?::[0-9]{1,5})?", remote_host
    ):
        raise PreviewError(
            "--remote-host must be an explicit tailnet HTTPS hostname[:port]"
        )
    ident = secrets.token_hex(8)
    entry = root / ident
    entry.mkdir(mode=0o700)
    (entry / "logs").mkdir()
    (entry / "artifacts").mkdir()
    (entry / "runtime").mkdir(mode=0o700)
    state = dict(
        owner=OWNER,
        id=ident,
        mode=mode,
        created_at=time.time(),
        lifecycle="building",
        build={"result": "NOT_RUN"},
        url=None,
        last_error=None,
        remote_host=remote_host,
        isolation="private-network-and-pid-namespace"
        if mode == "integrated"
        else "mock-only",
    )
    write_json(entry / "manifest.json", state)
    print(f"Preview ID: {ident}; logs: {entry / 'logs'}", file=sys.stderr, flush=True)
    try:
        with entry_lock(entry):
            if len(os.fsencode(entry / "runtime" / "api.sock")) >= 108:
                raise PreviewError(
                    "preview socket path is too long; choose a shorter --root"
                )
            state["source"] = snapshot(worktree.resolve(), entry / "source")
            if mode == "integrated" and not shutil.which("bwrap"):
                raise PreviewError(
                    "Integrated Preview requires bubblewrap; no host-network fallback"
                )
            state["build"] = build(entry, mode, verify)
            password = secrets.token_urlsafe(24)
            (entry / "password").write_text(password + "\n")
            os.chmod(entry / "password", 0o600)
            write_json(
                entry / "auth.json",
                {
                    "password_sha256": hashlib.sha256(password.encode()).hexdigest(),
                    "session": secrets.token_urlsafe(32),
                },
            )
            listener = socket.socket()
            try:
                listener.bind(("127.0.0.1", 0))
                listener.listen(32)
                state["url"] = f"http://127.0.0.1:{listener.getsockname()[1]}"
                write_json(entry / "manifest.json", state)
                env = dict(build_env(entry), PYTHONPATH=str(CODE))
                with (entry / "logs" / "frontend.log").open("a") as log:
                    process = subprocess.Popen(
                        [
                            sys.executable,
                            "-m",
                            "app.preview_runtime",
                            str(entry),
                            str(listener.fileno()),
                        ],
                        cwd=CODE,
                        env=env,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        stdin=subprocess.DEVNULL,
                        pass_fds=(listener.fileno(),),
                        start_new_session=True,
                    )
                identity = process_identity(process.pid)
                if not identity:
                    process.wait(timeout=5)
                    raise PreviewError(
                        "preview supervisor exited before its identity was recorded"
                    )
                state.update(
                    supervisor={
                        "pid": process.pid,
                        "identity": identity,
                        "pid_namespace": pid_namespace(),
                    },
                    lifecycle="running",
                )
                write_json(entry / "manifest.json", state)
            finally:
                listener.close()
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            current = status(entry)
            if current["health"] == "PASS":
                return current
            if not owns_process(entry, state["supervisor"]):
                raise PreviewError(current["last_error"] or "preview process failed")
            time.sleep(0.1)
        raise PreviewError(
            "preview health check timed out; inspect frontend/backend logs"
        )
    except (Exception, KeyboardInterrupt) as error:
        state.update(last_error=str(error) or "interrupted", lifecycle="failed")
        if state["build"]["result"] != "PASS":
            state["build"] = {"result": "FAIL"}
        write_json(entry / "manifest.json", state)
        stop(entry)
        failed = status(entry)
        print(json.dumps(failed, indent=2, ensure_ascii=False), flush=True)
        raise PreviewError(f"preview {ident} failed: {state['last_error']}") from error


def stop(entry: Path) -> dict:
    with entry_lock(entry):
        state = read_manifest(entry)
        record = state.get("supervisor", {})
        owned = owns_process(entry, record)
        same_namespace = record.get("pid_namespace", pid_namespace()) == pid_namespace()
        if (
            same_namespace
            and record.get("pid")
            and process_identity(record["pid"])
            and not owned
        ):
            raise PreviewError("refusing to signal a process with mismatched ownership")
        if owned:
            if same_namespace:
                os.killpg(record["pid"], signal.SIGTERM)
            else:
                control(entry, "stop")
            deadline = time.monotonic() + 5
            while owns_process(entry, record) and time.monotonic() < deadline:
                time.sleep(0.05)
            if owns_process(entry, record) and same_namespace:
                os.killpg(record["pid"], signal.SIGKILL)
                deadline = time.monotonic() + 2
                while owns_process(entry, record) and time.monotonic() < deadline:
                    time.sleep(0.05)
            if owns_process(entry, record):
                raise PreviewError("preview process did not stop")
        elif not same_namespace and record.get("pid"):
            # A live gateway with an unavailable control socket must never be removed.
            try:
                probe(state["url"], state["id"])
            except (OSError, ValueError, PreviewError):
                pass
            else:
                raise PreviewError(
                    "live preview in another PID namespace has no reachable control socket"
                )
        if state["lifecycle"] != "failed":
            state["lifecycle"] = "stopped"
        (entry / "runtime" / "api.sock").unlink(missing_ok=True)
        (entry / "runtime" / "ctl").unlink(missing_ok=True)
        write_json(entry / "manifest.json", state)
    return status(entry)


def cleanup(entry: Path) -> dict:
    stopped = stop(entry)
    # Tailscale routes are deliberately external, never assumed owned by this CLI.
    if (entry / "remote-route.json").exists():
        raise PreviewError(
            "remove the recorded remote route first; see the remote access runbook"
        )
    shutil.rmtree(entry)
    return {"id": stopped["id"], "lifecycle": "cleaned"}


def open_preview(entry: Path) -> dict:
    state = status(entry)
    if state["health"] != "PASS":
        raise PreviewError("preview is not healthy; refusing to open a stale URL")
    try:
        opened = webbrowser.open(state["url"], new=2)
    except (webbrowser.Error, OSError):
        opened = False
    return {
        "id": state["id"],
        "url": state["url"],
        "browser_launch": bool(opened),
        "visual_review": "NOT_RUN",
        "password_file": str(entry / "password"),
    }


def smoke(entry: Path) -> dict:
    state = status(entry)
    if state["health"] != "PASS":
        raise PreviewError("preview must be healthy before smoke tests")
    parsed = urllib.parse.urlsplit(state["url"])

    def request(path, method="GET", body=None, headers=None):
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return (
                response.status,
                dict(response.getheaders()),
                response.read(1024 * 1024),
            )
        finally:
            connection.close()

    code, headers, _ = request(
        "/_preview/login",
        "POST",
        "password=" + (entry / "password").read_text().strip(),
        {"Origin": state["url"], "Content-Type": "application/x-www-form-urlencoded"},
    )
    cookie = headers.get("Set-Cookie", "").split(";")[0]
    page_code, _, page = request("/", headers={"Cookie": cookie})
    denial, _, _ = request("/v1/jobs", "POST", "{}", {"Origin": state["url"]})
    api_code, _, _ = request(
        "/v1/compatibility?nonce=preview-smoke", headers={"Cookie": cookie}
    )
    result = {
        "id": state["id"],
        "source_sha256": state["source"]["snapshot_sha256"],
        "at": time.time(),
        "console_document": "PASS"
        if code == 303 and page_code == 200 and b"<script" in page
        else "FAIL",
        "unauthenticated_write_denied": "PASS" if denial == 401 else "FAIL",
        "console_api_access": "PASS" if api_code == 200 else "BLOCKED",
        "console_api_http_status": api_code,
        "backend_node_identity": state.get("api_smoke", "NOT_RUN"),
        "visual_review": "NOT_RUN",
        "remote_phone": "NOT_RUN",
        "production_release_gate": "NOT_RUN",
    }
    write_json(entry / "smoke.json", result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", default=str(CODE / ".preview"), help="private preview state directory"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("start", "verify"):
        command = commands.add_parser(name)
        command.add_argument("--worktree", required=True, type=Path)
        command.add_argument("--mode", choices=("ui", "integrated"), default="ui")
        command.add_argument(
            "--remote-host",
            help="approved tailnet HTTPS hostname[:port]; does not publish",
        )
        command.add_argument("--wait-seconds", type=float, default=30)
        command.add_argument(
            "--foreground",
            action="store_true",
            help="keep the CLI session alive; stop on Ctrl-C",
        )
    for name in ("status", "open", "stop", "cleanup", "logs", "smoke"):
        command = commands.add_parser(name)
        command.add_argument("id")
        if name == "logs":
            command.add_argument(
                "--component",
                choices=("frontend", "backend", "build"),
                default="frontend",
            )
            command.add_argument("--lines", type=int, default=100)
    args = parser.parse_args(argv)
    try:
        root = root_path(args.root, create=args.command in ("start", "verify"))
        if args.command in ("start", "verify"):
            result = start(
                root,
                args.worktree,
                args.mode,
                args.command == "verify",
                args.remote_host,
                args.wait_seconds,
            )
            if args.command == "verify":
                entry = root / result["id"]
                result["automated_smoke"] = smoke(entry)
                result["browser"] = open_preview(entry)
            result["password_file"] = str(root / result["id"] / "password")
        else:
            entry = entry_path(root, args.id)
            if args.command == "logs":
                from collections import deque

                path = entry / "logs" / f"{args.component}.log"
                if path.is_symlink():
                    raise PreviewError("log must not be a symlink")
                if path.exists():
                    with path.open(errors="replace") as stream:
                        print("".join(deque(stream, maxlen=max(0, args.lines))), end="")
                return 0
            result = {
                "status": status,
                "open": open_preview,
                "stop": stop,
                "cleanup": cleanup,
                "smoke": smoke,
            }[args.command](entry)
        print(json.dumps(result, indent=2, ensure_ascii=False), flush=True)
        checks = result.get(
            "automated_smoke", result if args.command == "smoke" else {}
        )
        failed = args.command in ("verify", "smoke") and any(
            value in ("FAIL", "BLOCKED") for value in checks.values()
        )
        if failed:
            return 1
        if args.command in ("start", "verify") and args.foreground:
            entry = root / result["id"]
            try:
                while owns_process(entry, result["supervisor"]):
                    time.sleep(0.5)
            except KeyboardInterrupt:
                stop(entry)
        return 0
    except (PreviewError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"preview: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
