"""Opt-in browser regression: temporary DB/repository, fake Codex, localhost ephemeral port."""
import os

import pytest

from test_ui_attachments_browser import gui_url  # noqa: F401

pytestmark = pytest.mark.skipif(os.environ.get('CODEX_GUI_BROWSER') != '1', reason='opt-in Chromium check')


def test_storage_dry_run_settings_and_history(gui_url):
    playwright = pytest.importorskip('playwright.sync_api')
    url, _ = gui_url
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={'width': 390, 'height': 844})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            page.goto(url + '/storage')
            page.wait_for_function("!document.getElementById('gc-dry-run').disabled")
            assert page.locator('#gc-days').input_value() == '5'
            assert not page.locator('#gc-delete').is_checked()
            assert page.locator('#gc-run').is_disabled()
            page.locator('#gc-dry-run').click()
            page.wait_for_function("document.getElementById('gc-message').textContent === '完了しました。'")
            assert '候補 0 件' in page.locator('#gc-summary').inner_text()
            assert 'Dry Run' in page.locator('#gc-history').inner_text()
            page.locator('#gc-days').fill('7')
            page.locator('#gc-settings button').click()
            page.wait_for_function("document.getElementById('gc-message').textContent === '設定を保存しました。'")
            page.reload()
            page.wait_for_function("document.getElementById('gc-days').value === '7'")
            assert not errors
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        finally:
            browser.close()
