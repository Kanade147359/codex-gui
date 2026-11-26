import asyncio
import json
from pathlib import Path
import sqlite3

import pytest

from app.providers import ClaudeCodeProvider, ProviderError, claude_env, session_id
from app.task_manager import TaskError
from conftest import wait_for

FAKE = str(Path(__file__).with_name('fake_claude.py'))


def fixture_calls(worktree):
    git_meta = Path((Path(worktree) / '.git').read_text().split(':', 1)[1].strip())
    return git_meta / 'fake-claude' / 'calls.jsonl'


def configure(manager):
    manager.providers.save('claude', dict(binary=FAKE, billing_mode='subscription', extra_usage_disabled_confirmed=True))
    manager.providers.pause('claude', False)
    return manager


async def create(manager, repo, prompt='ok', **fields):
    return await manager.create_task(repository=str(repo), prompt=prompt, provider='claude',
                                     provider_options=dict(trusted_repository=True), **fields)


async def finished(manager, task, timeout=15):
    return await wait_for(lambda: manager.get(task['id']) if manager.get(task['id'])['status'] not in ('queued', 'starting', 'running') else None, timeout=timeout)


def test_claude_discovery_is_local_only():
    provider = ClaudeCodeProvider(FAKE)
    info = asyncio.run(provider.describe())
    assert info['available'] and info['authenticated'] and info['billing'] == 'subscription'
    assert info['efforts'] == ['low', 'medium', 'high', 'xhigh', 'max']
    assert info['models'] == [] and info['quota'] is None
    assert not info['capabilities']['compaction']


@pytest.mark.parametrize('status', ['SUCCESS', 'BLOCKED', 'NEEDS_INPUT', 'PARTIAL'])
def test_terminal_completion_json_uses_the_existing_strict_contract(status):
    provider = ClaudeCodeProvider(FAKE)
    events = provider.events(dict(type='result', subtype='success', is_error=False,
                                 result=json.dumps(dict(status=status, reason='actual terminal result'))))
    final = [event for event in events if event.kind == 'final']
    assert len(final) == 1
    assert json.loads(final[0].data['text']) == dict(status=status, reason='actual terminal result')


@pytest.mark.parametrize('text', [
    'done', '```json\n{"status":"SUCCESS","reason":"wrapped"}\n```',
    '{"status":"PARTIAL","status":"SUCCESS","reason":"duplicate"}',
    '{"status":"SUCCESS","reason":""}', '{"status":"SUCCESS","reason":"ok","extra":true}',
])
def test_terminal_prose_and_invalid_json_do_not_satisfy_completion(text):
    events = ClaudeCodeProvider(FAKE).events(dict(type='result', subtype='success', is_error=False, result=text))
    assert not any(event.kind == 'final' for event in events)


def test_malformed_structured_output_cannot_fall_back_to_other_terminal_text():
    events = ClaudeCodeProvider(FAKE).events(dict(type='result', subtype='success', is_error=False,
              structured_output=None, result='{"status":"SUCCESS","reason":"must not replace invalid envelope"}'))
    assert not any(event.kind == 'final' for event in events)


def test_completion_protocol_is_system_instruction_on_initial_and_resumed_turns():
    provider = ClaudeCodeProvider(FAKE)
    for sid in (None, '11111111-1111-4111-8111-111111111111'):
        argv = provider.build_command(dict(provider_options='{}'), sid)
        protocol = argv[argv.index('--append-system-prompt') + 1]
        assert 'SUCCESS means the entire request is fulfilled' in protocol
        assert 'no preamble, Markdown, or trailing prose' in protocol


def test_terminal_json_completion_through_the_real_process_pipeline(git_repo, make_manager):
    manager = configure(make_manager())
    async def scenario():
        try:
            task = await create(manager, git_repo, 'terminal-json')
            row = await finished(manager, task)
            assert row['status'] == 'completed' and row['task_outcome'] == 'success'
        finally:
            await manager.shutdown()
    asyncio.run(scenario())


def test_regular_file_inputs_remain_scoped_and_are_not_encoded_as_claude_images(git_repo, make_manager):
    manager = configure(make_manager())
    payload = b'opaque-file-content-never-inline-93e1'
    async def stream():
        yield payload
    async def scenario():
        try:
            upload = await manager.attachments.upload_file(stream(), 'input.txt')
            task = await create(manager, git_repo, 'Read the file', attachment_ids=[upload['id']])
            first = await finished(manager, task)
            assert first['task_outcome'] == 'success'
            await manager.send_instruction(task['id'], 'Read the same input again', attachment_ids=[upload['id']])
            done = await finished(manager, task)
            assert done['task_outcome'] == 'success' and session_id(done) == session_id(first)
            calls = [json.loads(line) for line in fixture_calls(done['worktree']).read_text().splitlines()]
            assert len(calls) == 2
            for call in calls:
                assert 'Attached input files' in call['prompt'] and 'input.txt' in call['prompt']
                assert payload.decode() not in call['prompt']
                assert call['images'] == 0
        finally:
            await manager.shutdown()
    asyncio.run(scenario())


def test_new_resume_schedule_and_restart(git_repo, make_manager):
    m = configure(make_manager())
    async def scenario():
        task = await create(m, git_repo)
        done = await finished(m, task)
        assert done['task_outcome'] == 'success', done
        sid = session_id(done)
        assert sid and done['codex_thread_id'] is None
        assert m.present_task(done)['session_id'] == sid
        await m.send_instruction(task['id'], 'continue')
        done = await finished(m, task)
        assert session_id(done) == sid
        assert len(m.db.list_turns(task['id'])) == 2
        assert all(t['input_tokens'] == 44 and t['output_tokens'] == 8 for t in m.db.list_turns(task['id']))
        row = await m.schedule_instruction(task['id'], 'scheduled')
        for _ in range(100):
            m.tick()
            if m.db.get_scheduled(row['id'])['status'] == 'completed': break
            await asyncio.sleep(.02)
        assert m.db.get_scheduled(row['id'])['status'] == 'completed'
        assert session_id(m.get(task['id'])) == sid
        await m.shutdown()
        m.detach()
        other = configure(make_manager())
        other.recover()
        await other.send_instruction(task['id'], 'after restart')
        assert session_id(await finished(other, task)) == sid
        calls = [json.loads(s) for s in fixture_calls(done['worktree']).read_text().splitlines()]
        assert [c['resume'] for c in calls] == [False, True, True, True]
        assert 'after restart' in calls[-1]['prompt'] and 'scheduled' not in calls[-1]['prompt']
        await other.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize('prompt,kind,status', [
    ('quota', 'quota', 'waiting-for-quota'), ('overage', 'billing_violation', 'failed'), ('duplicate-fields', 'invalid_output', 'failed'), ('auth-expired', 'authentication_error', 'failed'),
    ('missing-session', 'session_missing', 'failed'), ('wrong-session', 'session_mismatch', 'failed'),
    ('permission', 'permission_required', 'failed'), ('duplicate-result', 'invalid_output', 'failed'),
    ('no-result', 'invalid_output', 'failed'), ('bad-shape', 'invalid_output', 'failed'),
])
def test_failure_does_not_release_dependencies(git_repo, make_manager, prompt, kind, status):
    m = configure(make_manager())
    async def scenario():
        task = await create(m, git_repo)
        original_sid = session_id(await finished(m, task))
        await m.send_instruction(task['id'], prompt)
        done = await finished(m, task)
        assert (done['last_failure_kind'], done['status']) == (kind, status), done
        assert done['task_outcome'] != 'success' and done['retry_count'] == 0
        assert session_id(done) == original_sid  # never silently replace persisted identity
        await m.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize('prompt,outcome', [('blocked','blocked'), ('needs-input','needs_input'), ('partial','incomplete'), ('no-structured','needs_review')])
def test_semantic_result_gate(git_repo, make_manager, prompt, outcome):
    m = configure(make_manager())
    async def scenario():
        task = await create(m, git_repo, prompt)
        done = await finished(m, task)
        assert done['status'] == 'completed' and done['task_outcome'] == outcome
        assert done['approved_at'] is None and done['approval_source'] == ''
        await m.shutdown()
    asyncio.run(scenario())


def test_streams_tools_subagents_and_usage(git_repo, make_manager):
    m = configure(make_manager())
    async def scenario():
        task = await create(m, git_repo, 'subagents', allow_subagents=True)
        done = await finished(m, task)
        data = m.present_task(done)
        assert data['provider_subagents'][0]['status'] == 'completed'
        assert data['provider_usage']['duration_ms'] == 42
        logs = m.read_log(task['id'])[0]
        assert any('Implementing' in e['message'] for e in logs)
        assert any('Read' in e['message'] for e in logs)
        assert any('file contents' in e['message'] for e in logs)
        assert any(e['type'] == 'future-event' for e in logs)
        assert any(e['type'] == 'raw' for e in logs)
        await m.shutdown()
    asyncio.run(scenario())


def test_two_providers_overlap_stop_and_dependencies(git_repo, make_manager):
    m = configure(make_manager(backend='app-server'))
    async def scenario():
        c = await create(m, git_repo, 'sleep')
        x = await m.create_task(repository=str(git_repo), prompt='sleep')
        await wait_for(lambda: m.get(c['id'])['status'] == m.get(x['id'])['status'] == 'running')
        await wait_for(lambda: session_id(m.get(c['id'])) and session_id(m.get(x['id'])))
        with pytest.raises(TaskError): await m.send_instruction(c['id'], 'steer')
        await m.stop(c['id'])
        assert (await finished(m, c))['status'] == 'stopped'
        await m.stop(x['id'])
        await finished(m, x)
        await m.send_instruction(c['id'], 'ok')
        await finished(m, c)
        dep = await m.create_task(repository=str(git_repo), prompt='ok', depends_on=[c['id']])
        assert (await finished(m, dep))['task_outcome'] == 'success'
        await m.shutdown()
    asyncio.run(scenario())


def test_billing_trust_missing_cli_and_isolated_env(git_repo, make_manager, monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-ant-never-leak-test-secret')
    monkeypatch.setenv('ANTHROPIC_BASE_URL', 'https://example.invalid')
    monkeypatch.setenv('NODE_OPTIONS', '--require=/untrusted.js')
    monkeypatch.setenv('SSH_AUTH_SOCK', '/tmp/test-agent')
    assert not {'ANTHROPIC_API_KEY', 'ANTHROPIC_BASE_URL', 'NODE_OPTIONS'} & claude_env().keys()
    assert claude_env()['SSH_AUTH_SOCK'] == '/tmp/test-agent'
    m = make_manager()
    async def scenario():
        m.providers.save('claude', dict(binary=FAKE))
        with pytest.raises(TaskError, match='Confirm subscription'):
            await create(m, git_repo)
        configure(m)
        with pytest.raises(TaskError, match='repository trust'):
            await m.create_task(repository=str(git_repo), prompt='ok', provider='claude')
        m.providers.save('claude', dict(binary='/missing/claude'))
        with pytest.raises(TaskError, match='unavailable'):
            await create(m, git_repo)
        x = await m.create_task(repository=str(git_repo), prompt='ok')
        assert (await finished(m, x))['task_outcome'] == 'success'
        await m.shutdown()
    asyncio.run(scenario())


def test_session_ownership_and_provider_limits(git_repo, make_manager):
    m = configure(make_manager())
    async def scenario():
        a = await create(m, git_repo)
        sid = session_id(await finished(m, a))
        b = await create(m, git_repo)
        await finished(m, b)
        with pytest.raises(ValueError, match='another task'): m.db.bind_session(b['id'], sid)
        m.providers.save('claude', dict(max_concurrent=1))
        c = await create(m, git_repo, 'sleep')
        d = await create(m, git_repo, 'sleep')
        await wait_for(lambda: m.get(c['id'])['status'] == 'running')
        assert m.get(d['id'])['status'] == 'queued'
        await m.shutdown()
    asyncio.run(scenario())


def test_provider_schema_rollback_and_legacy_migration(tmp_path, monkeypatch):
    from app.database import Database, SCHEMA, TASK_MIGRATIONS
    path = tmp_path / 'legacy.db'
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO tasks(id,name,repository,worktree,branch,base_ref,base_sha,prompt,status,created_at,codex_thread_id) VALUES('old','n','/r','/w','b','main','sha','p','completed','now','thread-old')")
    conn.commit(); conn.close()
    TASK_MIGRATIONS.append(('intentionally_broken', 'INVALID SQL )'))
    try:
        with pytest.raises(sqlite3.OperationalError): Database(path)
    finally:
        TASK_MIGRATIONS.pop()
    conn = sqlite3.connect(path)
    assert 'provider' not in [r[1] for r in conn.execute('PRAGMA table_info(tasks)')]
    conn.close()
    for _ in range(2):
        db = Database(path)
        old = db.get_task('old')
        assert old['provider'] == 'codex' and old['provider_session_id'] == 'thread-old'
        assert old['task_outcome'] == 'success'
        db.close()


def test_options_reject_bypass_and_argument_injection():
    with pytest.raises(ProviderError): ClaudeCodeProvider.validate_options({'permission_mode':'bypassPermissions'})
    with pytest.raises(ProviderError): ClaudeCodeProvider.validate_options({'extra_argv':['--dangerously-skip-permissions']})
    with pytest.raises(ProviderError): ClaudeCodeProvider.validate_options({'timeout_seconds':True})


def test_stop_resume_timeout_and_drain(git_repo, make_manager):
    m = configure(make_manager())
    async def scenario():
        task = await create(m, git_repo, 'sleep')
        await wait_for(lambda: session_id(m.get(task['id'])))
        sid = session_id(m.get(task['id']))
        await m.shutdown()
        assert m.get(task['id'])['status'] == 'interrupted'
        m.detach()
        other = configure(make_manager())
        other.recover()
        await other.send_instruction(task['id'], 'continue')
        assert session_id(await finished(other, task)) == sid
        timeout = await other.create_task(repository=str(git_repo), prompt='sleep', provider='claude',
                                          provider_options=dict(trusted_repository=True, timeout_seconds=1), auto_retry=False)
        done = await finished(other, timeout)
        assert done['last_failure_kind'] == 'timeout' and done['status'] == 'failed'
        await other.shutdown()
    asyncio.run(scenario())


def test_quota_pauses_only_that_provider(git_repo, make_manager):
    m = configure(make_manager())
    async def scenario():
        quota = await create(m, git_repo, 'quota')
        assert (await finished(m, quota))['status'] == 'waiting-for-quota'
        queued = await create(m, git_repo)
        codex = await m.create_task(repository=str(git_repo), prompt='ok')
        assert (await finished(m, codex))['task_outcome'] == 'success'
        assert m.get(queued['id'])['status'] == 'queued'
        m.providers.pause('claude', False)
        m.tick()
        assert (await finished(m, queued))['task_outcome'] == 'success'
        await m.shutdown()
    asyncio.run(scenario())


def test_images_initial_and_resume(git_repo, make_manager):
    import io
    from PIL import Image
    m = configure(make_manager())
    image = io.BytesIO()
    Image.new('RGB', (2, 2)).save(image, format='PNG')
    async def scenario():
        attachment = m.attachments.save(image.getvalue(), 'test.png')
        task = await create(m, git_repo, attachment_ids=[attachment['id']])
        await finished(m, task)
        await m.send_instruction(task['id'], 'continue with image', attachment_ids=[attachment['id']])
        done = await finished(m, task)
        calls = [json.loads(s) for s in fixture_calls(done['worktree']).read_text().splitlines()]
        assert [c['images'] for c in calls] == [1, 1]
        assert len(m.present_task(done)['messages']) == 2
        await m.shutdown()
    asyncio.run(scenario())


def test_abnormal_exit_retries_existing_session(git_repo, make_manager):
    m = configure(make_manager(retry_backoff_seconds=(0.01,)))
    async def scenario():
        task = await create(m, git_repo, 'crash', max_retries=1)
        first = await finished(m, task)
        assert first['status'] == 'retry_wait'
        sid = session_id(first)
        await asyncio.sleep(.03)
        m.tick()
        done = await finished(m, task)
        assert done['status'] == 'completed' and session_id(done) == sid
        calls = [json.loads(s) for s in fixture_calls(done['worktree']).read_text().splitlines()]
        assert len(calls) == 2 and calls[1]['resume']
        assert calls[1]['prompt'].startswith('Previous turn was interrupted unexpectedly.')
        await m.shutdown()
    asyncio.run(scenario())


def test_backup_restore_preserves_both_providers_sessions_and_dependencies(git_repo, make_manager, tmp_path):
    from app.database import Database
    m = configure(make_manager())
    async def scenario():
        a = await create(m, git_repo)
        before = await finished(m, a)
        b = await m.create_task(repository=str(git_repo), prompt='ok', depends_on=[a['id']])
        await finished(m, b)
        await m.shutdown()
        snapshot = tmp_path / 'restore.db'
        m.db.backup(snapshot)
        restored = Database(snapshot)
        assert restored.get_task(a['id'])['provider_session_id'] == before['provider_session_id']
        assert restored.dependencies_of(b['id']) == [a['id']]
        assert restored.get_task(b['id'])['provider'] == 'codex'
        assert restored.get_settings('provider.claude') == m.db.get_settings('provider.claude')
        restored.bind_session(a['id'], before['provider_session_id'])
        assert restored.list_turns(a['id'])[0]['provider'] == 'claude' 
        restored.close()
    asyncio.run(scenario())


def test_claude_argv_and_managed_policy_fail_closed(tmp_path):
    from app.providers import validate_managed_settings
    provider = ClaudeCodeProvider(FAKE)
    task = dict(provider_options='{}', model='sonnet', reasoning_effort='high', allow_subagents=False)
    argv = provider.build_command(task)
    assert '--safe-mode' in argv and '--dangerously-skip-permissions' not in argv and '--bare' not in argv
    assert argv[argv.index('--setting-sources')+1] == ''
    settings = json.loads(argv[argv.index('--settings')+1])
    assert settings['sandbox']['enabled'] and settings['sandbox']['allowUnsandboxedCommands'] is False
    assert settings['sandbox']['failIfUnavailable'] is True and settings['sandbox']['excludedCommands'] == []
    assert settings['disableAllHooks'] and settings['forceLoginMethod'] == 'claudeai'
    assert '--no-session-persistence' not in argv
    with pytest.raises(ProviderError): provider.build_command(task, '/other/task/transcript.jsonl')
    path = tmp_path / 'managed-settings.json'
    for policy in ({'hooks':{'SessionStart':[]}}, {'sandbox':{'enabled':False}}, {'apiKeyHelper':'secret-helper'},
                   {'env':{'ANTHROPIC_BASE_URL':'untrusted'}}, {'permissions':{'defaultMode':'bypassPermissions'}}):
        path.write_text(json.dumps(policy))
        with pytest.raises(ProviderError, match='managed policy'): validate_managed_settings([path])
    path.write_text('{}')
    validate_managed_settings([path])


def test_exited_leaders_children_are_collected(git_repo, make_manager):
    import time
    m = configure(make_manager())
    async def scenario():
        started = time.monotonic()
        task = await create(m, git_repo, 'orphan')
        done = await finished(m, task)
        assert time.monotonic() - started < 10
        pid = int(fixture_calls(done['worktree']).with_name('child.pid').read_text())
        proc_status = Path(f'/proc/{pid}/status')
        assert not proc_status.exists() or '\nState:\tZ' in proc_status.read_text()
        await m.shutdown()
    asyncio.run(scenario())


def test_repository_trust_and_auth_before_worktree_hooks(git_repo, make_manager, tmp_path):
    marker = tmp_path / 'hook-ran'
    hook = git_repo / '.git/hooks/post-checkout'
    hook.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\n')
    hook.chmod(0o700)
    m = make_manager()
    async def scenario():
        with pytest.raises(TaskError, match='repository trust'):
            await m.create_task(repository=str(git_repo), prompt='ok', provider='claude')
        m.providers.save('claude', dict(binary=FAKE))
        with pytest.raises(TaskError, match='Confirm subscription'):
            await create(m, git_repo)
        assert not marker.exists() and not m.db.list_tasks()
        configure(m)
        task = await create(m, git_repo)
        assert (await finished(m, task))['task_outcome'] == 'success'
        assert not marker.exists()
        await m.shutdown()
    asyncio.run(scenario())


def test_codex_environment_and_all_log_streams_redact_credentials(tmp_path, monkeypatch):
    from app.appserver import subscription_env
    from app.logstore import TaskLog
    secret = 'test-private-credential-value'
    monkeypatch.setenv('ANTHROPIC_API_KEY', secret)
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', '/private/claude-login')
    monkeypatch.setenv('SSH_AUTH_SOCK', '/tmp/owned-agent')
    env = subscription_env(True)
    assert not {'ANTHROPIC_API_KEY','CLAUDE_CONFIG_DIR'} & env.keys()
    assert env['SSH_AUTH_SOCK'] == '/tmp/owned-agent'
    path = tmp_path / 'events.jsonl'
    log = TaskLog(path)
    log.add_stdout(json.dumps({'type':'item.completed', 'item':{'type':'agent_message','text':secret}}))
    log.add_stderr('Bearer ' + secret)
    log.add_event('tool', secret, {'access_token':secret})
    log.add_system(secret)
    log.close()
    assert secret not in path.read_text()
    assert '[redacted]' in path.read_text()
    assert all(isinstance(json.loads(line), dict) for line in path.read_text().splitlines())


@pytest.mark.parametrize('rules,outcome,evidence', [
    ({'required_paths':['missing.bin']}, 'incomplete', 'FAIL'),
    ({'require_any_change':True}, 'incomplete', 'FAIL'),
    ({'manual_approval':True}, 'needs_review', 'PASS'),
])
def test_claude_evidence_and_manual_completion_contracts(git_repo, make_manager, rules, outcome, evidence):
    m = configure(make_manager())
    async def scenario():
        task = await create(m, git_repo, completion_contract=rules)
        done = await finished(m, task)
        assert done['status'] == 'completed' and done['semantic_result']
        assert (done['task_outcome'], done['evidence_result']) == (outcome, evidence)
        assert not done['approved_at']
        await m.shutdown()
    asyncio.run(scenario())


def test_claude_blocked_completion_holds_then_releases_codex_dependency(git_repo, make_manager):
    m = configure(make_manager())
    async def scenario():
        parent = await create(m, git_repo, 'blocked')
        child = await m.create_task(repository=str(git_repo), prompt='ok', depends_on=[parent['id']])
        assert (await finished(m, parent))['task_outcome'] == 'blocked'
        assert m.get(child['id'])['status'] == 'waiting_dependencies' and m.db.list_attempts(child['id']) == []
        await m.send_instruction(parent['id'], 'ok')
        assert (await finished(m, parent))['task_outcome'] == 'success'
        assert (await finished(m, child))['task_outcome'] == 'success'
        assert len(m.db.list_attempts(child['id'])) == 1
        await m.shutdown()
    asyncio.run(scenario())


def test_provider_reconfiguration_defers_dispatch_without_duplicate_attempts(git_repo, make_manager):
    m = make_manager()
    async def scenario():
        m._provider_reconfiguring = True
        task = await m.create_task(repository=str(git_repo), prompt='ok')
        m.tick()
        assert m.get(task['id'])['status'] == 'queued' and not m._jobs
        assert m.db.list_attempts(task['id']) == []
        m._provider_reconfiguring = False
        m.tick(); m.tick()
        assert (await finished(m, task))['task_outcome'] == 'success'
        assert len(m.db.list_attempts(task['id'])) == 1
        await m.shutdown()
    asyncio.run(scenario())
