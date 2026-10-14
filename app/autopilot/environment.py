"""Narrow, registered offline toolchain mounts, checked before repair is considered."""

from pathlib import Path
import re
import tempfile

from .policy import digest


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
        if any(part in {".ssh", ".aws", ".codex", ".claude", "codex-gui"} for part in source.parts):
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


def mounts(environment):
    from .runner import InfrastructureUnavailable

    result = []
    for mount in environment.get("mounts", []):
        source = Path(mount["source"])
        if not source.is_dir() or source.is_symlink() or source.resolve() != source:
            raise InfrastructureUnavailable("registered toolchain/offline dependency directory is unavailable")
        # Credential files anywhere inside a registered cache are an invalid profile.
        if any(
            p.name in {"auth.json", ".credentials.json", "id_rsa", "id_ed25519"} or p.is_socket() or p.is_symlink()
            for p in source.rglob("*")
        ):
            raise InfrastructureUnavailable("registered runtime contains credentials, symlinks or sockets")
        result += ["--ro-bind", str(source), mount["target"]]
    return result


def env(environment):
    from .runner import minimal_env

    return minimal_env() | dict(CARGO_NET_OFFLINE="true", npm_config_offline="true") | environment.get("env", {})


async def preflight(root, profile):
    from .runner import InfrastructureUnavailable, sandbox, run_bounded

    environment = profile.get("environment", {})
    evidence = []
    with tempfile.TemporaryDirectory(prefix="ap-preflight-") as scratch:
        prefix = sandbox(root, scratch) + mounts(environment)
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
