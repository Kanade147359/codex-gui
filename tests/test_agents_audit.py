"""AGENTS.md audit: read-only, cumulative budget, heuristics, and agreement with what the real Codex puts in the prompt."""
import asyncio
import hashlib
import os
import shutil
import subprocess

import pytest

from app import agents_audit as aa
from app import tool_probe as tp

HAS_CODEX = shutil.which("codex") is not None


def snapshot(root):
    """path -> (sha256, mtime_ns, mode) of every file and directory under root: any write changes it."""
    snap = {}
    for dirpath, dirs, files in os.walk(root):
        for name in dirs + files:
            p = os.path.join(dirpath, name)
            st = os.lstat(p)
            digest = hashlib.sha256(open(p, "rb").read()).hexdigest() if os.path.isfile(p) and not os.path.islink(p) else ""
            snap[p] = (digest, st.st_mtime_ns, st.st_mode)
    return snap


def doc(tag, size):
    return f"[{tag}]" + "x" * (size - len(tag) - 3) + "\n"


@pytest.fixture
def tree(tmp_path):
    repo = tmp_path / "repo"
    (repo / "a" / "b").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    home = tmp_path / "codexhome"
    home.mkdir()
    (home / "AGENTS.md").write_text(doc("GLOBAL", 100))
    (repo / "AGENTS.md").write_text(doc("ROOT", 200))
    (repo / "a" / "AGENTS.md").write_text(doc("A", 300))
    (repo / "a" / "b" / "AGENTS.md").write_text(doc("AB", 400))
    return repo, home


def run(repo, home, sub="a/b", **kw):
    return aa.audit(repo / sub, home=home, **kw)


def test_audit_never_writes_anything(tree):
    repo, home = tree
    before = snapshot(repo.parent)
    for budget in (None, 50, 450, 10**6):
        for sub in (".", "a", "a/b"):
            run(repo, home, sub, max_bytes=budget)
    assert snapshot(repo.parent) == before  # no file or directory was created, changed, touched or removed


def test_audit_opens_files_read_only(tree, monkeypatch):
    repo, home = tree
    modes = []
    real_open = open

    def spy(file, mode="r", *a, **k):
        modes.append(mode)
        return real_open(file, mode, *a, **k)

    monkeypatch.setattr("builtins.open", spy)
    run(repo, home)
    assert modes and all(m in ("rb", "r") for m in modes)


def test_chain_is_global_then_root_to_cwd(tree):
    repo, home = tree
    r = run(repo, home)
    assert [(f["scope"], f.get("relative")) for f in r["files"]] == [
        ("global", None), ("project", "AGENTS.md"), ("project", "a/AGENTS.md"), ("project", "a/b/AGENTS.md")]
    assert r["project_root"] == str(repo)
    assert [f["bytes"] for f in r["files"]] == [100, 200, 300, 400]


def test_cumulative_bytes_exclude_global(tree):
    repo, home = tree
    r = run(repo, home)
    assert [f["cumulative_bytes"] for f in r["files"]] == [None, 200, 500, 900]
    assert r["budget"]["project_bytes"] == 900 and r["budget"]["max_bytes"] == 32768 and r["budget"]["source"] == "default"


@pytest.mark.parametrize("budget,level", [(10_000, "ok"), (1200, "warning"), (900, "critical"), (500, "critical")])
def test_levels(tree, budget, level):
    repo, home = tree
    r = run(repo, home, max_bytes=budget)
    assert r["budget"]["level"] == level and r["budget"]["source"] == "codex config"


def test_level_boundaries_are_75_and_100_percent(tree):
    repo, home = tree  # project chain = 900 bytes
    assert run(repo, home, max_bytes=1200)["budget"]["level"] == "warning"   # exactly 75%
    assert run(repo, home, max_bytes=1201)["budget"]["level"] == "ok"        # just under 75%
    assert run(repo, home, max_bytes=900)["budget"]["level"] == "critical"   # exactly 100%
    assert run(repo, home, max_bytes=901)["budget"]["level"] == "warning"


def test_truncation_and_dropped_files(tree):
    repo, home = tree
    r = run(repo, home, max_bytes=450)
    st = {f.get("relative"): (f["status"], f["included_bytes"]) for f in r["files"] if f["scope"] == "project"}
    assert st == {"AGENTS.md": ("included", 200), "a/AGENTS.md": ("truncated", 250), "a/b/AGENTS.md": ("dropped", 0)}
    assert "truncated" in r["truncation"] or "cut at 250" in r["truncation"]
    assert "left out entirely" in r["truncation"]
    assert run(repo, home)["truncation"] is None


def test_override_file_wins_and_empty_files_do_not_count(tree):
    repo, home = tree
    (repo / "AGENTS.override.md").write_text(doc("OVR", 50))
    (repo / "a" / "AGENTS.md").write_text("   \n")
    r = run(repo, home)
    assert [f.get("relative") for f in r["files"] if f["scope"] == "project"] == ["AGENTS.override.md", "a/b/AGENTS.md"]


def test_no_project_root_marker_means_cwd_only(tmp_path):
    d = tmp_path / "plain" / "deep"
    d.mkdir(parents=True)
    (tmp_path / "plain" / "AGENTS.md").write_text("parent\n")
    (d / "AGENTS.md").write_text("child\n")
    for markers in ([], ["NO_SUCH_MARKER"]):  # (this machine may have a .git in /tmp, so the markers are explicit)
        r = aa.audit(d, home=tmp_path / "nohome", markers=markers)
        assert [f["name"] for f in r["files"]] == ["AGENTS.md"] and r["files"][0]["bytes"] == 6 and r["project_root"] == str(d)


def test_heuristics_flag_context_hungry_instructions(tree):
    repo, home = tree
    (repo / "AGENTS.md").write_text(
        "# Rules\nAlways read docs/architecture.md first.\nBefore every task, read all files in docs/.\n"
        "毎回 ドキュメントを読むこと\n" + "".join(f"See docs/guide{i}.md\n" for i in range(9)))
    r = run(repo, home, ".")
    ids = {w["id"] for w in r["warnings"]}
    assert {"always_read", "every_task", "read_all", "jp_always", "many_refs"} <= ids
    assert all(w["path"].endswith("AGENTS.md") for w in r["warnings"])


def test_clean_instructions_have_no_warnings(tree):
    repo, home = tree
    (repo / "AGENTS.md").write_text("# Style\nUse 4 spaces.\nRun pytest before committing.\n")
    assert run(repo, home, ".")["warnings"] == []


def test_duplicates_between_root_nested_and_global(tree):
    repo, home = tree
    shared = "".join(f"Shared rule number {i}: keep functions small and tested\n" for i in range(4))
    (repo / "AGENTS.md").write_text("# Root\n" + shared)
    (repo / "a" / "AGENTS.md").write_text("# Nested\n" + shared + "only here\n")
    (home / "AGENTS.md").write_text("# Global\n" + shared)
    kinds = {(d["kind"], d["shared_lines"]) for d in run(repo, home, "a")["duplicates"]}
    assert ("root_nested", 4) in kinds and ("global_project", 4) in kinds


def test_missing_everything_is_a_clean_empty_result(tmp_path):
    r = aa.audit(tmp_path, home=tmp_path / "nohome")
    assert r["files"] == [] and r["budget"]["level"] == "ok" and r["truncation"] is None and r["read_only"]


# ----- agreement with the real Codex -----

pytestmark_real = pytest.mark.skipif(not HAS_CODEX, reason="codex is not installed")


def included_x(block, tag):
    """number of 'x' payload characters of one document inside the project-doc text"""
    i = block.find(f"[{tag}]")
    if i < 0:
        return 0
    j = i + len(tag) + 2
    n = 0
    while j < len(block) and block[j] == "x":
        n += 1
        j += 1
    return n


@pytestmark_real
@pytest.mark.parametrize("budget", [32768, 900, 500, 450, 150])
def test_prediction_matches_what_codex_puts_in_the_prompt(tree, budget):
    repo, home = tree
    messages = asyncio.run(tp.prompt_input("codex", repo / "a" / "b", {"project_doc_max_bytes": budget}, codex_home=home))
    block = tp.agents_block(messages)
    assert block is not None
    predicted = run(repo, home, max_bytes=budget)
    by_tag = {"AGENTS.md": "ROOT", "a/AGENTS.md": "A", "a/b/AGENTS.md": "AB"}
    for f in predicted["files"]:
        if f["scope"] != "project":
            continue
        tag = by_tag[f["relative"]]
        overhead = len(tag) + 2 + (1 if f["status"] == "included" else 0)  # "[TAG]", and the newline unless the file was cut
        assert included_x(block, tag) == max(f["included_bytes"] - overhead, 0), (budget, f["relative"], f["status"])
    assert "[GLOBAL]" in block  # the global file is always there, outside the budget


@pytestmark_real
def test_no_marker_means_cwd_only_in_codex_too(tmp_path):
    d = tmp_path / "plain" / "deep"
    d.mkdir(parents=True)
    (tmp_path / "plain" / "AGENTS.md").write_text("PARENT_MARK\n")
    (d / "AGENTS.md").write_text("CHILD_MARK\n")
    home = tmp_path / "h"
    home.mkdir()
    block = tp.agents_block(asyncio.run(tp.prompt_input("codex", d, {"project_root_markers": []}, codex_home=home)))
    assert "CHILD_MARK" in block and "PARENT_MARK" not in block
