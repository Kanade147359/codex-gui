"""Optional browser E2E: temporary GUI/DB/repo, fixture CLIs, one Start and no approvals."""

import json
import os
import time

import pytest

from test_ui_attachments_browser import gui_url  # noqa: F401
from autopilot_helpers import install, plan

pytestmark = pytest.mark.skipif(os.environ.get("CODEX_GUI_BROWSER") != "1", reason="opt-in isolated Chromium check")


def test_fixture_workflow_ui_candidates_fallback_findings_and_task_details(gui_url, git_repo, tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    url, app = gui_url
    install(app.state.manager)
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=dict(width=390, height=844))
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(url + "/autopilot")
        page.locator("#ap-manifest").fill(json.dumps(plan(git_repo, ("A", "B", "C"))))
        page.locator("#ap-preview").click()
        page.wait_for_function("!document.querySelector('#ap-start').disabled")
        page.locator("#ap-opt-in").check()
        page.locator("#ap-start").click()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            listing = page.request.get(url + "/api/autopilot/workflows").json()["workflows"]
            if listing and listing[0]["state"] == "completed":
                break
            time.sleep(0.05)
        else:
            raise AssertionError("browser fixture workflow did not complete")
        page.wait_for_function("document.querySelector('#ap-graph').textContent.includes('accepted')")
        graph = page.locator("#ap-graph").inner_text()
        assert "A-value" in graph and "confirmed Claude included quota" in graph and "codex" in graph
        assert not errors
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1")
        page.screenshot(path=str(tmp_path / "autopilot-mobile.png"), full_page=True)
        page.locator("#ap-graph a").first.click()
        page.wait_for_function("!document.querySelector('#ap-task-review').hidden")
        assert "AI review accepted" in page.locator("#ap-task-review").inner_text()
        assert not errors
        browser.close()
