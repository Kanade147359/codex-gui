"""Optional browser E2E: temporary GUI/DB/repo, fixture CLIs, one Start and no approvals."""

import json
import os
import time
from pathlib import Path

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


def test_markdown_upload_ui_to_planning_parallel_fix_and_integration(gui_url, git_repo, tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    url, app = gui_url
    install(app.state.manager)
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=dict(width=390, height=844))
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(url + "/autopilot")
        fixture = Path(__file__).parent / "fixtures/autopilot-documents"
        page.locator("#ap-files").set_input_files([str(path) for path in sorted(fixture.glob("*.md"))])
        page.locator("#ap-import").click()
        page.wait_for_function("!document.querySelector('#ap-convert').disabled")
        assert page.get_by_label("COMMON.md", exact=True).input_value().startswith("# Shared")
        assert page.request.get(url + "/api/tasks").json()["tasks"] == []
        page.locator("#ap-repository").fill(str(git_repo))
        page.locator("#ap-import-commit").check()
        page.locator("#ap-convert").click()
        page.wait_for_function("!document.querySelector('#ap-start').disabled")
        page.locator("#ap-opt-in").check()
        page.locator("#ap-start").click()
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            workflows = page.request.get(url + "/api/autopilot/workflows").json()["workflows"]
            if workflows and workflows[0]["state"] == "completed":
                break
            time.sleep(0.05)
        else:
            state = page.request.get(url + "/api/autopilot/workflows/" + workflows[0]["id"]).json()
            raise AssertionError([(n["name"], n["state"], n["reason"]) for n in state["nodes"]])
        page.wait_for_function("document.querySelector('#ap-graph').textContent.includes('COMMON-required')")
        assert "計画revision: applied" in page.locator("#ap-graph").inner_text()
        assert not errors
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1")
        page.screenshot(path=str(tmp_path / "autopilot-markdown-mobile.png"), full_page=True)
        browser.close()
