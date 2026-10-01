"""AGENTS.md health check: which instruction files Codex reads for a working directory, how big they are against
`project_doc_max_bytes`, and heuristic warnings. READ-ONLY by design: this module only ever opens files for reading;
it never edits, moves, deletes or rewrites an AGENTS.md. Fixing a problem is the user's job (the Edit button).

How Codex builds the project instructions (verified with codex-cli 0.159.2 through `codex debug prompt-input`):
* global:  $CODEX_HOME/AGENTS.override.md, else $CODEX_HOME/AGENTS.md. It comes first and is NOT part of the budget.
* project: from the project root (nearest ancestor holding a `project_root_markers` entry, default `.git`) down to the
  working directory, one file per directory (`AGENTS.override.md`, else `AGENTS.md`, else a
  `project_doc_fallback_filenames` entry), joined in that order.
* `project_doc_max_bytes` (default 32768) is one cumulative budget for the project files only. The file that crosses it
  is cut at the budget and every later (deeper) file is left out entirely.
"""
import os
import re
from pathlib import Path
from typing import Optional

from .tokens import est_tokens

DEFAULT_MAX_BYTES = 32 * 1024
WARNING_PERCENT = 75
CRITICAL_PERCENT = 100
OVERRIDE = "AGENTS.override.md"
FILENAME = "AGENTS.md"
DEFAULT_ROOT_MARKERS = (".git",)
READ_LIMIT = 4 * 1024 * 1024  # never read more than this much of one file

# (id, pattern, message): phrases that tell the model to load a lot of context every time.
HEURISTICS = [
    ("always_read", re.compile(r"\balways\s+(?:read|load|open|review)\b", re.I), 'says to "always read" something'),
    ("every_task", re.compile(r"\bbefore\s+(?:every|each|any|starting)\s+(?:task|change|edit|commit|work)\b", re.I),
     'asks for a step "before every task"'),
    ("read_all", re.compile(r"\bread\s+(?:all|every|each|the\s+entire|the\s+whole)\b", re.I), 'says to "read all" / "read every" file'),
    ("read_first", re.compile(r"\b(?:read|review)\s+(?:\S+\s+){0,3}first\b|\bfirst,?\s+read\b", re.I), 'asks to read documents first'),
    ("jp_always", re.compile(r"(?:毎回|常に|必ず).{0,12}(?:読|参照|確認)"), "tells the agent to always read / consult documents (JP)"),
    ("jp_all", re.compile(r"(?:すべて|全て|全部).{0,10}(?:読|参照)"), "tells the agent to read everything (JP)"),
]
DOC_REF = re.compile(r"(?<![\w/.-])(?:[\w.-]+/)*[\w.-]+\.(?:md|mdx|txt|rst|adoc)\b")
MANY_REFS = 8
DUP_MIN_LINE = 25      # characters of a line to count as meaningful
DUP_MIN_SHARED = 3     # shared meaningful lines before two files are called overlapping


def codex_home(env: Optional[dict] = None) -> Path:
    return Path((env or os.environ).get("CODEX_HOME") or "~/.codex").expanduser()


def _read(path: Path) -> Optional[bytes]:
    try:
        with open(path, "rb") as f:  # read-only, always
            return f.read(READ_LIMIT)
    except OSError:
        return None


def find_project_root(cwd: Path, markers=None) -> Path:
    """Nearest ancestor (or cwd itself) holding a root marker; the working directory itself when there is none."""
    markers = DEFAULT_ROOT_MARKERS if markers is None else tuple(markers)  # [] = no markers: only cwd counts
    for d in (cwd, *cwd.parents):
        if any((d / m).exists() for m in markers):
            return d
    return cwd


def _pick(directory: Path, fallbacks=()) -> Optional[Path]:
    for name in (OVERRIDE, FILENAME, *fallbacks):
        p = directory / name
        if p.is_file():
            data = _read(p)
            if data is not None and data.strip():  # an empty file does not count
                return p
    return None


def chain_paths(cwd: Path, markers=None, fallbacks=()) -> tuple[Path, list[Path]]:
    """(project root, the project instruction files from the root down to cwd)."""
    root = find_project_root(cwd, markers)
    rel = cwd.relative_to(root)
    dirs = [root]
    cur = root
    for part in rel.parts:
        cur = cur / part
        dirs.append(cur)
    files = [f for f in (_pick(d, fallbacks) for d in dirs) if f]
    return root, files


def _lines(text: str) -> list[tuple[int, str]]:
    return [(i, ln) for i, ln in enumerate(text.splitlines(), 1)]


def _norm(line: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"^[#>\-*\d.)\s]+", "", line.strip().lower()))


def heuristic_warnings(text: str) -> list[dict]:
    out = []
    for hid, pat, msg in HEURISTICS:
        for n, ln in _lines(text):
            if pat.search(ln):
                out.append({"id": hid, "line": n, "message": msg, "excerpt": ln.strip()[:160]})
                break  # one hit per pattern is enough to flag the file
    refs = {m.group(0) for m in DOC_REF.finditer(text)}
    if len(refs) >= MANY_REFS:
        out.append({"id": "many_refs", "line": None, "excerpt": "",
                    "message": f"names {len(refs)} reference documents; if Codex is told to read them, they all enter the context"})
    return out


def _meaningful(text: str) -> dict[str, tuple[int, str]]:
    seen: dict[str, tuple[int, str]] = {}
    for n, ln in _lines(text):
        key = _norm(ln)
        if len(key) >= DUP_MIN_LINE and not set(key) <= set("-=_*#| "):
            seen.setdefault(key, (n, ln.strip()))
    return seen


def duplicates(files: list[dict], texts: dict[str, str]) -> list[dict]:
    """Pairs of files that repeat the same lines. kind: global_project | root_nested | project."""
    out = []
    keyed = {f["path"]: _meaningful(texts[f["path"]]) for f in files}
    for i, a in enumerate(files):
        for b in files[i + 1:]:
            ka, kb = keyed[a["path"]], keyed[b["path"]]
            shared = sorted(set(ka) & set(kb), key=lambda k: ka[k][0])
            smaller = min(len(ka), len(kb)) or 1
            if len(shared) >= DUP_MIN_SHARED:
                kind = "global_project" if "global" in (a["scope"], b["scope"]) else "root_nested"
                out.append({"kind": kind, "a": a["path"], "b": b["path"], "shared_lines": len(shared),
                            "share_percent": round(len(shared) / smaller * 100), "examples": [ka[k][1][:120] for k in shared[:3]]})
    return out


def audit(cwd, *, max_bytes: Optional[int] = None, markers=None, fallbacks=(), home: Optional[Path] = None) -> dict:
    """The AGENTS.md chain of `cwd`. `max_bytes` / `markers` / `fallbacks` should be Codex's effective config values
    (config/read); when unknown the defaults are used and `budget_source` says so."""
    cwd = Path(cwd).resolve()
    budget = max_bytes if isinstance(max_bytes, int) and max_bytes >= 0 else DEFAULT_MAX_BYTES
    source = "codex config" if isinstance(max_bytes, int) and max_bytes >= 0 else "default"
    root, project = chain_paths(cwd, markers, fallbacks)
    ghome = home or codex_home()
    global_file = _pick(ghome) if ghome.is_dir() else None

    files: list[dict] = []
    texts: dict[str, str] = {}

    def add(path: Path, scope: str) -> dict:
        data = _read(path) or b""
        size = path.stat().st_size if path.exists() else len(data)
        text = data.decode("utf-8", errors="replace")
        texts[str(path)] = text
        entry = {"path": str(path), "scope": scope, "name": path.name, "bytes": size, "tokens_est": est_tokens(text),
                 "lines": len(text.splitlines()), "warnings": heuristic_warnings(text)}
        if scope == "project":
            try:
                entry["relative"] = str(path.relative_to(root))
            except ValueError:
                entry["relative"] = path.name
        files.append(entry)
        return entry

    if global_file:
        g = add(global_file, "global")
        g.update(cumulative_bytes=None, included_bytes=g["bytes"], status="included")
    cumulative, remaining = 0, budget
    for p in project:
        f = add(p, "project")
        cumulative += f["bytes"]
        inc = max(min(f["bytes"], remaining), 0)
        remaining -= inc
        f.update(cumulative_bytes=cumulative, included_bytes=inc,
                 status="included" if inc >= f["bytes"] else "truncated" if inc > 0 else "dropped")

    percent = cumulative / budget * 100 if budget else (100.0 if cumulative else 0.0)
    level = "critical" if percent >= CRITICAL_PERCENT else "warning" if percent >= WARNING_PERCENT else "ok"
    cut = [f for f in files if f["scope"] == "project" and f["status"] != "included"]
    truncation = None
    if cut:
        truncation = ("Codex stops reading project instructions at project_doc_max_bytes; " +
                      "; ".join(f"{f['relative']} is {'cut at ' + format(f['included_bytes'], ',') + ' bytes' if f['status'] == 'truncated' else 'left out entirely'}"
                                for f in cut) + ". Nested instructions near the working directory are the ones lost.")
    dups = duplicates(files, texts) if len(files) > 1 else []
    return {
        "cwd": str(cwd), "project_root": str(root), "files": files,
        "budget": {"max_bytes": budget, "source": source, "project_bytes": cumulative, "used_percent": round(percent, 1),
                   "level": level, "warning_percent": WARNING_PERCENT, "critical_percent": CRITICAL_PERCENT},
        "global_bytes": files[0]["bytes"] if global_file else 0,
        "total_tokens_est": sum(f["tokens_est"] for f in files),
        "truncation": truncation, "duplicates": dups,
        "warnings": [dict(w, path=f["path"]) for f in files for w in f["warnings"]],
        "read_only": True,
    }
