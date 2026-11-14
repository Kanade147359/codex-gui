"""Conservative build-cache collection. The standalone command is read-only, including its DB connection.

python -m app.artifact_gc --home /path/to/codex-gui --output /tmp/gc-report.json
"""
import argparse
import fcntl
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
from contextlib import contextmanager, nullcontext
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

DAY = 86400
TERMINAL = {"completed", "failed", "stopped", "blocked"}
TASK_SNAPSHOT_SQL = """SELECT t.*, EXISTS (
    SELECT 1 FROM scheduled_instructions s WHERE s.task_id = t.id
    AND s.status IN ('waiting_dependencies', 'waiting_thread', 'ready', 'running', 'blocked')
) AS gc_pending_work FROM tasks t"""


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def stamp(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp()
    except (ValueError, TypeError):
        return None


def overlaps(a, b):
    a, b = str(a), str(b)
    return a == b or a.startswith(b.rstrip("/") + "/") or b.startswith(a.rstrip("/") + "/")


def plain_path(path):
    """No symlink component, mount crossing, or lexical escape, including the managed root."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        return False
    try:
        for part in (path, *path.parents):
            if part.is_symlink():
                return False
        return path.resolve() == path
    except (OSError, RuntimeError):
        return False


def git(root, *args):
    result = subprocess.run(
        ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
         "-C", str(root), *args], capture_output=True, timeout=30,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"})
    if result.returncode:
        raise ValueError("git_check_failed")
    return result.stdout.decode(errors="surrogateescape").strip()


@contextmanager
def launch_guard(home, *, exclusive=False, filename="artifact-gc.lock"):
    """Same interprocess gate for task dispatch and deletion. Never hold a DB lock while acquiring it."""
    fd = os.open(Path(home) / filename, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    acquired = False
    try:
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            pass
        yield acquired
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def process_usage(roots):
    """Inspect same-user processes, including inherited open files and mapped executables.

    Inspection failures fail closed. Stateless scans do not infer activity from directory mtimes.
    """
    busy = set()
    # Codex commands can run in a private PID namespace. An apparently empty /proc is not proof of idleness.
    try:
        unknown = Path("/proc/1/comm").read_text().strip() not in {"systemd", "init", "tini", "dumb-init"}
    except OSError:
        unknown = True
    uid = os.getuid()
    prefixes = [(str(root), str(root) + "/", root) for root in roots]
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            if proc.stat().st_uid != uid:
                continue
            refs = []
            for name in ("cwd", "exe"):
                try:
                    refs.append(os.readlink(proc / name).removesuffix(" (deleted)"))
                except FileNotFoundError:
                    pass  # exited processes / kernel threads
            for fd in (proc / "fd").iterdir():
                try:
                    refs.append(os.readlink(fd).removesuffix(" (deleted)"))
                except FileNotFoundError:
                    pass
            for line in (proc / "maps").read_text().splitlines():
                fields = line.split(None, 5)
                if len(fields) == 6 and fields[5].startswith("/"):
                    refs.append(fields[5].removesuffix(" (deleted)"))
            env = (proc / "environ").read_bytes().split(b"\0")
            refs.extend(os.fsdecode(v.split(b"=", 1)[1]) for v in env
                        if v.startswith((b"CARGO_TARGET_DIR=", b"CARGO_BUILD_TARGET_DIR=")))
            for ref in set(refs):
                if ref.startswith("/"):
                    for exact, prefix, root in prefixes:
                        if ref == exact or ref.startswith(prefix):
                            busy.add(root)
        except (FileNotFoundError, ProcessLookupError):
            continue
        except (OSError, ValueError):
            unknown = True
    return busy, unknown


class Scanner:
    def __init__(self, home, tasks, retention_days=5, now=None, probe=None, progress=None):
        self.home = Path(home).absolute()
        self.root = self.home / "worktrees"
        self.tasks = tasks
        self.retention_days = retention_days
        self.now = time.time() if now is None else now
        self.probe = probe or process_usage
        self._allocated = {}
        self._references = set()
        self.progress = progress or (lambda event: None)

    def discover(self):
        """Walk managed roots recursively without following links or entering Git metadata/cache contents."""
        found = {}
        errors = []
        roots = sorted({Path(t["worktree"]) for t in self.tasks if not t.get("worktree_removed")}, key=str)
        visited = set()
        for root in roots:
            if not root.is_relative_to(self.root) or not plain_path(root) or root == self.root:
                errors.append({"path": str(root), "reason": "unmanaged_or_symlink_worktree"})
                continue
            if not root.is_dir():
                continue
            def onerror(error):
                errors.append({"path": error.filename, "reason": "discovery_unreadable"})
            for current, dirs, files in os.walk(root, followlinks=False, onerror=onerror):
                current = Path(current)
                if current in visited:
                    dirs[:] = []
                    continue
                visited.add(current)
                for name in dirs + files:
                    link = current / name
                    if link.is_symlink():
                        try:
                            self._references.add(link.resolve())
                        except (OSError, RuntimeError):
                            errors.append({"path": str(link), "reason": "unresolved_link"})
                if current.name == ".cargo":
                    for name in ("config", "config.toml"):
                        cfg = current / name
                        if cfg.is_file() and not cfg.is_symlink():
                            try:
                                for match in re.finditer(r'''(?m)^\s*(?:target-dir|build-dir)\s*=\s*['"]([^'"\n]+)['"]''', cfg.read_text()):
                                    self._references.add((current.parent / match[1]).resolve())
                            except (OSError, UnicodeError):
                                errors.append({"path": str(cfg), "reason": "shared_configuration_unreadable"})
                for name in list(dirs):
                    path = current / name
                    if name in {"target", "build"}:
                        found[str(path)] = path
                        if name == "target" or path.is_symlink() or path.is_mount():
                            dirs.remove(name)
                    elif name == ".git" or path.is_symlink() or path.is_mount():
                        dirs.remove(name)
        return list(found.values()), errors

    def _worktrees(self, artifact, owner):
        roots = [owner]
        for parent in reversed(artifact.parent.relative_to(owner).parents):
            candidate = owner / parent
            if candidate != owner and (candidate / ".git").exists():
                roots.append(candidate)
        if artifact.parent != owner and (artifact.parent / ".git").exists():
            roots.append(artifact.parent)
        roots = list(dict.fromkeys(roots))
        states = []
        for root in roots:
            if Path(git(root, "rev-parse", "--show-toplevel")) != root:
                raise ValueError("unregistered_worktree")
            registered = git(root, "worktree", "list", "--porcelain", "-z").split("\0")
            if f"worktree {root}" not in registered:
                raise ValueError("unregistered_worktree")
            # A separate nested repository is not an agent-created Git worktree.
            if root != owner and not (root / ".git").is_file():
                raise ValueError("unknown_nested_repository")
            states.append({"path": str(root), "dirty": bool(git(root, "status", "--porcelain", "-uno"))})
        return states

    def inspect(self, path, busy=(), process_unknown=False):
        row = {"path": str(path), "bytes": 0, "size_complete": False, "last_used_at": None, "reasons": [],
               "candidate": False, "worktrees": [], "task_ids": []}
        reasons = row["reasons"]
        try:
            if not path.is_relative_to(self.root) or not plain_path(path) or not path.is_dir() or path.is_mount():
                raise ValueError("unmanaged_or_symlink_path")
            owners = [t for t in self.tasks if overlaps(path, Path(t["worktree"]))]
            ancestors = [t for t in owners if Path(t["worktree"]) in path.parents]
            if not ancestors:
                raise ValueError("no_managed_owner")
            owner = max((Path(t["worktree"]) for t in ancestors), key=lambda p: len(p.parts))
            row["task_ids"] = [t["id"] for t in owners]
            if len({t["worktree"] for t in owners}) != 1:
                reasons.append("shared_between_tasks")
            if any(overlaps(path, reference) for reference in self._references):
                reasons.append("referenced_or_shared_directory")
            last = []
            for task in owners:
                # Finished rows retain historical pid/claim metadata. Actual liveness is checked in /proc.
                if task.get("status") not in TERMINAL or task.get("gc_pending_work"):
                    reasons.append("task_active_or_waiting")
                if task.get("task_outcome") == "needs_review" or task.get("completion_pending"):
                    reasons.append("needs_review")
                if task.get("worktree_removed"):
                    reasons.append("removed_worktree")
                finished = stamp(task.get("finished_at"))
                if finished is None or any(task.get(key) and stamp(task[key]) is None
                                           for key in ("started_at", "last_turn_at")):
                    reasons.append("unknown_task_last_use")
                last.extend(v for v in (finished, stamp(task.get("started_at")), stamp(task.get("last_turn_at"))) if v is not None)
                contract = json.loads(task.get("completion_contract") or "{}")
                for rel in contract.get("required_paths", []) + contract.get("required_changed_paths", []):
                    if overlaps(path, (Path(task["worktree"]) / rel).resolve()):
                        reasons.append("explicit_artifact")
            if any((p / ".codex-gui-keep").exists() for p in (path, *path.parents) if p.is_relative_to(owner)):
                reasons.append("explicit_keep")
            row["worktrees"] = self._worktrees(path, owner)
            repository = Path(row["worktrees"][-1]["path"])
            # Ignore rules are necessary, never sufficient, evidence. Never delete tracked/modified source.
            relative = str(path.relative_to(repository))
            if git(repository, "ls-files", "-z", "--", relative):
                reasons.append("git_tracked_artifact")
            if git(repository, "ls-files", "--others", "--exclude-standard", "-z", "--", relative):
                reasons.append("uncommitted_artifact_content")
            ignored = subprocess.run(["git", "--no-optional-locks", "-C", str(repository), "check-ignore", "-q", "--", relative],
                                     capture_output=True, timeout=30)
            if ignored.returncode != 0:
                reasons.append("not_git_ignored")
            if path.name == "target":
                profiles = [path, *(p for p in path.iterdir() if p.is_dir() and not p.is_symlink())]
                if not ((path.parent / "Cargo.toml").is_file() and (path / ".rustc_info.json").is_file()
                        and any((base / profile / "deps").is_dir() for base in profiles for profile in ("debug", "release"))):
                    reasons.append("unconfirmed_rust_target")
                # Any custom target-dir can share caches with other workspaces. P0 excludes it.
                config_roots = {path.parent, *path.parent.parents, Path.home()}
                configs = {base / ".cargo" / name for base in config_roots for name in ("config", "config.toml")}
                configs.update(Path(os.environ.get("CARGO_HOME", str(Path.home() / ".cargo"))) / name for name in ("config", "config.toml"))
                for cfg in configs:
                    if cfg.is_file() and (cfg.is_symlink() or "target-dir" in cfg.read_text() or "build-dir" in cfg.read_text()):
                        reasons.append("custom_or_shared_build_directory")
                if os.environ.get("CARGO_TARGET_DIR") or os.environ.get("CARGO_BUILD_TARGET_DIR"):
                    reasons.append("custom_or_shared_build_directory")
            else:
                cache = path / "CMakeCache.txt"
                if not cache.is_file() or cache.is_symlink() or not (path / "CMakeFiles").is_dir():
                    reasons.append("unconfirmed_build_directory")
                else:
                    lines = cache.read_text().splitlines()
                    if (f"CMAKE_HOME_DIRECTORY:INTERNAL={path.parent}" not in lines or
                            f"CMAKE_CACHEFILE_DIR:INTERNAL={path}" not in lines):
                        reasons.append("custom_or_shared_build_directory")
            device = path.stat().st_dev
            identity = path.stat()
            row["identity"] = [identity.st_dev, identity.st_ino]
            files = 0
            seen = set()
            links = {}
            def onerror(error):
                raise error
            for current, dirs, names in os.walk(path, followlinks=False, onerror=onerror):
                for name in dirs + names:
                    child = Path(current) / name
                    info = child.lstat()
                    if info.st_dev != device or (stat.S_ISDIR(info.st_mode) and child.is_mount()):
                        reasons.append("mount_or_shared_directory")
                        if name in dirs:
                            dirs.remove(name)
                        continue
                    if stat.S_ISLNK(info.st_mode):
                        reasons.append("symlink_in_artifact")
                        continue
                    if (name in {".git", ".codex-gui-keep", "logs"} or
                            child.suffix.lower() in {".log", ".jsonl", ".ndjson", ".db", ".sqlite", ".sqlite3"} or
                            any(name.lower().endswith(ext + suffix) for ext in (".db", ".sqlite", ".sqlite3")
                                for suffix in ("-wal", "-shm", "-journal"))):
                        reasons.append("protected_content")
                    if path.name == "build" and name in {"Cargo.toml", "CMakeLists.txt", "package.json", "pyproject.toml"}:
                        reasons.append("source_manifest_in_build")
                    if stat.S_ISREG(info.st_mode):
                        files += 1
                        key = (info.st_dev, info.st_ino)
                        count, _ = links.get(key, (0, info.st_nlink))
                        links[key] = (count + 1, info.st_nlink)
                        if key not in seen:
                            row["bytes"] += info.st_blocks * 512
                            seen.add(key)
                        self._allocated[key] = info.st_blocks * 512
                        # Regular FILE access, write and metadata times; directory age is never evidence.
                        last[:] = [max(*last, info.st_atime, info.st_mtime, info.st_ctime)]
                    elif not stat.S_ISDIR(info.st_mode):
                        reasons.append("special_file")
            row["size_complete"] = "mount_or_shared_directory" not in reasons
            if any(count != nlink for count, nlink in links.values()):
                reasons.append("shared_hardlink")
            if not files:
                reasons.append("unknown_last_use")
            elif last:
                latest = max(last)
                row["last_used_at"] = iso(latest)
                if latest > self.now - self.retention_days * DAY:
                    reasons.append("within_retention")
            if any(Path(w["path"]) in busy for w in row["worktrees"]):
                reasons.append("worktree_process_active")
            if process_unknown:
                reasons.append("process_inspection_unavailable")
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            reasons.append(str(error) if isinstance(error, ValueError) else "inspection_failed")
        row["reasons"] = sorted(set(reasons))
        row["candidate"] = not row["reasons"]
        return row

    def scan(self):
        self.progress({"phase": "discovery"})
        paths, errors = self.discover()
        self.progress({"phase": "processes", "found": len(paths)})
        # Include every nested Git worktree and project root when checking processes.
        roots = {Path(t["worktree"]) for t in self.tasks}
        roots.update(path.parent for path in paths)
        try:
            busy, unknown = self.probe(roots) if paths else (set(), False)
        except OSError:
            busy, unknown = set(), True
        rows = []
        for index, path in enumerate(paths, 1):
            self.progress({"phase": "inspect", "item": index, "of": len(paths), "path": str(path)})
            rows.append(self.inspect(path, busy, unknown))
        if errors:
            for row in rows:
                row["candidate"] = False
                row["reasons"] = sorted(set([*row["reasons"], "discovery_incomplete"]))
        reasons = {}
        for row in rows:
            for reason in row["reasons"]:
                reasons[reason] = reasons.get(reason, 0) + 1
        return {"scanned_at": iso(self.now), "retention_days": self.retention_days, "items": rows,
                "candidate_count": sum(r["candidate"] for r in rows),
                "candidate_bytes": sum(r["bytes"] for r in rows if r["candidate"]),
                "total_bytes": sum(self._allocated.values()), "size_complete": not errors and all(r["size_complete"] for r in rows),
                "excluded_reasons": reasons,
                "discovery_errors": errors, "released_bytes": 0, "dry_run": True}


def remove_verified(path, root, identity):
    """Anchor removal to directory FDs; neither ancestor nor target symlink substitution is followed."""
    if not shutil.rmtree.avoids_symlink_attacks:
        raise ValueError("safe_removal_unavailable")
    relative = path.relative_to(root)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in (*root.parts[1:], *relative.parts[:-1]):
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        info = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
        if [info.st_dev, info.st_ino] != identity or not stat.S_ISDIR(info.st_mode):
            raise ValueError("artifact_changed")
        if sys.version_info >= (3, 11):
            shutil.rmtree(path.name, dir_fd=fd)
        else:
            # Python 3.10 has FD-safe rmtree but no public dir_fd argument; Linux /proc anchors its parent.
            shutil.rmtree(f"/proc/self/fd/{fd}/{path.name}")
    finally:
        os.close(fd)


class ArtifactGC:
    def __init__(self, home, db):
        self.home, self.db = Path(home), db

    def settings(self):
        values = self.db.get_settings("artifact_gc.")
        return {"retention_days": int(values.get("artifact_gc.retention_days", "5")),
                "auto_delete": values.get("artifact_gc.auto_delete", "false") == "true"}

    def configure(self, retention_days, auto_delete):
        if not 1 <= retention_days <= 3650:
            raise ValueError("retention_days must be between 1 and 3650")
        self.db.set_setting("artifact_gc.retention_days", str(retention_days))
        self.db.set_setting("artifact_gc.auto_delete", json.dumps(bool(auto_delete)))
        return self.settings()

    def due(self):
        rows = self.db.gc_history(1)
        return not rows or time.time() - stamp(rows[0]["started_at"]) >= DAY

    def run(self, dry_run=True, trigger="manual"):
        with launch_guard(self.home, exclusive=True, filename="artifact-gc-run.lock") as acquired:
            if not acquired:
                return {"status": "busy", "dry_run": dry_run, "released_bytes": 0}
            return self._run(dry_run, trigger)

    def _run(self, dry_run, trigger):
        # The CLI intentionally does not use this write-capable service.
        with (nullcontext(True) if dry_run else launch_guard(self.home, exclusive=True)) as acquired:
            if not acquired:
                return {"status": "busy", "dry_run": dry_run, "released_bytes": 0}
            config = self.settings()
            if not dry_run and not config["auto_delete"]:
                raise ValueError("Enable automatic deletion in settings before deleting artifacts")
            if trigger == "daily" and not self.due():
                return {"status": "not_due"}
            run_id = self.db.gc_start(dry_run, trigger)
            report = {"dry_run": dry_run, "released_bytes": 0, "items": []}
            try:
                report = Scanner(self.home, self.db.gc_tasks(), config["retention_days"]).scan()
                report.update(dry_run=dry_run, status="completed", run_id=run_id)
                if not dry_run:
                    for row in report["items"]:
                        if not row["candidate"]:
                            continue
                        # Refresh DB, processes, Git and EVERY file immediately before removing this target.
                        scanner = Scanner(self.home, self.db.gc_tasks(), self.settings()["retention_days"])
                        paths, errors = scanner.discover()
                        roots = {Path(t["worktree"]) for t in scanner.tasks}
                        roots.add(Path(row["path"]).parent)
                        busy, unknown = scanner.probe(roots)
                        check = scanner.inspect(Path(row["path"]), busy, unknown) if Path(row["path"]) in paths and not errors else None
                        if not self.settings()["auto_delete"] or not check or not check["candidate"] or check.get("identity") != row.get("identity"):
                            if check:
                                row.update(check)
                            row.update(candidate=False, reasons=(check or {}).get("reasons") or ["changed_before_delete"])
                            continue
                        try:
                            remove_verified(Path(row["path"]), scanner.root, row["identity"])
                            row["deleted"] = True
                            report["released_bytes"] += check["bytes"]
                        except (OSError, ValueError) as error:
                            row.update(deleted=False, candidate=False, reasons=[f"delete_failed: {error}"])
                            report["status"] = "partial"
                            report["released_bytes_incomplete"] = True
                    report.update(candidate_count=sum(r["candidate"] for r in report["items"]),
                                  candidate_bytes=sum(r["bytes"] for r in report["items"] if r["candidate"]),
                                  excluded_reasons=dict(Counter(reason for r in report["items"] for reason in r["reasons"])))
                return report
            except Exception as error:
                report.update(status="failed", error=str(error))
                raise
            finally:
                self.db.gc_finish(run_id, report)


def main():
    parser = argparse.ArgumentParser(description="Read-only build artifact GC analysis; never deletes or writes the production DB")
    parser.add_argument("--home", type=Path, default=Path(os.environ.get("CODEX_GUI_HOME", "~/.local/share/codex-gui")).expanduser())
    parser.add_argument("--retention-days", type=int, default=5)
    parser.add_argument("--output", type=Path, help="Save JSON outside the production data directory")
    parser.add_argument("--progress", action="store_true", help="Print scan progress to stderr")
    args = parser.parse_args()
    if not 1 <= args.retention_days <= 3650:
        parser.error("retention must be between 1 and 3650 days")
    if args.output and (args.output.absolute().is_relative_to(args.home.resolve()) or
                        args.output.resolve().is_relative_to(args.home.resolve())):
        parser.error("output must be outside CODEX_GUI_HOME")
    with sqlite3.connect(f"file:{args.home / 'codex-gui.db'}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        tasks = [dict(row) for row in conn.execute(TASK_SNAPSHOT_SQL)]
    progress = (lambda event: print(json.dumps(event), file=sys.stderr, flush=True)) if args.progress else None
    report = Scanner(args.home, tasks, args.retention_days, progress=progress).scan()
    report["coordination"] = "read_only_snapshot; production process is not modified; deletion requires the deployed launch gate"
    encoded = json.dumps(report, ensure_ascii=True, indent=2)
    if args.output:
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as output:
            output.write(encoded + "\n")
        print(json.dumps({k: v for k, v in report.items() if k != "items"}, ensure_ascii=True))
    else:
        print(encoded)


if __name__ == "__main__":
    main()
