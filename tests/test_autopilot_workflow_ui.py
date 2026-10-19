"""HTTP contracts and opt-in browser coverage for durable workflow pages."""

import json
import os

import pytest

from autopilot_helpers import plan
from test_autopilot_api import client  # noqa: F401
from test_ui_attachments_browser import gui_url  # noqa: F401


def register(client, raw, **fields):
    preview = client.post('/api/autopilot/dry-run', json=dict(manifest=raw, **fields))
    assert preview.status_code == 200, preview.text
    response = client.post('/api/autopilot/workflows', json=dict(
        manifest=raw, expected_digest=preview.json()['input_digest'], opt_in=True, **fields))
    assert response.status_code == 200, response.text
    return client.get('/api/autopilot/workflows/' + response.json()['id']).json()


def test_execution_defaults_are_shared_by_preview_and_start_without_overriding_markdown(client, git_repo):
    raw = plan(git_repo)
    raw['inputs'] = {'C.md': 'Model: markdown-model\nReasoning effort: High\n'}
    raw['nodes'][0]['input_files'] = ['C.md']
    defaults = dict(implementation_model='base-model', reasoning_effort='high', mentor_enabled=True, mentor_max_rounds=2)
    workflow = register(client, json.dumps(raw), execution_defaults=defaults)
    settings = workflow['nodes'][0]['execution_settings']
    assert settings['model'] == 'markdown-model' and settings['reasoning_effort'] == 'high'
    assert settings['model_source'] == 'markdown: C.md'
    assert workflow['plan']['policy']['implementation_model'] == 'base-model'
    assert workflow['plan']['policy']['mentor_enabled'] is True
    assert not client.get('/api/tasks').json()['tasks']


def test_manifest_policy_remains_authoritative_and_bad_defaults_fail_closed(client, git_repo):
    raw = plan(git_repo)
    raw['policy'].update(implementation_model='manifest-model', reasoning_effort='high', mentor_enabled=False)
    workflow = register(client, raw, execution_defaults=dict(implementation_model='ui-model', reasoning_effort='medium', mentor_enabled=True))
    assert workflow['nodes'][0]['execution_settings']['model'] == 'manifest-model'
    assert workflow['nodes'][0]['execution_settings']['reasoning_effort'] == 'high'
    assert workflow['plan']['policy']['mentor_enabled'] is False
    for value in [[], {'publish_main': True}, {'mentor_max_rounds': -1}]:
        assert client.post('/api/autopilot/dry-run', json=dict(manifest=raw, execution_defaults=value)).status_code == 400
    assert client.post('/api/autopilot/dry-run', json=dict(manifest='x' * (1024 * 1024 + 1), execution_defaults={})).status_code == 400


def test_detail_page_and_listing_have_permanent_link_and_one_review_narrative(client, git_repo):
    raw = plan(git_repo, ('A', 'B'))
    raw['name'] = '<script>alert(1)</script>'
    workflow = register(client, raw)
    ap = client.app.state.manager.autopilot
    with ap.ledger.transaction() as connection:
        connection.execute("UPDATE ap_workflows SET state='paused' WHERE id=?", (workflow['id'],))
        for name, reason in [('A', '境界条件の確認が必要'), ('B', '成果物の説明が不足')]:
            connection.execute("UPDATE ap_nodes SET state='blocked_result',reason=? WHERE id=?", (reason, workflow['id'] + ':' + name))
    listing = client.get('/api/autopilot/workflows').json()['workflows'][0]
    assert listing['url'] == '/autopilot/workflows/' + workflow['id']
    assert listing['review_summary']['count'] == 2
    assert '境界条件' in listing['review_summary']['text'] and '成果物' in listing['review_summary']['text']
    assert listing['progress']['blocked'] == 2
    page = client.get(listing['url'])
    assert page.status_code == 200
    assert 'data-workflow-id="' + workflow['id'] + '"' in page.text
    assert '<script>alert(1)</script>' not in page.text
    assert 'ap-instruction-form' in page.text and 'ap-node-form' in page.text
    assert client.get('/autopilot/workflows/missing').status_code == 404
    assert client.get('/static/autopilot-common.js').status_code == 200
    assert client.get('/static/autopilot-workflow.js').status_code == 200


def test_addition_routes_persist_notes_and_child_specs_and_reject_stale_requests(client, git_repo):
    workflow = register(client, plan(git_repo))
    endpoint = '/api/autopilot/workflows/' + workflow['id']
    body = dict(owner_action=True, expected_target=workflow['control_target'], request_key='note-0000001', prompt='全体の補足')
    response = client.post(endpoint + '/instructions', json=body)
    assert response.status_code == 200
    assert response.json()['events'][0]['kind'] == 'note'
    assert client.post(endpoint + '/instructions', json=body).json()['events'] == response.json()['events']
    node = dict(id='D', prompt='追補文書を作成', kind='report', depends_on=['C'], paths=['docs/extra.md'],
                validation=dict(artifacts=['docs/extra.md']))
    added = client.post(endpoint + '/nodes', json=dict(owner_action=True, expected_target=workflow['control_target'], request_key='add-node-0001', node=node))
    assert added.status_code == 200, added.text
    assert [item['name'] for item in added.json()['nodes']] == ['C', 'D']
    assert added.json()['control_target'] != workflow['control_target']
    stale = client.post(endpoint + '/instructions', json=dict(body, request_key='note-0000002'))
    assert stale.status_code == 400 and 'plan changed' in stale.json()['detail']['message']
    persisted = client.get(endpoint).json()
    assert len(persisted['events']) == 2 and persisted['nodes'][1]['spec']['depends_on'] == ['C']
    assert not client.get('/api/tasks').json()['tasks']


@pytest.mark.parametrize('code', ['busy', 'no_session', 'autopilot_approval'])
def test_child_instruction_task_errors_are_actionable_http_errors(client, git_repo, monkeypatch, code):
    from app.autopilot import workflow_controls
    from app.task_manager import TaskError

    workflow = register(client, plan(git_repo))

    async def unavailable(*args):
        raise TaskError('Inspect the child before continuing', 409, code)

    monkeypatch.setattr(workflow_controls, 'add_instruction', unavailable)
    response = client.post('/api/autopilot/workflows/' + workflow['id'] + '/instructions', json=dict(
        owner_action=True, expected_target=workflow['control_target'], request_key='instruction-01', node_id='C', prompt='continue'))
    assert response.status_code == 409
    assert response.json()['detail']['code'] == code


browser_only = pytest.mark.skipif(os.environ.get('CODEX_GUI_BROWSER') != '1', reason='opt-in isolated Chromium check')


@browser_only
def test_browser_repository_candidates_browse_high_and_markdown_override(gui_url, git_repo):
    playwright = pytest.importorskip('playwright.sync_api')
    url, app = gui_url
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch()
        page = browser.new_page(viewport=dict(width=390, height=844))
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(url + '/autopilot')
        page.locator('#ap-recent-repos button').click()
        assert page.locator('#ap-repository').input_value() == str(git_repo)
        assert page.locator('#ap-effort').input_value() == 'high'
        assert page.locator('#ap-mentor-enabled').is_checked()
        page.locator('#ap-browse').click()
        page.locator('#ap-picker-select:not([disabled])').wait_for()
        page.locator('#ap-picker-select').click()
        assert not page.locator('#ap-picker').is_visible()
        assert page.locator('#ap-repository').input_value() == str(git_repo)
        page.locator('#ap-model').select_option('__custom__')
        page.locator('#ap-model-custom').fill('base-fixture')
        raw = plan(git_repo)
        raw['inputs'] = {'C.md': 'Model: markdown-fixture\n'}
        raw['nodes'][0]['input_files'] = ['C.md']
        page.locator('#ap-manifest').fill(json.dumps(raw))
        page.locator('#ap-preview').click()
        page.locator('#ap-start:not([disabled])').wait_for()
        assert 'markdown-fixture / HIGH' in page.locator('#ap-preview-summary').inner_text()
        assert 'markdown: C.md' in page.locator('#ap-preview-summary').inner_text()
        assert page.locator('#ap-preview-result').is_hidden()
        assert not page.request.get(url + '/api/tasks').json()['tasks']
        assert page.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1')
        assert not errors
        browser.close()


@browser_only
@pytest.mark.parametrize('width', [320, 1280])
def test_browser_workflow_detail_reload_aggregate_note_and_child_addition(gui_url, git_repo, tmp_path, width):
    playwright = pytest.importorskip('playwright.sync_api')
    url, app = gui_url
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch()
        page = browser.new_page(viewport=dict(width=width, height=900))
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        raw = plan(git_repo, ('A', 'B'))
        preview = page.request.post(url + '/api/autopilot/dry-run', data=dict(manifest=raw)).json()
        workflow = page.request.post(url + '/api/autopilot/workflows', data=dict(manifest=raw, expected_digest=preview['input_digest'], opt_in=True)).json()
        identity = workflow['id']
        with app.state.manager.autopilot.ledger.transaction() as connection:
            connection.execute("UPDATE ap_workflows SET state='paused' WHERE id=?", (identity,))
            for name, reason in [('A', '境界条件を確認'), ('B', '文書の検証が必要')]:
                connection.execute("UPDATE ap_nodes SET state='blocked_result',reason=? WHERE id=?", (reason, identity + ':' + name))
        page.goto(url + '/autopilot')
        page.locator('#ap-workflows h3 a').click()
        page.wait_for_url('**/autopilot/workflows/' + identity)
        page.locator('#ap-review-summary').wait_for()
        assert page.locator('#ap-review-summary').count() == 1
        narrative = page.locator('#ap-review-summary > .ap-review-text').inner_text()
        assert '境界条件' in narrative and '文書の検証' in narrative
        assert page.locator('#ap-graph > pre').count() == 0
        page.locator('#ap-instruction-prompt').fill('保存前の下書き')
        page.reload()
        page.locator('#ap-review-summary').wait_for()
        assert page.locator('#ap-instruction-prompt').input_value() == '保存前の下書き'
        page.locator('#ap-instruction-send').click()
        page.wait_for_function("document.querySelector('#ap-workflow-history').textContent.includes('保存前の下書き')")
        page.locator('#ap-add-node-details > summary').click()
        page.locator('#ap-node-name').fill('C')
        page.locator('#ap-node-kind').select_option('report')
        page.locator('#ap-node-prompt').fill('追加の調査結果を記録')
        page.locator('#ap-node-dependencies').select_option('A')
        page.locator('#ap-node-paths').fill('docs/extra.md')
        page.locator('#ap-node-artifacts').fill('docs/extra.md')
        page.locator('#ap-node-add').click()
        page.wait_for_function("document.querySelectorAll('#ap-graph .ap-node').length === 3")
        page.reload()
        page.wait_for_function("document.querySelectorAll('#ap-graph .ap-node').length === 3")
        view = page.request.get(url + '/api/autopilot/workflows/' + identity).json()
        assert view['nodes'][2]['spec']['depends_on'] == ['A']
        assert len(view['events']) == 2
        assert page.locator('#ap-workflow-inputs > table').is_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1')
        assert not errors
        page.screenshot(path=str(tmp_path / f'autopilot-workflow-{width}.png'), full_page=True)
        browser.close()


@browser_only
def test_browser_completed_child_accepts_same_thread_instruction_and_links_to_workflow(gui_url, git_repo):
    from autopilot_helpers import FixtureProvider

    playwright = pytest.importorskip('playwright.sync_api')
    url, app = gui_url
    app.state.manager.autopilot.providers = dict(claude=FixtureProvider('claude'), codex=FixtureProvider('codex'))
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch()
        page = browser.new_page(viewport=dict(width=390, height=844))
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        raw = plan(git_repo)
        preview = page.request.post(url + '/api/autopilot/dry-run', data=dict(manifest=raw)).json()
        identity = page.request.post(url + '/api/autopilot/workflows', data=dict(manifest=raw,
            expected_digest=preview['input_digest'], opt_in=True)).json()['id']
        response = page.request.post(url + '/api/tasks', data=dict(repository=str(git_repo), prompt='Prepare C', name='C'))
        assert response.ok, response.text()
        task_id = response.json()['id']
        page.goto(url + '/tasks/' + task_id)
        page.wait_for_function("""async id => {
          const task = await (await fetch('/api/tasks/' + id)).json();
          return task.status === 'completed' && task.codex_thread_id && !task.completion_pending;
        }""", arg=task_id)
        task = app.state.manager.db.get_task(task_id)
        thread, worktree = task['codex_thread_id'], task['worktree']
        with app.state.manager.autopilot.ledger.transaction() as connection:
            connection.execute("UPDATE ap_workflows SET state='completed' WHERE id=?", (identity,))
            connection.execute("UPDATE ap_nodes SET state='accepted',task_id=? WHERE id=?", (task_id, identity + ':C'))
        page.goto(url + '/autopilot/workflows/' + identity)
        page.locator('#ap-instruction-node option[value="C"]').wait_for(state='attached')
        page.locator('#ap-instruction-node').select_option('C')
        page.locator('#ap-instruction-prompt').fill('追加の説明を記録してください')
        page.locator('#ap-instruction-send').click()
        page.wait_for_function("document.querySelector('#ap-workflow-history').textContent.includes('追加の説明を記録してください')")
        current = app.state.manager.db.get_task(task_id)
        assert current['codex_thread_id'] == thread and current['worktree'] == worktree
        view = page.request.get(url + '/api/autopilot/workflows/' + identity).json()
        assert view['events'][0]['payload']['thread'] == thread
        assert view['nodes'][0]['generation'] == 1
        assert view['nodes'][0]['state'] != 'accepted'
        page.get_by_role('link', name='Task詳細', exact=True).click()
        page.locator('#ap-task-review a').wait_for()
        assert page.locator('#ap-task-review a').get_attribute('href') == '/autopilot/workflows/' + identity
        assert not errors
        browser.close()
