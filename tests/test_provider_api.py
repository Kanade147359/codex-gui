from fastapi.testclient import TestClient

from app.main import create_app
from conftest import FakeRunner, FakeAppServer
from test_api import wait_done
from test_providers import FAKE


def test_provider_settings_api_execution_and_explicit_new_provider_session(settings, git_repo):
    app = create_app(settings, FakeRunner(), FakeAppServer('fake'))
    with TestClient(app) as client:
        r = client.put('/api/providers/claude/settings', json={'settings':dict(binary=FAKE, billing_mode='subscription', extra_usage_disabled_confirmed=True)})
        assert r.status_code == 200
        task = client.post('/api/tasks', json=dict(repository=str(git_repo), prompt='ok')).json()
        before = wait_done(client, task['id'])
        response = client.post(f"/api/tasks/{task['id']}/new-session", json=dict(prompt='ok', provider='claude', model='', provider_options={'trusted_repository':True}))
        assert response.status_code == 200, response.text
        done = wait_done(client, task['id'])
        assert done['provider'] == 'claude' and done['session_id'] != before['session_id'] and done['codex_thread_id'] is None
        turns = client.get(f"/api/tasks/{task['id']}/usage").json()['turns']
        assert [t['provider'] for t in turns] == ['codex', 'claude']
        messages = client.get(f"/api/tasks/{task['id']}").json()['messages']
        assert len(messages) == 2
        assert 'Provider handoff: this is a NEW independent session' in messages[-1]['prompt']
        assert '"previous_provider": "codex"' in messages[-1]['prompt'] and '"validation_checks":' in messages[-1]['prompt']
        assert client.post(f"/api/tasks/{task['id']}/compact").status_code == 409
        assert client.post(f"/api/tasks/{task['id']}/messages", json={'prompt':'ok', 'service_tier':'fast'}).status_code == 400
        assert client.put('/api/providers/claude/settings', json={'settings':{'permission_mode':'bypassPermissions'}}).status_code == 400
        assert client.put('/api/providers/claude/settings', json={'settings':{'api_key':'do-not-store'}}).status_code == 400
        assert 'do-not-store' not in str(app.state.manager.db.get_settings())
        assert client.post('/api/tasks', json=dict(repository=str(git_repo), prompt='x', provider='unknown')).status_code == 422


def test_cli_paths_apply_to_independent_roles_and_survive_restart(settings):
    from app.config import Settings
    from app.database import Database
    from app.task_manager import TaskManager
    app = create_app(settings, FakeRunner())
    with TestClient(app) as client:
        for provider, binary in [('codex', '/configured/codex'), ('claude', FAKE)]:
            response = client.put(f'/api/providers/{provider}/settings', json={'settings':{'binary':binary}})
            assert response.status_code == 200
            assert app.state.manager.autopilot.providers[provider].binary == binary
        assert app.state.catalog.codex_bin == '/configured/codex'
    db = Database(settings.db_path)
    restarted_settings = Settings(home=settings.home, backend='exec')
    other = TaskManager(restarted_settings, db, FakeRunner())
    assert restarted_settings.codex_bin == '/configured/codex'
    assert restarted_settings.claude_bin == FAKE
    assert other.runner.codex_bin == '/configured/codex'
    assert other.autopilot.providers['codex'].binary == '/configured/codex'
    assert other.autopilot.providers['claude'].binary == FAKE
    other.detach()
    db.close()


def test_legacy_new_session_reasoning_field_remains_compatible(settings, git_repo):
    with TestClient(create_app(settings, FakeRunner())) as client:
        task = client.post('/api/tasks', json=dict(repository=str(git_repo), prompt='ok')).json()
        old = wait_done(client, task['id'])
        response = client.post(f"/api/tasks/{task['id']}/new-session", json=dict(prompt='ok', reasoning_effort='high'))
        assert response.status_code == 200
        done = wait_done(client, task['id'])
        assert done['status'] == 'completed' and done['reasoning_effort'] == old['reasoning_effort']


def test_provider_switch_requires_finishing_or_cancelling_scheduled_turns(settings, git_repo):
    app = create_app(settings, FakeRunner())
    with TestClient(app) as client:
        client.put('/api/providers/claude/settings', json={'settings':dict(binary=FAKE, billing_mode='subscription', extra_usage_disabled_confirmed=True)})
        task = client.post('/api/tasks', json=dict(repository=str(git_repo), prompt='ok')).json()
        before = wait_done(client, task['id'])
        parent = client.post('/api/tasks', json=dict(repository=str(git_repo), prompt='sleep 60')).json()
        m = app.state.manager
        scheduled = client.portal.call(m.schedule_instruction, task['id'], 'original Codex follow-up', [parent['id']], 'priority')
        body = dict(prompt='new independent session', provider='claude', provider_options={'trusted_repository':True})
        response = client.post(f"/api/tasks/{task['id']}/new-session", json=body)
        assert response.status_code == 409 and 'scheduled' in response.text
        held = client.get('/api/tasks/' + task['id']).json()
        assert held['provider'] == 'codex' and held['session_id'] == before['session_id']
        assert len(held['messages']) == len(before['messages'])
        assert m.db.get_scheduled(scheduled['id'])['service_tier'] == 'priority'
        assert m.db.get_scheduled(scheduled['id'])['status'] == 'waiting_dependencies'
        # The conditional DB write also rejects a schedule appearing after the manager's check.
        from app.models import InvalidTransition
        from app.database import Database
        import pytest
        m.cancel_scheduled(task['id'], scheduled['id'])
        writer = Database(settings.db_path)
        try:
            late = writer.create_scheduled(task['id'], 'concurrent reservation', 'default', [parent['id']])
            with pytest.raises(InvalidTransition):
                m.db.set_status(task['id'], 'queued', provider='claude', provider_session_id=None, codex_thread_id=None)
            writer.cancel_scheduled(late['id'])
        finally:
            writer.close()
        assert m.get(task['id'])['codex_thread_id'] == before['session_id']
        response = client.post(f"/api/tasks/{task['id']}/new-session", json=body)
        assert response.status_code == 200, response.text
        done = wait_done(client, task['id'])
        assert done['provider'] == 'claude' and done['session_id'] != before['session_id']
        assert m.db.get_scheduled(scheduled['id'])['status'] == 'cancelled'
        client.portal.call(m.stop, parent['id'])
