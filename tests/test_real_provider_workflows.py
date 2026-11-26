"""Real mixed-provider workflows; never enabled by the default test suite."""
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import time
import uuid

import pytest

from app.procinfo import process_identity
from test_real_claude import isolated_claude_account  # noqa: F401
from test_real_provider_acceptance import real_gui, assert_success, pytestmark  # noqa: F401


def wait_until(predicate, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.1)
    raise AssertionError('real acceptance condition timed out')


def codex_model(gui):
    account = gui.request('GET', '/api/codex/account')
    if not account.get('signed_in') or account.get('account_type') != 'chatgpt':
        pytest.skip('NOT_RUN: isolated Codex ChatGPT subscription authentication unavailable')
    limits = gui.request('GET', '/api/limits?refresh=true')
    if limits.get('ordinary_usage_allowed') is not True:
        pytest.skip('NOT_RUN: Codex included-plan usage cannot be confirmed; no paid credits/fallback')
    providers = gui.request('GET', '/api/providers')['providers']
    info = next(provider for provider in providers if provider['id'] == 'codex')
    model = os.environ.get('CODEX_GUI_REAL_MODEL') or info.get('default_model') or info.get('recommended_model')
    if not model:
        pytest.skip('NOT_RUN: no verified Codex model metadata')
    return model


def codex_fields(model):
    return dict(provider='codex', provider_options={}, model=model, reasoning_effort='low',
                auto_approval=True, adaptive_reasoning=False, web_search='disabled')


def seed_script(repository, name, source):
    (repository / name).write_text(source)
    subprocess.run(['git', '-C', str(repository), 'add', name], check=True)
    subprocess.run(['git', '-C', str(repository), 'commit', '-qm', 'trusted acceptance command'], check=True)


def test_real_provider_overlap_and_completion_dependency_release(real_gui, git_repo):
    gui = real_gui
    model = codex_model(gui)
    release = gui.folder / 'release-implementations'
    seed_script(git_repo, 'acceptance_wait.py',
        "from pathlib import Path\nimport sys,time\nPath(sys.argv[1]).write_text(sys.argv[2])\n"
        "release=Path(sys.argv[3])\ndeadline=time.monotonic()+120\n"
        "while not release.exists():\n    assert time.monotonic()<deadline\n    time.sleep(.1)\n")
    tasks = []
    for provider in ('claude', 'codex'):
        command = shlex.join(['python3', 'acceptance_wait.py', provider + '.txt', provider, str(release)])
        fields = codex_fields(model) if provider == 'codex' else {}
        tasks.append(gui.create(git_repo, 'Execute this exact command in the foreground with a tool timeout of at least '
                                '150 seconds, then return only SUCCESS Completion JSON. The external test releases it; '
                                'do not create the release file yourself.\n' + command,
                                completion_contract=dict(required_paths=[provider + '.txt'], require_any_change=True), **fields))
    try:
        def overlap():
            rows = [gui.task(task['id']) for task in tasks]
            assert not any(row['status'] in ('failed', 'stopped', 'waiting-for-quota') for row in rows), [
                (row['provider'], row['status'], row['status_detail']) for row in rows]
            if all(row['status'] == 'running' and Path(row['worktree'], row['provider'] + '.txt').exists() for row in rows):
                return rows
        rows = wait_until(overlap)
        assert rows[0]['worktree'] != rows[1]['worktree']
        assert all(row['session_id'] for row in rows)
        release.write_text('release')
        for task in tasks:
            assert_success(gui.wait(task['id']))
    finally:
        release.touch()

    parent = gui.create(git_repo, 'Return SUCCESS with reason artifact probe. Do not create files or call filesystem tools.',
                        completion_contract=dict(required_paths=['evidence.txt'], require_any_change=True))
    blocked = gui.wait(parent['id'])
    assert blocked['status'] == 'completed' and blocked['task_outcome'] != 'success'
    child = gui.create(git_repo, 'Return only SUCCESS Completion JSON with reason dependency released. Do not call tools.',
                       depends_on=[parent['id']], **codex_fields(model))
    assert gui.task(child['id'])['status'] == 'waiting_dependencies'
    assert gui.request('GET', f'/api/tasks/{child["id"]}/attempts')['attempts'] == []
    gui.request('POST', f'/api/tasks/{parent["id"]}/messages', json=dict(
        prompt='Create evidence.txt containing verified, then return only SUCCESS Completion JSON.'))
    done = gui.wait(parent['id'])
    assert_success(done)
    assert done['session_id'] == blocked['session_id']
    assert_success(gui.wait(child['id']))
    assert len(gui.request('GET', f'/api/tasks/{child["id"]}/attempts')['attempts']) == 1
    (gui.folder / 'mixed-acceptance-evidence.json').write_text(json.dumps(dict(
        overlapping_tasks=[dict(id=row['id'], provider=row['provider'], session=row['session_id'], worktree=row['worktree']) for row in rows],
        blocked_parent=parent['id'], released_child=child['id'], parent_session=done['session_id']), indent=2))
    print('real acceptance PASS: actual Codex/Claude overlap; Completion Gate holds and releases a Codex dependency')


def processes_with_marker(marker):
    result = {}
    for folder in Path('/proc').iterdir():
        if not folder.name.isdecimal():
            continue
        try:
            if str(marker).encode() not in (folder / 'cmdline').read_bytes():
                continue
            if '\nState:\tZ' not in (folder / 'status').read_text():
                pid = int(folder.name)
                result[pid] = process_identity(pid)
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            pass
    return result


def test_real_claude_interrupt_reap_shutdown_and_restart(real_gui, git_repo):
    gui = real_gui
    seed_script(git_repo, 'acceptance_sleep.py',
                "from pathlib import Path\nimport sys,time\nPath(sys.argv[1]).write_text('running')\ntime.sleep(120)\n")
    snapshots = {}
    try:
        for action in ('stop', 'shutdown'):
            marker_name = 'owned-shell-' + action + '-' + uuid.uuid4().hex
            task = gui.create(git_repo, 'Execute this command in foreground Bash with timeout 180000ms. '
                              'Do not background it or edit any files. Wait for it, then return Completion JSON.\n' +
                              shlex.join(['python3', 'acceptance_sleep.py', marker_name]))
            marker = Path(gui.task(task['id'])['worktree']) / marker_name
            def shell_started():
                row = gui.task(task['id'])
                assert row['status'] in ('queued', 'starting', 'running'), (row['status'], row['outcome_reason'])
                return marker.exists() and processes_with_marker(marker_name)
            wait_until(shell_started)
            before = gui.task(task['id'])
            assert before['status'] == 'running' and before['session_id']
            snapshots.update(processes_with_marker(marker_name))
            if action == 'stop':
                gui.request('POST', f'/api/tasks/{task["id"]}/stop')
                assert gui.wait(task['id'])['status'] == 'stopped'
            else:
                gui.stop()
                gui.start()
                assert gui.task(task['id'])['status'] == 'interrupted'
            wait_until(lambda: not processes_with_marker(marker_name), timeout=10)
            gui.request('POST', f'/api/tasks/{task["id"]}/messages', json=dict(
                prompt='Return only SUCCESS Completion JSON with reason resumed after interruption. Do not run tools.'))
            resumed = gui.wait(task['id'])
            assert_success(resumed)
            assert resumed['session_id'] == before['session_id'] and resumed['worktree'] == before['worktree']
        print('real acceptance PASS: stop and GUI shutdown collect native shell descendants; same-session restart')
    finally:
        # Never trust a PID reported inside a nested PID namespace. These IDs were
        # observed in our own /proc with unique test argv and saved start identities.
        for pid, identity in snapshots.items():
            if identity is not None and process_identity(pid) == identity:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


def test_real_autopilot_reviews_in_both_provider_directions(real_gui, git_repo):
    gui = real_gui
    model = codex_model(gui)
    raw = dict(version=1, name='real heterogeneous acceptance', repository=str(git_repo), base_ref='main',
        nodes=[dict(id=name, provider=provider, review_provider=reviewer,
                    provider_options=dict(trusted_repository=True, permission_mode='acceptEdits') if provider == 'claude' else {},
                    model=os.environ.get('CODEX_GUI_REAL_CLAUDE_MODEL', 'sonnet') if provider == 'claude' else model,
                    prompt=f'Create {name}.py with def value(): returning {value}. Only edit that file. '
                           'For any Python verification use python3 -B so no __pycache__ or other files are created. '
                           'Return only SUCCESS Completion JSON, using StructuredOutput when available. Do not commit.',
                    paths=[name + '.py'], validation=dict(commands=[['python3', '-B', '-c', f'from {name} import value; assert value()=={value}']], artifacts=[name + '.py']))
               for name, provider, reviewer, value in [('codex_impl', 'codex', 'claude', 7), ('claude_impl', 'claude', 'codex', 11)]],
        policy=dict(extra_usage_disabled_confirmed=True, allow_provider_fallback=False, claude_model=os.environ.get('CODEX_GUI_REAL_CLAUDE_MODEL', 'sonnet'), codex_model=model),
        permissions=dict(commit_owned_changes=True), inputs={})
    preview = gui.request('POST', '/api/autopilot/dry-run', json=dict(manifest=raw))
    workflow = gui.request('POST', '/api/autopilot/workflows', json=dict(
        manifest=raw, opt_in=True, expected_digest=preview['input_digest'], start=True))
    identity = workflow['id']
    deadline = time.monotonic() + 360
    while time.monotonic() < deadline:
        view = gui.request('GET', '/api/autopilot/workflows/' + identity)
        if view['state'] == 'completed':
            break
        states = [(node['name'], node['state'], node['reason'][:180]) for node in view['nodes']]
        assert not any(state in ('waiting_provider', 'failed', 'needs_human') or state.startswith('blocked_')
                       for _, state, _ in states), states
        time.sleep(.25)
    else:
        (gui.folder / 'autopilot-failure.json').write_text(json.dumps(view, indent=2))
        raise AssertionError('real heterogeneous workflow timeout: ' + str(states))
    for node in view['nodes']:
        task = gui.task(node['task_id'])
        assert_success(task)
        reviews = node['candidates'][-1]['reviews']
        assert reviews and reviews[-1]['provider'] != task['provider']
        assert reviews[-1]['payload']['session']
        assert reviews[-1]['payload']['session'] != task['session_id']
        assert not reviews[-1]['payload'].get('fallback_reason')
    assert len({gui.task(node['task_id'])['worktree'] for node in view['nodes']}) == 2
    (gui.folder / 'autopilot-acceptance-evidence.json').write_text(json.dumps(view, indent=2))
    print('real acceptance PASS: Autopilot Codex implementation -> Claude review AND Claude implementation -> Codex review')
