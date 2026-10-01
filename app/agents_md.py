"""Reading, editing and inspecting AGENTS.md files of a repository or of a task worktree.

Two scopes, deliberately kept apart:
- repository: <main checkout>/AGENTS.md, the project rules every future task starts from;
- task worktree: <worktree>/AGENTS.md, a copy that belongs to one task (experiments, special rules).
Editing one never touches the other. The GUI never puts an AGENTS.md into a prompt: Codex reads the file itself
(it reports the files it loaded as `instructionSources`), so nothing is duplicated and the prompt prefix stays stable.
"""
import hashlib
import os
from pathlib import Path, PurePosixPath
from typing import Optional

from . import git_manager as git

FILENAME = "AGENTS.md"
MAX_BYTES = 1024 * 1024
MAX_NESTED = 200
MAX_DIFF_BYTES = 512 * 1024


class AgentsError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def resolve(root, rel: str = FILENAME) -> Path:
    """The file for a path relative to `root`. Only files named AGENTS.md inside the root can be addressed."""
    rel = (rel or FILENAME).strip()
    pure = PurePosixPath(rel)
    if pure.is_absolute() or ".." in pure.parts or "\\" in rel or "\x00" in rel or pure.name != FILENAME:
        raise AgentsError(f"not an AGENTS.md path inside the repository: {rel}")
    root_real = Path(root).resolve()
    target = root_real / pure
    try:
        parent = target.parent.resolve()
        parent.relative_to(root_real)
        if target.exists():
            target.resolve().relative_to(root_real)  # a symlink must not lead out of the repository
    except (ValueError, OSError):
        raise AgentsError(f"path leaves the repository: {rel}") from None
    if not target.parent.is_dir():
        raise AgentsError(f"directory does not exist: {pure.parent}", 404)
    return target


def _read_bytes(path: Path) -> Optional[bytes]:
    try:
        if path.stat().st_size > MAX_BYTES:
            raise AgentsError(f"{path.name} is larger than {MAX_BYTES // 1024} KiB", 413)
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except IsADirectoryError:
        raise AgentsError(f"{path.name} is a directory", 409) from None


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise AgentsError("AGENTS.md is not UTF-8 text; edit it outside the GUI", 415) from None


async def file_state(root, rel: str = FILENAME) -> dict:
    """{"state", "short"}: state is missing | untracked | modified | added | deleted | clean | ignored | conflict."""
    path = resolve(root, rel)
    code, out, _ = await git.run_git(root, "status", "--porcelain=v1", "--untracked-files=all", "--", rel, check=False)
    short = out.rstrip("\n").splitlines()[0] if out.strip() else ""
    exists = path.exists()
    code_xy = short[:2]
    if code_xy == "??":
        state = "untracked"
    elif "U" in code_xy or code_xy in ("AA", "DD"):
        state = "conflict"
    elif code_xy.strip() == "A":
        state = "added"
    elif "D" in code_xy:
        state = "deleted"
    elif short:
        state = "modified"
    elif not exists:
        state = "missing"
    else:
        tracked = (await git.run_git(root, "ls-files", "--error-unmatch", "--", rel, check=False))[0] == 0
        state = "clean" if tracked else "ignored"
    return {"state": state, "short": short}


async def read(root, rel: str = FILENAME) -> dict:
    path = resolve(root, rel)
    data = _read_bytes(path)
    text = _decode(data) if data is not None else ""
    crlf = "\r\n" in text
    return {
        "path": rel or FILENAME, "exists": data is not None,
        "content": text.replace("\r\n", "\n"), "crlf": crlf,  # the textarea works with \n; the file's EOL is restored on save
        "sha256": sha256(data) if data is not None else "", "size": len(data) if data is not None else 0,
        "git": await file_state(root, rel),
    }


async def write(root, rel: str, content: str, expected_sha: Optional[str] = None) -> dict:
    """Save `content` as the file. `expected_sha` is the sha256 the editor loaded ("" = the file was absent):
    if the file changed on disk since, nothing is written (409) so another editor's work is not overwritten."""
    path = resolve(root, rel)
    current = _read_bytes(path)
    if expected_sha is not None and (sha256(current) if current is not None else "") != expected_sha:
        raise AgentsError("AGENTS.md changed on disk since it was loaded; reload it before saving", 409, "changed")
    if "\x00" in content:
        raise AgentsError("AGENTS.md must be text")
    if current is not None and "\r\n" in current.decode("utf-8", errors="replace") and "\r\n" not in content:
        content = content.replace("\n", "\r\n")  # keep a CRLF file CRLF
    data = content.encode("utf-8")
    if len(data) > MAX_BYTES:
        raise AgentsError(f"AGENTS.md may not be larger than {MAX_BYTES // 1024} KiB", 413)
    created = current is None
    if current == data:
        return {"changed": False, "created": False, "sha256": sha256(data)}
    path.write_bytes(data)  # in place: keeps a symlink, the mode and the owner of the file
    return {"changed": True, "created": created, "sha256": sha256(data)}


async def diff(root, rel: str = FILENAME) -> dict:
    """`git diff HEAD -- AGENTS.md` (staged and unstaged); an untracked file is shown as all-added."""
    path = resolve(root, rel)
    st = await file_state(root, rel)
    if st["state"] == "untracked":
        _, out, _ = await git.run_git(root, "diff", "--no-index", "--", os.devnull, str(path), check=False)
    else:
        code, out, _ = await git.run_git(root, "diff", "HEAD", "--", rel, check=False)
        if code != 0:  # no commit yet: staged and unstaged parts separately
            out = ((await git.run_git(root, "diff", "--cached", "--", rel, check=False))[1] +
                   (await git.run_git(root, "diff", "--", rel, check=False))[1])
    if len(out.encode()) > MAX_DIFF_BYTES:
        out = out.encode()[:MAX_DIFF_BYTES].decode(errors="ignore") + "\n... (truncated)\n"
    return {"state": st["state"], "short": st["short"], "diff": out}


async def find_all(root) -> list[str]:
    """Every AGENTS.md of the repository (tracked or not yet ignored), relative paths, the root one first."""
    _, out, _ = await git.run_git(
        root, "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", f":(glob)**/{FILENAME}", check=False)
    found = sorted({p for p in out.split("\0") if p}, key=lambda p: (p.count("/"), p))
    return found[:MAX_NESTED]


async def main_worktree_only(root) -> None:
    """Repository scope edits the main checkout. A linked worktree (a task's) is a different thing."""
    _, gdir, _ = await git.run_git(root, "rev-parse", "--path-format=absolute", "--git-dir", check=False)
    _, common, _ = await git.run_git(root, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False)
    if gdir.strip() and common.strip() and os.path.realpath(gdir.strip()) != os.path.realpath(common.strip()):
        raise AgentsError("this is a linked worktree (a task's copy), not the main repository; "
                          "open it from the task's \"Edit Worktree AGENTS.md\"", 409, "linked_worktree")


async def repo_info(root) -> dict:
    """What the dashboard shows for a repository: git cleanliness and whether AGENTS.md exists."""
    files = await find_all(root)
    status = (await git.run_git(root, "status", "--porcelain", check=False))[1]
    dirty = [ln for ln in status.splitlines() if ln.strip()]
    return {
        "repository": str(root),
        "git": {"clean": not dirty, "changed": len(dirty), "label": "clean" if not dirty else f"{len(dirty)} changed"},
        "agents_md": {"found": FILENAME in files, "nested": [f for f in files if f != FILENAME]},
    }
