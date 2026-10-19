"""Narrow, registered offline toolchain mounts, checked before repair is considered."""

from contextlib import contextmanager, nullcontext
import os
from pathlib import Path
import re
import stat
import tempfile

from .policy import digest


_PRIVATE_NAMES = {
    ".ssh", ".aws", ".codex", ".claude", ".gnupg", ".kube", ".docker", ".git", "codex-gui",
    "auth.json", ".credentials.json", "credentials", "credentials.json", "credentials.toml",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".npmrc", ".netrc", ".pypirc", ".env",
    "state.sqlite", "state.sqlite3", "state.db", "codex-gui.db",
}


def _private_name(name):
    name = name.casefold()
    return (name in _PRIVATE_NAMES or name.startswith(".env.")
            or any(name == base + suffix for base in ("state.sqlite", "state.sqlite3", "state.db", "codex-gui.db")
                   for suffix in ("-wal", "-shm", "-journal")))


def normalize(raw):
    if not raw:
        return {}
    raw = dict(raw)
    raw.setdefault("toolchains", [])
    raw.setdefault("mounts", [])
    raw.setdefault("env", {})
    raw.setdefault("offline", True)
    if raw["offline"] is not True:
        raise ValueError("validation dependencies must be prepared offline")
    if not isinstance(raw["env"], dict) or any(
        key
        not in {
            "PATH",
            "CARGO_HOME",
            "RUSTUP_HOME",
            "CARGO_TARGET_DIR",
            "PYTHONPATH",
            "VIRTUAL_ENV",
            "npm_config_cache",
            "npm_config_offline",
            "CARGO_NET_OFFLINE",
        }
        or not isinstance(value, str)
        or "\x00" in value
        for key, value in raw["env"].items()
    ):
        raise ValueError("unregistered validation environment variable")
    if any(raw["env"].get(key, "true").lower() != "true" for key in ("CARGO_NET_OFFLINE", "npm_config_offline")):
        raise ValueError("offline validation flags cannot be disabled")
    if not isinstance(raw["mounts"], list) or len(raw["mounts"]) > 12:
        raise ValueError("too many environment mounts")
    destinations = set()
    for mount in raw["mounts"]:
        if not isinstance(mount, dict) or set(mount) != {"source", "target"}:
            raise ValueError("mount requires source and target")
        source, target = Path(mount["source"]), mount["target"]
        # No home roots, GUI state, sockets, symlinks or arbitrary overlay of /source.
        if not source.is_absolute() or ".." in source.parts or len(source.parts) < 4 or source.name.startswith("."):
            raise ValueError("mount must identify a narrow toolchain/cache directory")
        if any(_private_name(part) for part in source.parts):
            raise ValueError("credential/GUI directories cannot be mounted")
        if (
            not isinstance(target, str)
            or not re.fullmatch(r"/opt/validation/[A-Za-z0-9_-]+", target)
            or target in destinations
        ):
            raise ValueError("mount target must be unique under /opt/validation")
        destinations.add(target)
    if not isinstance(raw["toolchains"], list) or len(raw["toolchains"]) > 12:
        raise ValueError("invalid toolchain probes")
    for probe in raw["toolchains"]:
        if (
            not isinstance(probe, dict)
            or set(probe) - {"command", "contains"}
            or not isinstance(probe.get("command"), list)
            or not probe["command"]
            or any(not isinstance(a, str) or not a or "\x00" in a for a in probe["command"])
        ):
            raise ValueError("toolchain probes require explicit argv")
        if not isinstance(probe.get("contains", ""), str):
            raise ValueError("toolchain version expectation must be text")
    return raw


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _open_directory(path):
    """Open every ancestor without following links, including a concurrently replaced ancestor."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open("/", flags)
    try:
        for part in path.parts[1:]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _stage_directory(source_fd, destination):
    """Copy inspected regular files; never expose a mutable host directory to candidate code."""
    from .runner import InfrastructureUnavailable

    before = os.fstat(source_fd)
    with os.scandir(source_fd) as entries:
        for entry in entries:
            if _private_name(entry.name):
                raise InfrastructureUnavailable("registered runtime contains private configuration or GUI state")
            info = entry.stat(follow_symlinks=False)
            directory = stat.S_ISDIR(info.st_mode)
            if not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
                raise InfrastructureUnavailable("registered runtime contains links or special files")
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
            fd = os.open(entry.name, flags | (os.O_DIRECTORY if directory else 0), dir_fd=source_fd)
            try:
                if _identity(info) != _identity(os.fstat(fd)):
                    raise InfrastructureUnavailable("registered runtime changed during inspection")
                target = destination / entry.name
                if directory:
                    target.mkdir()
                    _stage_directory(fd, target)
                else:
                    # The read is bounded by the inspected size; appending cannot extend a copy forever.
                    with os.fdopen(os.dup(fd), "rb") as source, target.open("xb") as output:
                        remaining = info.st_size
                        while remaining:
                            block = source.read(min(remaining, 1024 * 1024))
                            if not block:
                                raise InfrastructureUnavailable("registered runtime changed during staging")
                            output.write(block)
                            remaining -= len(block)
                        if source.read(1):
                            raise InfrastructureUnavailable("registered runtime changed during staging")
                    if _identity(info) != _identity(os.fstat(fd)):
                        raise InfrastructureUnavailable("registered runtime changed during staging")
                    target.chmod(info.st_mode & 0o555)
            finally:
                os.close(fd)
    if _identity(before) != _identity(os.fstat(source_fd)):
        raise InfrastructureUnavailable("registered runtime changed during staging")


@contextmanager
def mounts(environment):
    """Mount a private immutable copy whose lifetime covers preflight and validation."""
    from .runner import InfrastructureUnavailable

    environment = normalize(environment)
    with tempfile.TemporaryDirectory(prefix="ap-runtime-") as staging:
        result = []
        try:
            for index, mount in enumerate(environment.get("mounts", [])):
                source = _open_directory(Path(mount["source"]))
                try:
                    target = Path(staging) / str(index)
                    target.mkdir()
                    _stage_directory(source, target)
                finally:
                    os.close(source)
                result += ["--ro-bind", str(target), mount["target"]]
        except (OSError, RecursionError) as error:
            raise InfrastructureUnavailable("registered runtime cannot be safely inspected and staged") from error
        yield result


def env(environment):
    from .runner import minimal_env

    return minimal_env() | dict(CARGO_NET_OFFLINE="true", npm_config_offline="true") | environment.get("env", {})


async def preflight(root, profile, runtime_mounts=None):
    from .runner import InfrastructureUnavailable, sandbox, run_bounded

    environment = profile.get("environment", {})
    evidence = []
    runtime_context = mounts(environment) if runtime_mounts is None else nullcontext(runtime_mounts)
    with runtime_context as runtime, tempfile.TemporaryDirectory(prefix="ap-preflight-") as scratch:
        prefix = sandbox(root, scratch) + runtime
        probes = list(environment.get("toolchains", []))
        # Check command executables even when no explicit version requirement was supplied.
        for command in profile["commands"]:
            if "/" in command[0] and not command[0].startswith(("/usr/", "/bin/", "/opt/validation/")):
                continue  # Repository/scratch executables may be produced by implementation or an earlier build.
            probes.append(dict(command=["/bin/sh", "-c", 'command -v "$1" >/dev/null', "preflight", command[0]]))
            if command[0] in {"cargo", "rustc", "node", "npm", "python", "python3"}:
                probes.append(dict(command=[command[0], "--version"]))
        for probe in probes:
            rc, out, timeout = await run_bounded(prefix + ["--", *probe["command"]], env=env(environment), timeout=15)
            if rc or timeout or probe.get("contains", "") not in out:
                raise InfrastructureUnavailable(
                    "toolchain/offline environment preflight failed: " + probe["command"][0]
                )
            evidence.append(dict(command_digest=digest(probe["command"]), exit_status=rc, output=out[:500]))
    return evidence
