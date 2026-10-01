"""Server-side folder listing for the repository picker (the browser cannot reveal real paths itself)."""
import os
from pathlib import Path

MAX_ENTRIES = 1000


class BrowseError(Exception):
    pass


def is_git_dir(path: Path) -> bool:
    # a directory for a clone, a file for a worktree. os.path.exists() (unlike Path.exists on 3.10)
    # returns False instead of raising for folders we may not read.
    return os.path.exists(path / ".git")


def list_dir(path: str = "", show_hidden: bool = False) -> dict:
    p = Path(path.strip() or "~").expanduser()
    try:
        p = p.resolve(strict=True)
    except OSError:
        raise BrowseError(f"no such folder: {path}")
    if not p.is_dir():
        raise BrowseError(f"not a folder: {p}")
    entries = []
    try:
        with os.scandir(p) as it:
            for e in it:
                if (not show_hidden and e.name.startswith(".")) or not e.is_dir():
                    continue
                entries.append({"name": e.name, "path": str(p / e.name), "is_git": is_git_dir(p / e.name)})
    except PermissionError:
        raise BrowseError(f"permission denied: {p}")
    entries.sort(key=lambda x: x["name"].lower())
    return {"path": str(p), "parent": str(p.parent) if p.parent != p else None, "is_git": is_git_dir(p),
            "entries": entries[:MAX_ENTRIES], "truncated": len(entries) > MAX_ENTRIES}
