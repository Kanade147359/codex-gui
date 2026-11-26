"""Opt-in real desktop/mobile Chromium, temporary app/data, offline fake CLIs."""
import os
import time

import pytest

from test_providers import FAKE
from test_ui_attachments_browser import gui_url  # noqa: F401
from test_ui_mobile_browser import browser, no_overflow  # noqa: F401
from test_attachments import image_bytes

pytestmark = pytest.mark.skipif(os.environ.get('CODEX_GUI_BROWSER') != '1', reason='NOT_RUN: opt-in Chromium check')


@pytest.mark.parametrize('width', [375, 1280])
def test_claude_create_images_continue_schedule_and_settings(browser, gui_url, git_repo, width, tmp_path):
    from playwright.sync_api import expect
    url, app = gui_url
    # The account/catalog on this page is fully offline too.
    async def offline_codex():
        return dict(id='codex', available=True, version='fake', authenticated=True, billing='subscription',
                    capabilities={}, models=[])
    app.state.manager.providers.get('codex').describe = offline_codex
    async def offline_catalog():
        return dict(models=[dict(slug='fixture-codex', name='Fixture Codex', efforts=['low', 'high'],
                    default_effort='low', service_tiers=[])], recommended_model='fixture-codex',
                    default_model='', default_effort='low', error='')
    app.state.catalog.get = offline_catalog  # Settings invalidates the cache; discovery must stay offline too.
    context = browser.new_context(base_url=url, viewport={'width':width, 'height':900})
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    response = page.request.put('/api/providers/claude/settings', data={'settings':{'binary':FAKE, 'billing_mode':'subscription', 'extra_usage_disabled_confirmed':True, 'default_effort':'high'}})
    assert response.ok, response.text()
    page.goto(url)
    if width < 769:
        page.locator('[data-mobile-tab="settings"]').click()
    expect(page.locator('[data-provider-settings="claude"]')).to_contain_text('signed in')
    no_overflow(page)
    if width < 769:
        page.locator('[data-mobile-tab="tasks"]').click()
    page.locator('#new-task-btn').click()
    page.locator('#provider-select').select_option('claude')
    expect(page.locator('#claude-options')).to_be_visible()
    expect(page.locator('[name="service_tier"]')).not_to_be_visible()
    assert page.locator('#effort-select option').evaluate_all('(options) => options.map(o => o.value)') == ['default', 'low', 'medium', 'high', 'xhigh', 'max']
    expect(page.locator('#effort-select')).to_have_value('default')
    expect(page.locator('#effort-select option[value="default"]')).to_contain_text('High')
    page.locator('[name="repository"]').fill(str(git_repo))
    page.locator('[name="prompt"]').fill('subagents')
    page.locator('[name="claude_trusted"]').check()
    page.locator('[name="claude_subagents"]').check()
    image = tmp_path / 'input.png'; image.write_bytes(image_bytes())
    page.locator('#new-task-dialog input[type="file"]').set_input_files(str(image))
    page.locator('#run-btn').click()
    expect(page.locator('#new-task-dialog')).not_to_be_visible()
    deadline = time.monotonic() + 15
    task = None
    while time.monotonic() < deadline:
        tasks = page.request.get('/api/tasks').json()['tasks']
        if tasks and tasks[0]['status'] == 'completed': task = tasks[0]; break
        time.sleep(.05)
    assert task and task['task_outcome'] == 'success'
    assert task['reasoning_effort'] == 'high'
    page.goto(f"{url}/tasks/{task['id']}")
    expect(page.locator('#meta')).to_contain_text('Claude Code')
    expect(page.locator('#provider-task-subagents')).to_contain_text('completed')
    expect(page.locator('#provider-task-usage')).to_contain_text('42')
    expect(page.locator('#attempts-body td[title="' + task['session_id'] + '"]')).to_have_text(task['session_id'][:8])
    expect(page.locator('#send-fast-btn')).not_to_be_visible()
    expect(page.locator('#compact-btn')).not_to_be_visible()
    page.locator('#instruction').fill('continue')
    page.locator('#send-btn').click()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        done = page.request.get(f"/api/tasks/{task['id']}").json()
        if done['status'] == 'completed' and len(done['messages']) == 2: break
        time.sleep(.05)
    assert done['session_id'] == task['session_id'] and len(done['messages']) == 2
    page.locator('#instruction').fill('scheduled')
    page.locator('#schedule-btn').click()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        scheduled = page.request.get(f"/api/tasks/{task['id']}/scheduled").json()['scheduled']
        if scheduled and scheduled[0]['status'] == 'completed': break
        time.sleep(.05)
    assert scheduled and scheduled[0]['status'] == 'completed'
    assert page.request.get(f"/api/tasks/{task['id']}").json()['session_id'] == task['session_id']
    no_overflow(page)
    response = page.request.put('/api/providers/codex/settings', data={'settings':{'default_effort':'high'}})
    assert response.ok
    page.goto(url)
    page.locator('#new-task-btn').click()
    page.locator('#provider-select').select_option('codex')
    expect(page.locator('#model-select')).to_have_value('fixture-codex')
    expect(page.locator('#effort-select')).to_have_value('default')
    expect(page.locator('#effort-select option[value="default"]')).to_contain_text('High')
    page.locator('#effort-select').select_option('low')
    page.locator('#model-select').select_option('__custom__')
    expect(page.locator('#effort-select')).to_have_value('low')
    assert errors == []
    context.close()
