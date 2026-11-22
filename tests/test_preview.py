"""Preview lifecycle uses temporary source, state, listeners and processes only."""

import hashlib
import http.client
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from app import preview
from app.preview_runtime import isolated_command
from app.procinfo import process_identity

FAKE_AGENT = """#!/usr/bin/python3
import http.server, json, pathlib, re, sys
args = sys.argv[1:]
def value(name): return args[args.index(name) + 1]
if args[:2] == ['operator', 'init']:
    pathlib.Path(value('--key-file')).write_text('isolated key')
    print('preview-operator')
elif args[:2] == ['node', 'identity']:
    pathlib.Path(value('--data-directory')).mkdir(parents=True, exist_ok=True)
    print(json.dumps(dict(node_id='isolated-preview-node', node_public_key='preview-key')))
elif args[:2] == ['operator', 'attest-node']:
    pathlib.Path(value('--output')).write_text('isolated attestation')
elif args[:2] == ['agent', 'start']:
    config = pathlib.Path(value('--config')).read_text()
    assert 'request_auth = "require"' in config
    assert 'join_addresses = []' in config
    assert '/nonexistent/preview-containerd.sock' in config
    port = int(re.search('listen_address = "127.0.0.1:([0-9]+)"', config)[1])
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/v1/health': body = b'ok'
            elif self.path == '/v1/node-identity': body = b'{"node_id":"isolated-preview-node"}'
            else: body = b'[]'
            self.send_response(200); self.end_headers(); self.wfile.write(body)
        def do_POST(self):
            self.send_response(401); self.end_headers()
    http.server.HTTPServer(('127.0.0.1', port), Handler).serve_forever()
else: sys.exit(2)
"""


def request(state, path="/", method="GET", body=None, headers=None):
    parsed = urlsplit(state["url"])
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=4)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def login(state, root):
    password = (root / state["id"] / "password").read_text().strip()
    code, headers, _ = request(
        state,
        "/_preview/login",
        "POST",
        "password=" + password,
        {"Origin": state["url"], "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert code == 303
    assert (
        "HttpOnly" in headers["Set-Cookie"]
        and "SameSite=Strict" in headers["Set-Cookie"]
    )
    return {"Cookie": headers["Set-Cookie"].split(";")[0], "Origin": state["url"]}


@pytest.fixture
def preview_environment(tmp_path, git_repo, monkeypatch):
    console = git_repo / "apps" / "console"
    console.mkdir(parents=True)
    (console / "text.txt").write_text("original UI")
    (console / "package-lock.json").write_text("{}")
    (git_repo / "Cargo.lock").write_text("# fixture")
    subprocess.run(["git", "-C", str(git_repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(git_repo), "commit", "-qm", "console fixture"], check=True
    )
    root = preview.root_path(tmp_path / "previews", create=True)

    def fake_build(entry, mode, verify):
        source = entry / "source"
        if (source / "fail-build").exists():
            raise preview.PreviewError("intentional failed build")
        dist = entry / "artifacts" / "console"
        dist.mkdir()
        (dist / "index.html").write_text(
            (source / "apps" / "console" / "text.txt").read_text()
        )
        binary = entry / "artifacts" / "corriedale"
        binary.write_text(FAKE_AGENT)
        binary.chmod(0o755)
        return dict(result="PASS", tests="NOT_RUN", production_release_gate="NOT_RUN")

    monkeypatch.setattr(preview, "build", fake_build)
    yield root, git_repo
    for child in root.iterdir():
        if child.is_dir():
            preview.cleanup(child)


def test_ui_dirty_snapshot_auth_smoke_and_cli_restart(preview_environment):
    root, source = preview_environment
    (source / "apps/console/text.txt").write_text("changed UI from dirty worktree")
    state = preview.start(root, source, "ui")
    assert state["source"]["dirty"] and state["source"]["commit"] == preview.git(
        source, "rev-parse", "HEAD"
    )
    assert state["build"]["result"] == "PASS" and state["backend"] == "MOCK"
    assert state["visual_review"] == state["remote_phone"] == "NOT_RUN"
    assert request(state)[0] == 303
    assert request(state, "/v1/jobs", "POST", "{}")[0] == 403  # Missing Origin
    assert request(state, "/v1/jobs", "POST", "{}", {"Origin": state["url"]})[0] == 401
    headers = login(state, root)
    assert request(state, headers=headers)[2] == b"changed UI from dirty worktree"
    assert request(state, "/v1/compat/r1/v1/members", headers=headers)[2] == b"[]"
    assert request(state, "/v1/jobs", "POST", "{}", headers)[0] == 405
    assert request(state, "/%2e%2e/password", headers=headers)[0] == 403
    assert request(state, "/", headers=dict(headers, Host="evil.example"))[0] == 403
    assert (
        request(
            state,
            "/v1/jobs",
            "POST",
            "{}",
            dict(headers, Origin="https://evil.example"),
        )[0]
        == 403
    )
    # Separate CLI invocation recovers persisted process identity without starting anything.
    output = subprocess.check_output(
        [
            sys.executable,
            "-m",
            "app.preview",
            "--root",
            str(root),
            "status",
            state["id"],
        ],
        cwd=preview.CODE,
        text=True,
    )
    recovered = json.loads(output)
    assert (
        recovered["supervisor"] == state["supervisor"] and recovered["health"] == "PASS"
    )
    entry = root / state["id"]
    assert not list((entry / "source").rglob(".git"))
    assert (entry / "password").stat().st_mode & 0o077 == 0


def test_multiple_real_git_worktrees_concurrent_start_and_cleanup(preview_environment):
    from concurrent.futures import ThreadPoolExecutor

    root, source = preview_environment
    other = source.parent / "other-worktree"
    subprocess.run(
        ["git", "-C", str(source), "worktree", "add", "-qb", "other", str(other)],
        check=True,
    )
    (other / "apps/console/text.txt").write_text("other worktree UI")
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            one, two = list(
                pool.map(lambda path: preview.start(root, path, "ui"), (source, other))
            )
        assert one["id"] != two["id"] and one["url"] != two["url"]
        assert one["source"]["snapshot_sha256"] != two["source"]["snapshot_sha256"]
        assert request(one, headers=login(one, root))[2] == b"original UI"
        assert request(two, headers=login(two, root))[2] == b"other worktree UI"
        preview.cleanup(root / one["id"])
        assert not (root / one["id"]).exists()
        assert preview.status(root / two["id"])["health"] == "PASS"
        assert (source / "apps/console/text.txt").read_text() == "original UI"
    finally:
        subprocess.run(
            ["git", "-C", str(source), "worktree", "remove", "--force", str(other)],
            check=True,
        )


def test_failed_build_never_reuses_existing_preview(preview_environment):
    root, source = preview_environment
    old = preview.start(root, source, "ui")
    (source / "fail-build").write_text("bad build")
    with pytest.raises(preview.PreviewError, match="intentional failed build"):
        preview.start(root, source, "ui")
    entries = [p for p in root.iterdir() if p.is_dir() and p.name != old["id"]]
    assert len(entries) == 1
    failed = preview.status(entries[0])
    assert failed["build"]["result"] == "FAIL" and failed["url"] is None
    assert failed["frontend"] == "STOPPED"
    assert preview.status(root / old["id"])["health"] == "PASS"


def test_crash_does_not_touch_other_preview_or_unrelated_process(preview_environment):
    root, source = preview_environment
    one, two = preview.start(root, source, "ui"), preview.start(root, source, "ui")
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        os.kill(one["supervisor"]["pid"], signal.SIGKILL)
        deadline = time.monotonic() + 5
        while (
            preview.status(root / one["id"])["frontend"] != "STOPPED"
            and time.monotonic() < deadline
        ):
            time.sleep(0.05)
        crashed = preview.status(root / one["id"])
        assert (
            crashed["frontend"] == "STOPPED" and "unexpectedly" in crashed["last_error"]
        )
        preview.cleanup(root / one["id"])
        assert (
            unrelated.poll() is None
            and preview.status(root / two["id"])["health"] == "PASS"
        )
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=5)


@pytest.mark.skipif(
    not shutil.which("bwrap"),
    reason="bubblewrap required for private network namespace",
)
def test_integrated_private_namespace_real_api_and_cleanup(preview_environment):
    root, source = preview_environment
    state = preview.start(root, source, "integrated")
    entry = root / state["id"]
    assert (
        state["health"] == "PASS"
        and state["api_smoke"] == "PASS"
        and state["backend"] == "RUNNING"
    )
    headers = login(state, root)
    assert (
        json.loads(request(state, "/v1/node-identity", headers=headers)[2])["node_id"]
        == "isolated-preview-node"
    )
    assert request(state, "/v1/jobs", "POST", "{}", headers)[0] == 401
    # A host listener on loopback is unreachable from the same isolation command used by the Agent.
    with socket.socket() as sentinel:
        sentinel.bind(("127.0.0.1", 0))
        sentinel.listen()
        argv = isolated_command(entry)
        argv[argv.index("--") + 1 :] = [
            "/usr/bin/python3",
            "-c",
            f"import socket; s=socket.socket(); s.settimeout(1); "
            f"assert s.connect_ex(('127.0.0.1',{sentinel.getsockname()[1]})) != 0",
        ]
        subprocess.run(argv, check=True)
    config = (entry / "runtime/agent.toml").read_text()
    assert 'request_auth = "require"' in config
    assert "join_addresses = []" in config and "containerd.sock" in config
    preview.stop(entry)
    assert preview.status(entry)["frontend"] == "STOPPED"
    assert not (entry / "runtime/api.sock").exists()
    preview.cleanup(entry)
    assert not entry.exists()


def test_identity_mismatch_never_signals_unrelated_process(preview_environment):
    root, source = preview_environment
    state = preview.start(root, source, "ui")
    entry = root / state["id"]
    record = preview.read_manifest(entry)
    original = record["supervisor"]
    record["supervisor"] = {"pid": os.getpid(), "identity": "wrong identity"}
    preview.write_json(entry / "manifest.json", record)
    try:
        with pytest.raises(preview.PreviewError, match="mismatched ownership"):
            preview.stop(entry)
        assert process_identity(original["pid"]) == original["identity"]
    finally:
        record["supervisor"] = original
        preview.write_json(entry / "manifest.json", record)


def test_open_no_browser_is_success_without_visual_review(
    preview_environment, monkeypatch
):
    root, source = preview_environment
    state = preview.start(root, source, "ui")
    monkeypatch.setattr(preview.webbrowser, "open", lambda *_args, **_kwargs: False)
    result = preview.open_preview(root / state["id"])
    assert not result["browser_launch"] and result["visual_review"] == "NOT_RUN"


def test_remote_host_cookie_origin_and_route_cleanup(preview_environment):
    root, source = preview_environment
    state = preview.start(root, source, "ui", remote_host="preview.example.ts.net:8443")
    password = (root / state["id"] / "password").read_text().strip()
    headers = {
        "Host": state["remote_host"],
        "Origin": "https://" + state["remote_host"],
    }
    code, response, _ = request(
        state, "/_preview/login", "POST", "password=" + password, headers
    )
    assert code == 303 and "; Secure" in response["Set-Cookie"]
    entry = root / state["id"]
    (entry / "remote-route.json").write_text("{}")
    with pytest.raises(preview.PreviewError, match="remote route"):
        preview.cleanup(entry)
    assert entry.exists() and preview.status(entry)["frontend"] == "STOPPED"
    (entry / "remote-route.json").unlink()


def test_snapshot_rejects_symlinks_and_change_during_copy(
    tmp_path, git_repo, monkeypatch
):
    (git_repo / "link").symlink_to("/etc/passwd")
    with pytest.raises(preview.PreviewError, match="symlinks"):
        preview.snapshot(git_repo, tmp_path / "snapshot")
    (git_repo / "link").unlink()
    original = shutil.copy2

    def mutate(src, dst):
        result = original(src, dst)
        (git_repo / "README.md").write_text("changed while copying")
        return result

    monkeypatch.setattr(shutil, "copy2", mutate)
    with pytest.raises(preview.PreviewError, match="changed during snapshot"):
        preview.snapshot(git_repo, tmp_path / "snapshot")


def test_ownership_traversal_and_production_directory_rejected(tmp_path):
    production = tmp_path / "production"
    production.mkdir()
    (production / "database").write_text("keep")
    with pytest.raises(preview.PreviewError, match="nonempty"):
        preview.root_path(production, create=True)
    root = preview.root_path(tmp_path / "previews", create=True)
    for ident in ("../production", "", "/tmp", "a" * 15):
        with pytest.raises(preview.PreviewError, match="invalid"):
            preview.entry_path(root, ident)
    link = root / ("a" * 16)
    link.symlink_to(production, target_is_directory=True)
    with pytest.raises(preview.PreviewError, match="symlink"):
        preview.entry_path(root, link.name)
    assert (production / "database").read_text() == "keep"


def test_build_environment_never_inherits_cluster_targets(tmp_path, monkeypatch):
    for key in (
        "VITE_API_BASE_URL",
        "CORRIEDALE_API_PROXY_TARGET",
        "CODEX_GUI_HOME",
        "CORRIEDALE_CONFIG",
    ):
        monkeypatch.setenv(key, "production")
    env = preview.build_env(tmp_path)
    assert (
        env["VITE_API_BASE_URL"] == ""
        and env["CORRIEDALE_API_PROXY_TARGET"] == "http://127.0.0.1:1"
    )
    assert "CODEX_GUI_HOME" not in env and "CORRIEDALE_CONFIG" not in env
    assert env["CARGO_TARGET_DIR"].startswith(str(tmp_path))


def test_build_records_hashes_and_verify_checks(tmp_path, monkeypatch):
    entry = tmp_path / "entry"
    console = entry / "source/apps/console"
    console.mkdir(parents=True)
    (console / "package-lock.json").write_text("{}")
    (entry / "source/Cargo.lock").write_text("# fixture")
    (entry / "artifacts").mkdir()
    calls = []

    def fake_command(argv, cwd, preview_entry, label):
        calls.append(argv)
        if argv == ["npm", "run", "build"]:
            (console / "dist").mkdir()
            (console / "dist/index.html").write_text("built UI")

    monkeypatch.setattr(preview, "run_checked", fake_command)
    result = preview.build(entry, "ui", True)
    assert result["tests"] == "PASS" and result["production_release_gate"] == "NOT_RUN"
    assert [
        "npm",
        "run",
        "lint",
        "--",
        "--no-ignore",
        "src",
        "tests",
        "vite.config.ts",
    ] in calls
    assert ["npm", "run", "test"] in calls
    hashes = json.loads((entry / "artifact-hashes.json").read_text())
    assert hashes["console/index.html"] == hashlib.sha256(b"built UI").hexdigest()


@pytest.mark.skipif(not shutil.which("bwrap"), reason="bubblewrap required")
def test_build_cannot_write_outside_preview_and_timeout_leaves_no_children(tmp_path):
    entry = tmp_path / "build-entry"
    entry.mkdir()
    (entry / "logs").mkdir()
    preview.root_path(preview.CODE / ".preview", create=True)
    directory = tempfile.TemporaryDirectory(
        prefix="build-isolation-test-", dir=preview.CODE / ".preview"
    )
    unrelated = Path(directory.name) / "user-data"
    unrelated.write_text("preserve")
    program = (
        "from pathlib import Path; "
        f"p=Path({str(unrelated)!r}); "
        "\ntry: p.write_text('modified')\n"
        "except OSError: pass\n"
        "else: raise AssertionError('host path was writable')\n"
        f"Path({str(entry / 'build-output')!r}).write_text('isolated output')"
    )
    preview.run_checked([sys.executable, "-c", program], entry, entry, "isolated build")
    assert unrelated.read_text() == "preserve"
    assert (entry / "build-output").read_text() == "isolated output"
    directory.cleanup()
    with pytest.raises(subprocess.TimeoutExpired):
        preview.run_checked(
            [
                sys.executable,
                "-c",
                "import subprocess,time; subprocess.Popen(['sleep','60']); time.sleep(60)",
            ],
            entry,
            entry,
            "timeout build",
            timeout=0.1,
        )


def test_smoke_records_automated_checks_without_visual_approval(preview_environment):
    root, source = preview_environment
    state = preview.start(root, source, "ui")
    entry = root / state["id"]
    (entry / "artifacts/console/index.html").write_text(
        '<script src="bundle.js"></script>'
    )
    result = preview.smoke(entry)
    assert (
        result["console_document"] == result["unauthenticated_write_denied"] == "PASS"
    )
    assert (
        result["console_api_access"] == "PASS"
        and result["backend_node_identity"] == "NOT_RUN"
    )
    assert result["visual_review"] == result["production_release_gate"] == "NOT_RUN"
    assert preview.status(entry)["automated_smoke"] == result


def test_control_socket_recovers_across_pid_namespaces(
    preview_environment, monkeypatch
):
    root, source = preview_environment
    state = preview.start(root, source, "ui")
    entry = root / state["id"]
    monkeypatch.setattr(
        preview, "pid_namespace", lambda: "pid:[another-command-namespace]"
    )
    assert preview.status(entry)["health"] == "PASS"
    assert preview.control(entry, "status")["authorized"]
    preview.stop(entry)
    assert preview.status(entry)["frontend"] == "STOPPED"
    assert not (entry / "runtime/ctl").exists()


@pytest.mark.skipif(
    os.environ.get("CODEX_GUI_BROWSER") != "1",
    reason="opt-in real Chromium login check",
)
def test_preview_browser_form_login_has_same_origin(preview_environment):
    playwright = pytest.importorskip("playwright.sync_api")
    root, source = preview_environment
    state = preview.start(root, source, "ui")
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(state["url"])
        assert page.title() == "Console Preview sign in"
        page.locator('input[name="password"]').fill(
            (root / state["id"] / "password").read_text().strip()
        )
        page.get_by_role("button", name="Sign in").click()
        page.wait_for_url(state["url"] + "/")
        assert "original UI" in page.locator("body").inner_text()
        browser.close()


@pytest.mark.skipif(not shutil.which("bwrap"), reason="bubblewrap required")
def test_integrated_supervisor_crash_reaps_private_backend(preview_environment):
    root, source = preview_environment
    state = preview.start(root, source, "integrated")
    pid = state["supervisor"]["pid"]
    children = Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
    assert children
    identities = {int(child): process_identity(int(child)) for child in children}
    os.kill(pid, signal.SIGKILL)
    deadline = time.monotonic() + 5
    while (
        any(
            process_identity(child) == identity
            for child, identity in identities.items()
        )
        and time.monotonic() < deadline
    ):
        time.sleep(0.05)
    assert all(
        process_identity(child) != identity for child, identity in identities.items()
    )
    entry = root / state["id"]
    assert preview.status(entry)["frontend"] == "STOPPED"
    preview.cleanup(entry)
    assert not entry.exists()


def test_status_and_stop_succeed_while_recording_blocked_smoke(preview_environment):
    root, source = preview_environment
    state = preview.start(root, source, "ui")
    entry = root / state["id"]
    preview.write_json(entry / "smoke.json", {"console_api_access": "BLOCKED"})
    assert preview.main(["--root", str(root), "status", state["id"]]) == 0
    assert preview.main(["--root", str(root), "stop", state["id"]]) == 0
    assert preview.main(["--root", str(root), "cleanup", state["id"]]) == 0
