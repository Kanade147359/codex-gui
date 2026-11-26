"""Opt-in acceptance through a separate real GUI process and official CLIs.

Requires the owner's no-extra-usage confirmation, bubblewrap and socat on PATH.
All GUI databases, CLI credentials/state and worktrees belong to this test.
"""
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

import httpx
import pytest

from test_real_claude import isolated_claude_account  # noqa: F401
from test_real_images import labeled_image

pytestmark = pytest.mark.skipif(
    os.environ.get('CODEX_GUI_REAL_CLAUDE') != '1' or os.environ.get('CODEX_GUI_CLAUDE_EXTRA_USAGE_DISABLED') != '1',
    reason='NOT_RUN: requires owner confirmation and explicit real-Claude opt-in',
)
ROOT = Path(__file__).resolve().parents[1]


class RealGUI:
    def __init__(self, folder, runtime):
        self.folder = folder
        self.runtime = runtime
        self.home = folder / 'gui'
        self.socket = socket.socket()
        self.socket.bind(('127.0.0.1', 0))
        self.url = f'http://127.0.0.1:{self.socket.getsockname()[1]}'
        self.client = httpx.Client(base_url=self.url, timeout=30)
        self.proc = None
        self.server_pid = None
        self.owned_groups = set()
        self.output = (folder / 'server.log').open('ab', buffering=0)
        self.entry = folder / 'acceptance_app.py'
        self.entry.write_text(
            'import os\nfrom pathlib import Path\nfrom app.config import Settings\nfrom app.main import create_app\n'
            'app = create_app(Settings(home=Path(os.environ["PR19_GUI_HOME"]), backend="app-server", '
            'codex_bin=os.environ["PR19_CODEX_BIN"], claude_bin=os.environ["PR19_CLAUDE_BIN"], '
            'default_auto_retry=False, stop_grace_seconds=1, git_refresh_seconds=.5, scheduler_interval_seconds=.1))\n'
            '@app.get("/fixture/pid")\nasync def pid(): return {"pid": os.getpid()}\n')

    def start(self):
        # The whole test GUI gets private runtime mounts. Its isolated reviewer
        # sandbox must receive the real ELF, not an outer-namespace shell wrapper.
        search = os.pathsep.join(p for p in os.environ['PATH'].split(os.pathsep)
                                 if Path(p) != self.runtime.parent / 'private-codex-bin')
        binary = shutil.which('codex', path=search) or 'codex'
        env = dict(os.environ, PR19_GUI_HOME=str(self.home), PR19_CODEX_BIN=binary,
                   PR19_CLAUDE_BIN=shutil.which('claude') or 'claude',
                   PYTHONPATH=str(ROOT), CODEX_GUI_SSH_DIAGNOSTICS='0')
        command = ['bwrap', '--bind', '/', '/', '--dev-bind', '/dev', '/dev', '--proc', '/proc',
                   '--bind', str(self.runtime), f'/tmp/codex-daemon-{os.getuid()}',
                   '--bind', str(self.runtime.parent / 'private-codex-mounts'),
                   f'/tmp/codex-bwrap-synthetic-mount-targets-{os.getuid()}',
                   sys.executable, '-m', 'uvicorn', 'acceptance_app:app', '--host', '127.0.0.1',
                   '--fd', str(self.socket.fileno()), '--log-level', 'warning']
        self.proc = subprocess.Popen(
            command,
            cwd=self.folder, env=env,
            pass_fds=(self.socket.fileno(),), stdout=self.output, stderr=self.output, start_new_session=True)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            assert self.proc.poll() is None, 'isolated GUI exited; inspect ' + str(self.folder / 'server.log')
            try:
                if self.client.get('/api/tasks').status_code == 200:
                    self.server_pid = self.client.get('/fixture/pid').json()['pid']
                    return
            except httpx.TransportError:
                pass
            time.sleep(.05)
        raise AssertionError('isolated GUI did not become ready')

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            if self.server_pid is not None:
                os.kill(self.server_pid, signal.SIGTERM)
            else:
                os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(timeout=25)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait(timeout=5)
                raise AssertionError('isolated GUI shutdown exceeded the drain deadline')

    def request(self, method, path, **kwargs):
        response = self.client.request(method, path, **kwargs)
        assert response.is_success, (response.status_code, response.text[:600])
        return response.json()

    def task(self, identity):
        row = self.request('GET', f'/api/tasks/{identity}')
        if row.get('pid'):
            self.owned_groups.add(row['pid'])
        return row

    def wait(self, identity, timeout=360):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            row = self.task(identity)
            if row['status'] not in ('queued', 'starting', 'running', 'waiting_dependencies'):
                return row
            time.sleep(.1)
        raise AssertionError('real task timeout: ' + str((identity, row['status'], row['status_detail'])))

    def create(self, repository, prompt, **fields):
        body = dict(repository=str(repository), prompt=prompt, provider='claude', auto_retry=False,
                    model=os.environ.get('CODEX_GUI_REAL_CLAUDE_MODEL', 'sonnet'),
                    provider_options=dict(trusted_repository=True, permission_mode='acceptEdits', timeout_seconds=300))
        body.update(fields)
        return self.request('POST', '/api/tasks', json=body)

    def events(self, identity):
        return [json.loads(line) for line in (self.home / 'logs' / f'{identity}.jsonl').read_text().splitlines()]

    def close(self):
        try:
            self.stop()
        finally:
            for pid in self.owned_groups:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            self.client.close()
            self.socket.close()
            self.output.close()


@pytest.fixture
def real_gui(tmp_path, monkeypatch, private_codex_runtime, isolated_claude_account):
    source = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
    config = tmp_path / 'codex-account'
    config.mkdir(mode=0o700)
    credential = source / 'auth.json'
    if credential.is_file():
        auth = json.loads(credential.read_text())
        if auth.get('auth_mode') in (None, 'chatgpt') and isinstance(auth.get('tokens'), dict) and not auth.get('OPENAI_API_KEY'):
            (config / 'auth.json').write_bytes(credential.read_bytes())
            (config / 'auth.json').chmod(0o600)
    monkeypatch.setenv('CODEX_HOME', str(config))
    gui = RealGUI(tmp_path, private_codex_runtime)
    try:
        gui.start()
        gui.request('PUT', '/api/providers/claude/settings', json=dict(settings=dict(
            billing_mode='subscription', extra_usage_disabled_confirmed=True, max_concurrent=2)))
        yield gui
    finally:
        gui.close()
        shutil.rmtree(config)


def assert_success(row):
    assert row['status'] == 'completed' and row['task_outcome'] == 'success', (
        row['status'], row['task_outcome'], row['status_detail'], row['outcome_reason'])


def test_native_edit_tests_subagent_image_sandbox_and_gui_restart(real_gui, git_repo):
    gui = real_gui
    (git_repo / 'calculator.py').write_text('def add(a, b):\n    raise NotImplementedError\n')
    (git_repo / 'test_calculator.py').write_text(
        'import unittest\nfrom calculator import add\nclass Addition(unittest.TestCase):\n'
        '    def test_add(self): self.assertEqual(add(2, 3), 5)\n')
    subprocess.run(['git', '-C', str(git_repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(git_repo), 'commit', '-qm', 'acceptance fixture'], check=True)
    contract = dict(required_changed_paths=['calculator.py'], require_any_change=True,
                    validation_commands=[['python3', '-m', 'unittest', '-v']])
    task = gui.create(git_repo,
        'Use the built-in Agent tool to delegate implementation of add in calculator.py to a foreground '
        'general-purpose native subagent. Do not simulate delegation using Bash. After it returns, YOU run '
        'python3 -m unittest -v using Bash. Do not commit or change test assertions. Return the Completion JSON.',
        allow_subagents=True, completion_contract=contract)
    done = gui.wait(task['id'])
    assert_success(done)
    sid, worktree = done['session_id'], done['worktree']
    assert sid and Path(worktree, 'calculator.py').read_text() != (git_repo / 'calculator.py').read_text()
    assert any(agent['status'] == 'completed' for agent in done['provider_subagents']), done['provider_subagents']
    events = gui.events(task['id'])
    tool_names = [block.get('name') for entry in events for block in
                  (((entry.get('event') or {}).get('message') or {}).get('content') or []) if isinstance(block, dict)]
    assert 'Agent' in tool_names or 'Task' in tool_names
    assert any('Ran 1 test' in entry['message'] and 'OK' in entry['message'] for entry in events if entry['type'] == 'user')
    assert Path(worktree, 'test_calculator.py').read_text() == (git_repo / 'test_calculator.py').read_text()
    assert any(entry['type'] == 'stream_event' for entry in events)
    assert done['provider_usage']['duration_ms'] > 0
    assert done['provider_usage']['usage']['output_tokens'] > 0

    from playwright.sync_api import sync_playwright, expect
    with sync_playwright() as engine:
        browser = engine.chromium.launch(executable_path=os.environ.get('CODEX_GUI_CHROMIUM') or None)
        for width in (375, 1280):
            page = browser.new_page(viewport=dict(width=width, height=900))
            page.goto(gui.url + '/tasks/' + task['id'])
            expect(page.locator('#meta')).to_contain_text('Claude Code')
            expect(page.locator('#provider-task-subagents')).to_contain_text('completed')
            page.close()
        browser.close()

    image = gui.request('POST', '/api/attachments?filename=visual.png', content=labeled_image('NATIVE742', 'blue'))
    with tempfile.TemporaryDirectory(prefix='.pr19-native-denial-', dir=ROOT) as outside:
        target = Path(outside) / 'must-not-exist'
        target.write_text('parent write permission witness')
        target.unlink()
        gui.request('POST', f'/api/tasks/{task["id"]}/messages', json=dict(
            prompt='Read the attached image. Then use Bash to attempt this exact out-of-worktree write: '
                   f'printf forbidden > {target}. It MUST fail under the sandbox. Do not use file-edit tools, '
                   'do not request additional directories or retry unsandboxed. If denied, return SUCCESS '
                   'with the image letters/digits in reason; if it succeeds return BLOCKED.', attachment_ids=[image['id']]))
        second = gui.wait(task['id'])
        assert_success(second)
        assert second['session_id'] == sid and second['worktree'] == worktree
        assert 'NATIVE742' in second['outcome_reason']
        assert not target.exists()
        assert any(('read-only file system' in e['message'].lower() or 'operation not permitted' in e['message'].lower())
                   for e in gui.events(task['id']) if e['type'] == 'user')

    old_pid = gui.proc.pid
    gui.stop()
    gui.start()
    assert gui.proc.pid != old_pid
    assert gui.task(task['id'])['session_id'] == sid
    gui.request('POST', f'/api/tasks/{task["id"]}/messages', json=dict(
        prompt='Add subtract(a,b) to calculator.py and add one unittest for subtract(5,2)==3. '
               'Keep add and its test. Run python3 -m unittest -v using Bash. Then call the StructuredOutput tool '
               'with status SUCCESS and a short reason if all work passed. The final response must be only that '
               'JSON object. Do not write explanatory prose before or after the JSON.'))
    third = gui.wait(task['id'])
    assert_success(third)
    assert third['session_id'] == sid and third['worktree'] == worktree
    assert 'def subtract' in Path(worktree, 'calculator.py').read_text()
    assert len(third['messages']) == 3
    assert third['messages'][1]['attachment_ids'] == [image['id']]
    (gui.folder / 'native-acceptance-evidence.json').write_text(json.dumps(dict(
        task=task['id'], session=sid, worktree=worktree, previous_gui_pid=old_pid, restarted_gui_pid=gui.proc.pid,
        provider_usage=third['provider_usage'], native_subagents=done['provider_subagents'],
        outcome=third['task_outcome'], completion_checks=third['completion_checks']), indent=2))
    print(f'real acceptance PASS: edits/tests/native Agent/image/sandbox/restart; task={task["id"]}; session={sid}')
