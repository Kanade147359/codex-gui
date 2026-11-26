import asyncio

import pytest

from app.autopilot.policy import manifest
from app.autopilot.roles import reader_policy
from app.providers import session_id
from autopilot_helpers import drive, plan, register
from test_autopilot_execution_mentor import controlled_install  # noqa: F401
from test_providers import configure, fixture_calls


def test_heterogeneous_implementations_reviews_and_same_session_fix(git_repo, make_manager, controlled_install):
    m = configure(make_manager())
    ap = controlled_install(m)
    raw = plan(git_repo, ('A', 'C'))
    raw['nodes'][0].update(provider='claude', provider_options={'trusted_repository':True}, review_provider='codex')
    raw['nodes'][1].update(provider='codex', review_provider='claude')
    raw['policy'].update(allow_provider_fallback=False)
    async def scenario():
        identity = await register(ap, raw)
        await drive(m, lambda: ap.ledger.view(identity)['state'] == 'completed')
        view = ap.ledger.view(identity)
        a, c = sorted(view['nodes'], key=lambda n: n['name'])
        task = m.get(a['task_id'])
        assert task['provider'] == 'claude' and task['codex_thread_id'] is None
        assert a['fix_rounds'] == 1 and len(a['candidates']) == 2
        assert [r['provider'] for candidate in a['candidates'] for r in candidate['reviews']] == ['codex', 'codex']
        assert c['candidates'][0]['reviews'][0]['provider'] == 'claude'
        assert all(not review['payload'].get('fallback_reason') for node in (a, c) for candidate in node['candidates'] for review in candidate['reviews'])
        import json
        calls = [json.loads(s) for s in fixture_calls(task['worktree']).read_text().splitlines()]
        assert len(calls) == 2 and calls[0]['session'] == calls[1]['session'] and calls[1]['resume']
        assert m.get(c['task_id'])['worktree'] != task['worktree']
        await m.shutdown()
    asyncio.run(scenario())


def test_roles_manifest_and_unknown_capabilities(git_repo):
    raw = plan(git_repo)
    raw['policy']['roles'] = {
        'implementer':dict(provider='claude', model='sonnet', reasoning_effort='high', allow_subagents=True,
                           provider_options={'trusted_repository':True}),
        'reviewer':dict(provider='codex', model='review-model'),
        'mentor':dict(provider='claude', model='mentor-model'),
        'planner':dict(provider='claude', model='planner-model'),
    }
    result = manifest(raw)
    from app.autopilot.execution import task_fields
    fields = task_fields(result['nodes'][0], result, creation=True)
    assert fields['provider'] == 'claude' and fields['allow_subagents'] and fields['model'] == 'sonnet'
    assert reader_policy(result['policy'], 'reviewer', 'codex')['codex_model'] == 'review-model'
    assert reader_policy(result['policy'], 'planner', 'claude')['claude_model'] == 'planner-model'
    raw['policy']['roles']['reviewer']['allow_subagents'] = True
    with pytest.raises(ValueError, match='no tool/subagent'): manifest(raw)


def test_fallback_requires_explicit_authorization(git_repo, make_manager, controlled_install):
    from autopilot_helpers import FixtureProvider
    m = make_manager()
    ap = controlled_install(m, FixtureProvider('claude', {'quota':True}), FixtureProvider('codex'))
    raw = plan(git_repo)
    raw['policy']['allow_provider_fallback'] = False
    async def scenario():
        identity = await register(ap, raw)
        await drive(m, lambda: ap.ledger.view(identity)['nodes'][0]['state'] == 'waiting_provider')
        reviews = ap.ledger.rows('reviews')
        assert len(reviews) == 1 and reviews[0]['provider'] == 'claude'
        assert not ap.providers['codex'].calls
        await m.shutdown()
    asyncio.run(scenario())


def test_explicit_claude_queue_resume_is_same_session_idempotent_and_restart_safe(git_repo, make_manager, controlled_install):
    from app.task_manager import TaskManager
    from conftest import FakeRunner
    m = configure(make_manager())
    ap = controlled_install(m)
    raw = plan(git_repo, ('C',))
    raw['nodes'][0].update(provider='claude', provider_options={'trusted_repository':True}, prompt='quota-once')
    async def scenario():
        identity = await register(ap, raw)
        await drive(m, lambda: ap.ledger.get('nodes', identity + ':C')['state'] in
                    ('waiting_implementation_provider', 'probing_implementation'))
        await asyncio.sleep(.01)
        node = ap.ledger.get('nodes', identity + ':C')
        task = m.get(node['task_id'])
        assert task['status'] == 'waiting-for-quota' and m.providers.paused('claude')
        original_session = session_id(task)
        await m.shutdown()
        other = TaskManager(m.settings, m.db, FakeRunner())
        restarted = controlled_install(other)
        other.recover()
        try:
            for _ in range(5):
                other.tick()
                await asyncio.sleep(.01)
            assert other.get(task['id'])['status'] == 'waiting-for-quota'
            assert len(other.db.list_attempts(task['id'])) == 1
            # POST /api/providers/claude/resume-queue performs this explicit persisted action.
            other.providers.pause('claude', False)
            await drive(other, lambda: len(other.db.list_attempts(task['id'])) == 2 and
                        other.get(task['id'])['status'] == 'completed')
            done = other.get(task['id'])
            assert done['provider'] == 'claude' and session_id(done) == original_session
            assert done['worktree'] == task['worktree']
            row = restarted.ledger.get('nodes', node['id'])
            assert row['state'] not in ('waiting_implementation_provider', 'probing_implementation')
            assert not restarted._resume_quota_implementation(row, restarted.ledger.get('workflows', identity))
            for _ in range(5):
                other.tick()
                await asyncio.sleep(.01)
            assert len(other.db.list_attempts(task['id'])) == 2
            import json
            calls = [json.loads(line) for line in fixture_calls(task['worktree']).read_text().splitlines()]
            assert len(calls) == 2 and calls[-1]['resume'] and calls[-1]['session'] == original_session
        finally:
            await other.shutdown()
    asyncio.run(scenario())


def test_research_reports_use_both_providers(git_repo, make_manager, controlled_install):
    m = configure(make_manager())
    ap = controlled_install(m)
    raw = plan(git_repo, ('C', 'E'))
    raw['mode'] = 'research'
    raw['nodes'][0].update(provider='claude', provider_options={'trusted_repository':True}, review_provider='codex')
    raw['nodes'][1].update(provider='codex', review_provider='claude', depends_on=['C'])
    async def scenario():
        identity = await register(ap, raw)
        await drive(m, lambda: ap.ledger.view(identity)['state'] == 'completed')
        saved = ap.ledger.view(identity)
        assert saved['plan']['mode'] == 'research'
        assert all(n['spec']['kind'] == 'report' for n in saved['nodes'])
        assert {m.get(n['task_id'])['provider'] for n in saved['nodes']} == {'codex', 'claude'}
        await m.shutdown()
    asyncio.run(scenario())
