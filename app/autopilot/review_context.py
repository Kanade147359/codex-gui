"""Read only fixed snapshots, partition every diff byte, and aggregate all reviews."""

import codecs
from pathlib import Path
import re

from .git import git
from .inputs import bytes_digest
from .policy import canonical, digest, safe_path

PAGE = 64 * 1024
MAX_PARTITIONS = 256


def read(root, path, start=1, end=240):
    """Controller-mediated bounded read. No model shell, network, or host paths."""
    path = safe_path(path)
    root = Path(root).resolve()
    target = root / path
    if (
        path == "."
        or path.startswith((".autopilot-", ".git/"))
        or not target.resolve().is_relative_to(root)
        or any(p.is_symlink() for p in [target, *target.parents] if p.is_relative_to(root))
    ):
        raise ValueError("review read escapes the fixed source snapshot")
    if type(start) is not int or type(end) is not int or not 1 <= start <= end <= start + 239:
        raise ValueError("review reads require a range of at most 240 lines")
    lines, eof = [], False
    with target.open("r", encoding="utf-8") as stream:
        for number in range(1, end + 1):
            line = stream.readline(PAGE + 1)
            if not line:
                eof = True
                break
            if len(line.encode()) > PAGE or "\x00" in line:
                raise ValueError("binary or oversized source line requires explicit review")
            if number >= start:
                lines.append(line)
    text = "".join(lines)
    if len(text.encode()) > PAGE:
        raise ValueError("requested source range exceeds page size")
    return dict(
        path=path, start=start, end=start + len(lines) - 1, text=text, eof=eof, digest=bytes_digest(text.encode())
    )


async def partitions(candidate, root):
    p = candidate["payload"]
    pages = []
    for path in p["changed_paths"]:
        decoder, pending, pieces = codecs.getincrementaldecoder("utf-8")("replace"), "", []

        def consume(data):
            nonlocal pending
            pending += decoder.decode(data)
            while len(pending) >= PAGE:
                cut = pending.rfind("\n", 0, PAGE) + 1 or PAGE
                pieces.append(pending[:cut])
                pending = pending[cut:]
                if len(pieces) + len(pages) > MAX_PARTITIONS:
                    raise ValueError("review partition budget exceeded; split the task explicitly")

        await git(
            root,
            "diff",
            "--binary",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            p["scope"]["base_sha"],
            p["scope"]["head_sha"],
            "--",
            path,
            _stdout_consumer=consume,
        )
        pending += decoder.decode(b"", final=True)
        if pending or not pieces:
            pieces.append(pending)
        for i, text in enumerate(pieces):
            pages.append(
                dict(path=path, segment=i + 1, segments=len(pieces), diff=text, digest=bytes_digest(text.encode()))
            )
    if len(pages) > MAX_PARTITIONS:
        raise ValueError("review partition budget exceeded")
    # Unchanged repository files are never enumerated/read wholesale. Explicit and
    # local import references can be obtained via bounded, snapshot-only reads.
    related = []
    requested = set(p.get("review_files", []))
    for page in pages:
        path = Path(page["path"])
        if path.suffix == ".py":
            for module in re.findall(r"(?:from|import) ([a-zA-Z_][\w.]*)", page["diff"]):
                requested.update(
                    (str(path.parent / (module.replace(".", "/") + ".py")), module.replace(".", "/") + ".py")
                )
    if len(requested) > 40:
        raise ValueError("related code read budget exceeded; declare review_files explicitly")
    for path in sorted(requested):
        if (Path(root) / path).is_file():
            related.append(read(root, path))
    if not pages:
        pages = [dict(path="", segment=1, segments=1, diff="", digest=digest(""))]
    return pages, related


async def prompt(candidate, root):
    p = candidate["payload"]
    context = p.get("review_context")
    if context is None:
        pages, related = await partitions(candidate, root)
        if len(pages) != 1:
            raise ValueError("controller partition review required")
        context = dict(partition=pages[0], related=related)
    return canonical(
        dict(
            candidate_id=candidate["id"],
            run_generation=candidate["run_generation"],
            scope=p["scope"],
            contract=p["contract"],
            acceptance=p["acceptance"],
            recorded_completion=p.get("recorded_completion"),
            changed_paths=p["changed_paths"],
            requirements=p.get("requirements", {}),
            validation=p.get("validation", {}),
            unresolved=p.get("unresolved", []),
            prior_findings=p.get("findings", []),
            owner_decisions=p.get("owner_decisions", []),
            context=context,
        )
    )


async def review_all(provider, candidate, policy, root, attempt, on_identity):
    """All partitions and a final whole-scope review are mandatory for large diffs."""
    from .providers import validate_result, ProviderFailure

    pages, related = await partitions(candidate, root)

    async def invoke(part, child):
        reads = []
        for round_number in range(5):
            part["payload"]["read_protocol"] = True
            part["payload"]["review_context"]["requested_code"] = reads
            result = await provider.start_review(part, policy, root, child, on_identity)
            if not result.get("requested_reads"):
                return result
            if round_number == 4 or len(reads) + len(result["requested_reads"]) > 16:
                raise ValueError("bounded snapshot read budget exhausted; no verdict accepted")
            for request in result["requested_reads"]:
                path = safe_path(request["path"])
                _, tracked, _ = await git(root, "ls-files", "--error-unmatch", "--", path)
                if tracked.strip() != path:
                    raise ValueError("review requested unregistered source")
                reads.append(read(root, path, request["start"], request["end"]))
            if len(canonical(reads).encode()) > 512 * 1024:
                raise ValueError("snapshot read byte budget exceeded")
            child = dict(
                child,
                id=child["id"] + f"-read-{round_number}",
                event_log=str(Path(child["event_log"]).with_name(f"read-{round_number}.jsonl")),
            )
        raise AssertionError("unreachable")

    summaries = []
    for index, page in enumerate(pages):
        paths = [page["path"]] if page["path"] else candidate["payload"]["changed_paths"]
        part = dict(
            candidate,
            payload=dict(
                candidate["payload"],
                changed_paths=paths,
                review_context=dict(
                    phase="partition", index=index + 1, total=len(pages), partition=page, related=related
                ),
            ),
        )
        child = dict(attempt, id=attempt["id"] + f"-part-{index}") if len(pages) > 1 else attempt
        child["event_log"] = str(Path(attempt["event_log"]).with_name(f"part-{index}.jsonl"))
        try:
            result = await invoke(part, child)
        except ProviderFailure as error:
            error.findings += [f for entry in summaries for f in entry["result"]["blocking"]]
            raise
        validate_result(
            {
                k: result[k]
                for k in (
                    "candidate_id",
                    "run_generation",
                    "scope",
                    "verdict",
                    "blocking",
                    "nonblocking",
                    "resolved",
                    "coverage",
                    "unverified",
                )
            },
            part,
        )
        summaries.append(dict(partition=page | {"diff": "[reviewed in partition]"}, result=result))
    if len(pages) == 1:
        return summaries[0]["result"]
    overall = dict(
        candidate,
        payload=dict(candidate["payload"], review_context=dict(phase="overall", partitions=summaries, related=related)),
    )
    final_attempt = dict(
        attempt, id=attempt["id"] + "-overall", event_log=str(Path(attempt["event_log"]).with_name("overall.jsonl"))
    )
    try:
        result = await invoke(overall, final_attempt)
    except ProviderFailure as error:
        error.findings += [f for entry in summaries for f in entry["result"]["blocking"]]
        raise
    # An overall self-report cannot override a missing/negative partition verdict.
    blocking = {f["id"]: f for entry in summaries for f in entry["result"]["blocking"]}
    blocking.update({f["id"]: f for f in result["blocking"]})
    unverified = list(
        dict.fromkeys(result["unverified"] + [item for entry in summaries for item in entry["result"]["unverified"]])
    )
    if blocking:
        result.update(verdict="CHANGES_REQUESTED", blocking=list(blocking.values()))
    elif any(entry["result"]["verdict"] != "APPROVE" for entry in summaries):
        result.update(verdict="INCONCLUSIVE", unverified=unverified + ["not all fixed partitions approved"])
    if any(entry["result"]["verdict"] == "NEEDS_HUMAN" for entry in summaries):
        result["verdict"] = "NEEDS_HUMAN"
    result["partition_evidence"] = [
        dict(
            index=i + 1,
            path=entry["partition"]["path"],
            segment=entry["partition"]["segment"],
            digest=entry["partition"]["digest"],
            verdict=entry["result"]["verdict"],
            session=entry["result"].get("session"),
        )
        for i, entry in enumerate(summaries)
    ]
    return result
