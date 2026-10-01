"""AGENTS.md editor: the module, the API, and the separation of repository vs task worktree."""
import asyncio
import json
import os
import subprocess

import pytest
from fastapi.testclient import TestClient

from app import agents_md
from app.main import create_app

from conftest import FakeRunner, wait_for


def go(coro):
    return asyncio.run(coro)


def commit_all(repo, msg="c"):
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", msg], check=True)


# ---------- paths ----------

@pytest.mark.parametrize("rel", ["../AGENTS.md", "/etc/AGENTS.md", "sub/../../AGENTS.md", "README.md", "a/agents.md",
                                 "AGENTS.md/x", "a\\AGENTS.md", "AGENTS.md\x00"])
def test_resolve_rejects_anything_but_an_agents_md_inside_the_root(git_repo, rel):
    with pytest.raises(agents_md.AgentsError):
        agents_md.resolve(git_repo, rel)


def test_resolve_refuses_a_symlink_that_leaves_the_repository(git_repo, tmp_path):
    outside = tmp_path / "secret.md"
    outside.write_text("x")
    (git_repo / "AGENTS.md").symlink_to(outside)
    with pytest.raises(agents_md.AgentsError):
        agents_md.resolve(git_repo, "AGENTS.md")
    (git_repo / "AGENTS.md").unlink()
    (git_repo / "sub").mkdir()
    assert agents_md.resolve(git_repo, "sub/AGENTS.md") == (git_repo / "sub" / "AGENTS.md").resolve()
    with pytest.raises(agents_md.AgentsError) as ei:
        agents_md.resolve(git_repo, "nope/AGENTS.md")
    assert ei.value.status == 404


# ---------- read / write ----------

def test_missing_file_reads_as_not_existing(git_repo):
    r = go(agents_md.read(git_repo))
    assert r["exists"] is False and r["content"] == "" and r["git"]["state"] == "missing"


def test_create_then_edit_then_unchanged_does_not_write(git_repo):
    async def scenario():
        r = await agents_md.write(git_repo, "AGENTS.md", "# Rules\n", expected_sha="")  # "" = expected absent
        assert r == {"changed": True, "created": True, "sha256": r["sha256"]}
        assert (git_repo / "AGENTS.md").read_text() == "# Rules\n"
        loaded = await agents_md.read(git_repo)
        assert loaded["exists"] and loaded["content"] == "# Rules\n" and loaded["git"]["state"] == "untracked"
        mtime = os.stat(git_repo / "AGENTS.md").st_mtime_ns
        same = await agents_md.write(git_repo, "AGENTS.md", "# Rules\n", loaded["sha256"])
        assert same["changed"] is False and os.stat(git_repo / "AGENTS.md").st_mtime_ns == mtime  # no write at all
        edited = await agents_md.write(git_repo, "AGENTS.md", "# Rules\nmore\n", loaded["sha256"])
        assert edited["changed"] and not edited["created"]

    go(scenario())


def test_save_is_refused_when_the_file_changed_on_disk(git_repo):
    async def scenario():
        await agents_md.write(git_repo, "AGENTS.md", "one\n")
        loaded = await agents_md.read(git_repo)
        (git_repo / "AGENTS.md").write_text("changed elsewhere\n")
        with pytest.raises(agents_md.AgentsError) as ei:
            await agents_md.write(git_repo, "AGENTS.md", "mine\n", loaded["sha256"])
        assert ei.value.status == 409 and ei.value.code == "changed"
        assert (git_repo / "AGENTS.md").read_text() == "changed elsewhere\n"  # nothing was overwritten
        with pytest.raises(agents_md.AgentsError):  # "I expected it to be absent" but it exists
            await agents_md.write(git_repo, "AGENTS.md", "mine\n", "")

    go(scenario())


def test_crlf_files_stay_crlf_and_non_utf8_is_refused(git_repo):
    async def scenario():
        (git_repo / "AGENTS.md").write_bytes(b"a\r\nb\r\n")
        r = await agents_md.read(git_repo)
        assert r["content"] == "a\nb\n" and r["crlf"] is True
        await agents_md.write(git_repo, "AGENTS.md", "a\nb\nc\n", r["sha256"])
        assert (git_repo / "AGENTS.md").read_bytes() == b"a\r\nb\r\nc\r\n"
        (git_repo / "AGENTS.md").write_bytes(b"\xff\xfe bad")
        with pytest.raises(agents_md.AgentsError) as ei:
            await agents_md.read(git_repo)
        assert ei.value.status == 415

    go(scenario())


def test_size_limit(git_repo):
    with pytest.raises(agents_md.AgentsError) as ei:
        go(agents_md.write(git_repo, "AGENTS.md", "x" * (agents_md.MAX_BYTES + 1)))
    assert ei.value.status == 413


# ---------- git state and diff ----------

def test_git_state_and_diff(git_repo):
    async def scenario():
        assert (await agents_md.diff(git_repo))["state"] == "missing"
        (git_repo / "AGENTS.md").write_text("# a\n")
        d = await agents_md.diff(git_repo)
        assert d["state"] == "untracked" and "+# a" in d["diff"]          # a new file: shown as all-added
        commit_all(git_repo)
        assert (await agents_md.file_state(git_repo))["state"] == "clean"
        (git_repo / "AGENTS.md").write_text("# a\n# b\n")
        st = await agents_md.file_state(git_repo)
        assert st["state"] == "modified" and st["short"].endswith("AGENTS.md") and st["short"].lstrip().startswith("M")
        d = await agents_md.diff(git_repo)
        assert "+# b" in d["diff"] and d["state"] == "modified"
        subprocess.run(["git", "-C", str(git_repo), "add", "AGENTS.md"], check=True)  # staged changes still show
        assert "+# b" in (await agents_md.diff(git_repo))["diff"]

    go(scenario())


def test_diff_without_any_commit(tmp_path):
    repo = tmp_path / "empty"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "AGENTS.md").write_text("x\n")
    subprocess.run(["git", "-C", str(repo), "add", "AGENTS.md"], check=True)
    d = go(agents_md.diff(repo))
    assert d["state"] == "added" and "+x" in d["diff"]


# ---------- nested files and repo info ----------

def test_nested_agents_files_are_found_root_first_and_ignored_ones_skipped(git_repo):
    (git_repo / "AGENTS.md").write_text("root\n")
    (git_repo / "pkg" / "deep").mkdir(parents=True)
    (git_repo / "pkg" / "AGENTS.md").write_text("pkg\n")
    (git_repo / "pkg" / "deep" / "AGENTS.md").write_text("deep\n")
    (git_repo / "node_modules").mkdir()
    (git_repo / "node_modules" / "AGENTS.md").write_text("vendored\n")
    (git_repo / ".gitignore").write_text("node_modules/\n")
    assert go(agents_md.find_all(git_repo)) == ["AGENTS.md", "pkg/AGENTS.md", "pkg/deep/AGENTS.md"]
    info = go(agents_md.repo_info(git_repo))
    assert info["agents_md"] == {"found": True, "nested": ["pkg/AGENTS.md", "pkg/deep/AGENTS.md"]}
    assert info["git"]["clean"] is False  # the new files are untracked


def test_repo_info_found_or_not_found_and_clean(git_repo):
    info = go(agents_md.repo_info(git_repo))
    assert info["agents_md"] == {"found": False, "nested": []} and info["git"] == {"clean": True, "changed": 0, "label": "clean"}
    assert info["repository"] == str(git_repo)


# ---------- the API, and repository vs task worktree ----------

@pytest.fixture
def client(settings):
    with TestClient(create_app(settings, FakeRunner())) as c:
        yield c


def test_repo_api_create_edit_save_diff(client, git_repo):
    repo = str(git_repo)
    info = client.get("/api/repo-info", params={"repository": repo}).json()
    assert info["agents_md"]["found"] is False
    r = client.get("/api/agents-md", params={"repository": repo}).json()
    assert r["exists"] is False and r["root"] == repo and r["files"] == []
    ok = client.put("/api/agents-md", json={"repository": repo, "content": "# Project\n", "expected_sha": ""})
    assert ok.status_code == 200 and ok.json()["created"] is True
    assert (git_repo / "AGENTS.md").read_text() == "# Project\n"
    r = client.get("/api/agents-md", params={"repository": repo}).json()
    assert r["exists"] and r["content"] == "# Project\n" and r["git"]["state"] == "untracked" and r["files"] == ["AGENTS.md"]
    assert client.get("/api/repo-info", params={"repository": repo}).json()["agents_md"]["found"] is True
    d = client.get("/api/agents-md/diff", params={"repository": repo}).json()
    assert d["state"] == "untracked" and "+# Project" in d["diff"]
    stale = client.put("/api/agents-md", json={"repository": repo, "content": "x", "expected_sha": "0" * 64})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "changed"


def test_api_errors(client, tmp_path, git_repo):
    r = client.get("/api/agents-md", params={"repository": str(tmp_path)})
    assert r.status_code == 400 and "not a git repository" in r.json()["detail"]["message"]
    r = client.get("/api/agents-md", params={"repository": str(git_repo), "path": "../AGENTS.md"})
    assert r.status_code == 400
    assert client.get("/api/repo-info", params={"repository": str(tmp_path)}).status_code == 400


def test_pages_render(client, git_repo):
    r = client.get("/agents", params={"repository": str(git_repo)})
    assert r.status_code == 200 and "Repository AGENTS.md" in r.text


def wait_done(client, task_id):
    return go(wait_for_status(client, task_id))


async def wait_for_status(client, task_id):
    return await wait_for(lambda: client.get(f"/api/tasks/{task_id}").json()["status"] not in ("queued", "starting", "running")
                          and client.get(f"/api/tasks/{task_id}").json(), 15)


def test_repository_and_task_worktree_agents_md_are_separate_files(client, git_repo):
    (git_repo / "AGENTS.md").write_text("# repo rules\n")
    commit_all(git_repo)
    task = client.post("/api/tasks", json={"repository": str(git_repo), "prompt": "ok", "name": "t"}).json()
    wait_done(client, task["id"])
    wt = task["worktree"]
    # the worktree starts with the copy from the base ref
    assert open(os.path.join(wt, "AGENTS.md")).read() == "# repo rules\n"
    page = client.get(f"/tasks/{task['id']}/agents")
    assert page.status_code == 200 and "Task Worktree AGENTS.md" in page.text

    # edit the task's copy: the main repository's file must stay untouched
    t = client.get(f"/api/tasks/{task['id']}/agents-md").json()
    assert t["root"] == wt and t["content"] == "# repo rules\n"
    saved = client.put(f"/api/tasks/{task['id']}/agents-md", json={"content": "# experiment\n", "expected_sha": t["sha256"]})
    assert saved.status_code == 200
    assert (git_repo / "AGENTS.md").read_text() == "# repo rules\n"
    assert open(os.path.join(wt, "AGENTS.md")).read() == "# experiment\n"
    d = client.get(f"/api/tasks/{task['id']}/agents-md/diff").json()
    assert d["state"] == "modified" and "+# experiment" in d["diff"]

    # and the other way round
    client.put("/api/agents-md", json={"repository": str(git_repo), "content": "# repo v2\n"})
    assert open(os.path.join(wt, "AGENTS.md")).read() == "# experiment\n"

    # the repository scope refuses a task worktree path: that is the other file
    r = client.get("/api/agents-md", params={"repository": wt})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "linked_worktree"


def test_task_agents_requires_an_existing_worktree(client, git_repo):
    task = client.post("/api/tasks", json={"repository": str(git_repo), "prompt": "ok"}).json()
    wait_done(client, task["id"])
    assert client.delete(f"/api/tasks/{task['id']}/worktree?force=true").status_code == 200
    r = client.get(f"/api/tasks/{task['id']}/agents-md")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "no_worktree"
    assert client.get("/api/tasks/nope/agents-md").status_code == 404


def test_agents_md_is_never_copied_into_the_prompt(git_repo, make_manager, fake_codex_state):
    """Codex loads AGENTS.md itself; the GUI sends neither its text nor a pointer to it."""
    (git_repo / "AGENTS.md").write_text("SECRET-RULE-TEXT-12345\n")
    commit_all(git_repo)
    m = make_manager(backend="app-server")

    async def scenario():
        t = await m.create_task(repository=str(git_repo), prompt="ok please", name="t")
        await wait_for(lambda: m.get(t["id"])["status"] == "completed")
        await m.shutdown()

    go(scenario())
    rows = [json.loads(line) for line in (fake_codex_state / "invocations.jsonl").read_text().splitlines()]
    sent = json.dumps([r for r in rows if r["method"] in ("thread/start", "turn/start")])
    assert "SECRET-RULE-TEXT-12345" not in sent and "AGENTS" not in sent
    assert [r["params"]["input"][0]["text"] for r in rows if r["method"] == "turn/start"] == ["ok please"]
