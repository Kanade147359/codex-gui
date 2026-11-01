"""Build an inspectable, non-executing Autopilot task contract from a pinned GitHub Issue.

Issue text is untrusted requirements data, never an authorization for edit scope,
validation commands, billing or publication. No task/workflow is created here.
"""

import asyncio
import hashlib
import json
import re
from pathlib import Path

from . import git
from .policy import manifest, safe_path

MAX_ISSUE_BYTES = 128 * 1024
MAX_CONTEXT_BYTES = 64 * 1024
MAX_CONTEXT_FILES = 8
URL = re.compile(r"\Ahttps://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/issues/([1-9][0-9]*)/?\Z")
REMOTE = re.compile(
    r"\A(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?\Z"
)
HEADING = re.compile(
    r"^\s*#{1,6}\s*(?:acceptance criteria|definition of done|受け入れ条件|完了条件|検収条件)\s*:?\s*$",
    re.IGNORECASE,
)
ITEM = re.compile(r"^\s*(?:[-*]\s*(?:\[[ xX]\]\s*)?|\d+[.)]\s+)(\S.*)$")
PATH_REF = re.compile(r"(?<![\w./])(?:[\w.-]+/)*[\w.-]+\.(?:md|py|rs|toml|yaml|yml)(?![\w])")
ADR_REF = re.compile(r"\bADR[-_ ]?0*(\d{1,5})\b", re.IGNORECASE)


def issue_identity(url):
    if not isinstance(url, str):
        raise ValueError("a GitHub Issue URL is required")
    match = URL.fullmatch(url)
    if not match:
        raise ValueError("use an exact https://github.com/owner/repo/issues/N URL without query or fragment")
    return f"{match[1]}/{match[2]}", int(match[3])


async def _issue_from_gh(slug, number):
    """Use the locally authenticated gh CLI; no shell, token echo or arbitrary URL fetching."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh", "issue", "view", str(number), "--repo", slug,
            "--json", "number,title,body,state,url,updatedAt",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise ValueError("gh CLI is required; install gh and authenticate with gh auth login") from exc
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
    except asyncio.TimeoutError as exc:
        proc.kill()
        await proc.communicate()
        raise ValueError("GitHub Issue lookup timed out") from exc
    if proc.returncode:
        raise ValueError("GitHub Issue unavailable; check gh authentication and repository access")
    if len(out) > MAX_ISSUE_BYTES:
        raise ValueError("GitHub Issue response exceeds size limit")
    try:
        issue = json.loads(out)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("invalid GitHub Issue response") from exc
    if not isinstance(issue, dict):
        raise ValueError("invalid GitHub Issue response")
    return issue


def acceptance_criteria(body):
    """Only capture explicitly enumerated acceptance items; never invent a test oracle."""
    active, criteria = False, []
    for line in body.splitlines():
        if HEADING.fullmatch(line):
            active = True
            continue
        if active and re.match(r"^\s*#{1,6}\s+", line):
            break
        if active:
            match = ITEM.match(line)
            if match:
                criterion = match[1].strip()
                if criterion and criterion not in criteria:
                    criteria.append(criterion)
    return criteria[:30]


async def _paths_at_base(repository, base_sha):
    _, data, _ = await git.git(repository, "ls-tree", "-r", "--name-only", "-z", base_sha)
    return {p for p in data.split("\0") if p}


def _references(body, tree):
    """Collect only explicitly named tracked sources and ADR identifiers."""
    ordered, missing = [], []
    for match in PATH_REF.finditer(body):
        name = match.group(0)
        try:
            name = safe_path(name)
        except ValueError:
            continue
        if name in tree:
            if name not in ordered:
                ordered.append(name)
        elif name.startswith(("docs/", "app/", "tests/")):
            if name not in missing:
                missing.append(name)
    for match in ADR_REF.finditer(body):
        number = int(match[1])
        candidates = sorted(p for p in tree if p.lower().endswith(".md")
                            and re.search(r"(?<!\d)0*" + str(number) + r"(?!\d)", Path(p).name)
                            and "adr" in p.lower())
        if not candidates:
            missing.append("ADR-" + str(number))
        for name in candidates:
            if name not in ordered:
                ordered.append(name)
    return ordered, missing


async def build(ap, raw):
    """Generate a draft or runnable contract; only existing controller dry-run may authorize it."""
    if not isinstance(raw, dict) or set(raw) - {"repository", "base_ref", "issue_url", "paths", "validation"}:
        raise ValueError("expected repository, issue_url, optional base_ref/paths/validation")
    slug, number = issue_identity(raw.get("issue_url"))
    repository = raw.get("repository")
    if not isinstance(repository, str) or not repository.strip():
        raise ValueError("repository must be an absolute local Git root")
    root = Path(repository).expanduser().resolve()
    from app.git_manager import repo_toplevel

    if not root.is_dir() or await repo_toplevel(root) != str(root):
        raise ValueError("repository must be an existing local Git root")
    _, remote, _ = await git.git(root, "remote", "get-url", "origin")
    match = REMOTE.fullmatch(remote.strip())
    if not match or f"{match[1]}/{match[2]}".lower() != slug.lower():
        raise ValueError("Issue repository does not match the local repository origin")
    base_ref = raw.get("base_ref", "main")
    if not isinstance(base_ref, str) or not re.fullmatch(r"[A-Za-z0-9_./-]{1,128}", base_ref) or (
        base_ref.startswith("-") or ".." in base_ref or base_ref.endswith(".lock")
    ):
        raise ValueError("invalid base ref")
    base_sha = await git.sha(root, base_ref)
    issue = await _issue_from_gh(slug, number)
    if (not isinstance(issue.get("body"), str) or not isinstance(issue.get("title"), str)
        or not isinstance(issue.get("url"), str) or issue.get("number") != number
        or issue["url"].rstrip("/") != raw["issue_url"].rstrip("/")
        or not isinstance(issue.get("updatedAt"), str)):
        raise ValueError("GitHub Issue identity or required fields are invalid")
    body, title = issue["body"], issue["title"].strip()
    if (not title or not body.strip() or "\x00" in body
        or len(body.encode("utf-8")) > MAX_ISSUE_BYTES or len(title.encode("utf-8")) > 1024):
        raise ValueError("Issue title/body is empty or oversized")
    tree = await _paths_at_base(root, base_sha)
    refs, missing = _references(body, tree)
    blockers = []
    if issue.get("state") != "OPEN":
        blockers.append("Issue is not OPEN")
    criteria = acceptance_criteria(body)
    if not criteria:
        blockers.append("Explicit bullet acceptance criteria are missing (Acceptance Criteria / 受け入れ条件)")
    if missing:
        blockers.append("Issue references unresolved paths or ADRs: " + ", ".join(missing[:12]))
    if len(refs) > MAX_CONTEXT_FILES:
        blockers.append("Context references exceed 8 files; narrow the Issue references")
    inputs = {
        "issue.md": "# " + title + "\n\nSource: " + issue["url"]
                    + "\nUpdated: " + issue["updatedAt"] + "\n\n" + body + "\n"
    }
    context = []
    for name in refs[:MAX_CONTEXT_FILES]:
        # Frozen AGENTS/CLAUDE rules are already collected by Autopilot.dry_run.
        entry = dict(path=name, sha=base_sha, kind="code" if not name.endswith(".md") else "document")
        if name.endswith(".md") and Path(name).name not in ("AGENTS.md", "CLAUDE.md"):
            _, kind, _ = await git.git(root, "ls-tree", base_sha, "--", name)
            if not kind.startswith(("100644 ", "100755 ")):
                blockers.append("Referenced document is not a regular Git file: " + name)
                continue
            try:
                from .inputs import accepted_text
                content = await accepted_text(root, base_sha, name)
            except (ValueError, UnicodeError):
                blockers.append("Referenced document cannot be read as bounded UTF-8: " + name)
                continue
            if len(content.encode("utf-8")) > MAX_CONTEXT_BYTES:
                blockers.append("Referenced document exceeds 64 KiB: " + name)
                continue
            frozen = "context/" + name
            inputs[frozen] = content
            entry["input_file"] = frozen
            entry["digest"] = hashlib.sha256(content.encode()).hexdigest()
        context.append(entry)
    paths = raw.get("paths", [])
    validation = raw.get("validation", {})
    if not isinstance(paths, list) or any(not isinstance(p, str) for p in paths):
        raise ValueError("paths must be an owner-approved list")
    if not isinstance(validation, dict):
        raise ValueError("validation must be an owner-approved profile")
    if not paths:
        blockers.append("Owner-approved edit paths are missing")
    if not validation.get("commands"):
        blockers.append("Owner-approved validation commands are missing")
    if paths and validation.get("commands"):
        from .policy import profile
        profile(validation, "code")  # Validate before asserting readiness.
    goal_entries = [dict(id="AC-" + str(i), kind="goal", text=c, source="issue.md")
                    for i, c in enumerate(criteria, 1)]
    node = dict(
        id="issue-" + str(number), kind="code", paths=paths, validation=validation,
        input_files=list(inputs), goal_ids=[g["id"] for g in goal_entries],
        draft=bool(blockers), contract={"require_any_change": True, "require_commit": True},
        prompt=("Implement the pinned issue.md requirements. Follow base-SHA repository rules and "
                "referenced context. Do not invent missing conditions or change permissions. "
                "Use the explicit acceptance goals and approved validation profile."),
    )
    raw_manifest = dict(
        version=1, name="Issue #" + str(number) + ": " + title,
        repository=str(root), base_ref=base_ref, inputs=inputs, goals=goal_entries,
        nodes=[node], permissions={"commit_owned_changes": False, "publish_main": False},
        policy={"automatic_acceptance": False},
    )
    manifest(raw_manifest)  # The same schema is used by normal Autopilot registration.
    preview = await ap.dry_run(raw_manifest)
    return dict(
        ready=not blockers, blockers=blockers, issue=dict(url=issue["url"], number=number,
        updated_at=issue["updatedAt"], body_digest=hashlib.sha256(body.encode()).hexdigest()),
        context=context, acceptance_criteria=criteria, manifest=raw_manifest, preview=preview,
        note="Structural completeness only; semantic requirements and test oracles need independent review.",
    )
