import pytest

from app.ssh_agent import child_env, classify_github_ssh, is_github_ssh_url, read_agent_env

AGENT_ENV = """SSH_AUTH_SOCK=/tmp/agent.sock; export SSH_AUTH_SOCK;
SSH_AGENT_PID=4242; export SSH_AGENT_PID;
# echo Agent pid 4242;
"""


def test_read_agent_env(tmp_path):
    f = tmp_path / "agent.env"
    f.write_text(AGENT_ENV)
    assert read_agent_env(f) == {"SSH_AUTH_SOCK": "/tmp/agent.sock", "SSH_AGENT_PID": "4242"}
    assert read_agent_env(tmp_path / "missing") == {}


def test_child_env_prefers_inherited_socket(tmp_path, monkeypatch):
    monkeypatch.setenv("SSH_AUTH_SOCK", "/inherited.sock")
    f = tmp_path / "agent.env"
    f.write_text(AGENT_ENV)
    assert child_env(agent_env_path=f)["SSH_AUTH_SOCK"] == "/inherited.sock"


def test_child_env_falls_back_to_saved_agent_only_if_socket_exists(tmp_path, monkeypatch):
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    sock = tmp_path / "live.sock"
    sock.touch()
    f = tmp_path / "agent.env"
    f.write_text(AGENT_ENV.replace("/tmp/agent.sock", str(sock)))
    assert child_env(agent_env_path=f)["SSH_AUTH_SOCK"] == str(sock)
    sock.unlink()
    assert "SSH_AUTH_SOCK" not in child_env(agent_env_path=f)


def test_child_env_extra_wins(monkeypatch):
    monkeypatch.setenv("GIT_PAGER", "less")
    assert child_env({"GIT_PAGER": "cat"})["GIT_PAGER"] == "cat"


@pytest.mark.parametrize("url,expected", [
    ("git@github.com:user/repo.git", True),
    ("ssh://git@github.com/user/repo.git", True),
    ("https://github.com/user/repo.git", False),
    ("git@gitlab.com:user/repo.git", False),
    ("git@notgithub.com:user/repo.git", False),
])
def test_is_github_ssh_url(url, expected):
    assert is_github_ssh_url(url) is expected


def test_github_exit_code_1_is_success_when_greeted():
    # GitHub exits 1 on a successful `ssh -T`; the greeting is what counts.
    ok, _ = classify_github_ssh(1, "Hi someone! You've successfully authenticated, but GitHub does not provide shell access.")
    assert ok


def test_github_failures_are_not_judged_by_exit_code_alone():
    ok, msg = classify_github_ssh(255, "git@github.com: Permission denied (publickey).")
    assert not ok and "ssh-add -l" in msg
    assert classify_github_ssh(255, "ssh: Could not resolve hostname github.com")[0] is False
    assert classify_github_ssh(None, "timed out after 15s")[0] is False
    # exit 1 without the greeting is not a success either
    assert classify_github_ssh(1, "")[0] is False


def test_spawn_passes_ssh_auth_sock(tmp_path, monkeypatch):
    import asyncio
    import sys
    from app.codex_runner import CodexRunner

    monkeypatch.setenv("SSH_AUTH_SOCK", "/shared/agent.sock")

    class Echo(CodexRunner):
        def build_command(self, task, resume_thread=None):
            return [sys.executable, "-c", "import os; print(os.environ['SSH_AUTH_SOCK'])"]

    async def go():
        proc = await Echo().spawn({"worktree": str(tmp_path)})
        return await proc.communicate()

    out, _ = asyncio.run(go())
    assert out.decode().strip() == "/shared/agent.sock"
