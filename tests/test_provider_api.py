import time

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
