"""Opt-in localhost/temporary SQLite/Git browser E2E with independent fixture CLIs."""
import asyncio
import json
import os
from pathlib import Path
import time

import pytest

from app.autopilot.git import git
from goal_helpers import install_goals
from test_ui_attachments_browser import gui_url  # noqa: F401

pytestmark = pytest.mark.skipif(os.environ.get("CODEX_GUI_BROWSER") != "1", reason="opt-in isolated Chromium")


@pytest.mark.parametrize("mode", ["auto_within_envelope", "propose_only"])
def test_markdown_goal_review_root_cause_and_graph_decision_in_browser(gui_url, git_repo, tmp_path, mode):  # noqa: F811
    playwright = pytest.importorskip("playwright.sync_api")
    url, app = gui_url
    async def seed():
        (git_repo / "input.csv").write_text("good,data\n")
        await git(git_repo, "add", "input.csv")
        await git(git_repo, "commit", "-qm", "Public CSV counterexample")
    asyncio.run(seed())
    install_goals(app.state.manager)
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=dict(width=390, height=844))
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(url + "/autopilot")
        fixture = Path(__file__).parent / "fixtures/autopilot-goals"
        page.locator("#ap-files").set_input_files([str(path) for path in sorted(fixture.glob("*.md"))])
        page.locator("#ap-import").click()
        page.wait_for_function("!document.querySelector('#ap-convert').disabled")
        assert "G-local" in page.get_by_label("COMMON.md", exact=True).input_value()
        page.locator("#ap-repository").fill(str(git_repo))
        page.locator("#ap-import-commit").check()
        page.locator("#ap-import-billing").check()
        page.locator("#ap-convert").click()
        page.wait_for_function("!document.querySelector('#ap-start').disabled")
        raw = json.loads(page.locator("#ap-manifest").input_value())
        raw["policy"]["replanning"] = dict(mode=mode)
        page.locator("#ap-manifest").fill(json.dumps(raw))
        page.locator("#ap-preview").click()
        page.wait_for_function("!document.querySelector('#ap-start').disabled")
        page.locator("#ap-opt-in").check()
        page.locator("#ap-start").click()
        wid = None
        deadline = time.monotonic() + 40
        approved = False
        while time.monotonic() < deadline:
            workflows = page.request.get(url + "/api/autopilot/workflows").json()["workflows"]
            if workflows:
                wid = workflows[0]["id"]
                view = page.request.get(url + "/api/autopilot/workflows/" + wid).json()
                if mode == "propose_only" and view["graph_revisions"] and not approved:
                    page.get_by_role("button", name="Owner approve root-cause plan", exact=True).wait_for()
                    note = page.get_by_placeholder("所有者判断の理由", exact=True)
                    note.fill("Reviewed this exact public fixture graph")
                    page.wait_for_timeout(3200)
                    assert note.input_value() == "Reviewed this exact public fixture graph"
                    assert "old_graph" in page.locator("#ap-graph").inner_text()
                    page.get_by_role("button", name="Owner approve root-cause plan", exact=True).click()
                    approved = True
                if view["state"] == "completed":
                    break
            time.sleep(0.05)
        else:
            raise AssertionError(view if wid else "workflow never registered")
        page.wait_for_function("document.querySelector('#ap-graph').textContent.includes('superseded')")
        graph = page.locator("#ap-graph").inner_text()
        assert "G-local" in graph and "Goal Alignment" in graph and "TASK_ASSUMPTION_FALSE" in graph
        assert "根本原因からの新タスク案" in graph and "独立Codex根本原因分析" in graph
        assert not errors
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1")
        page.screenshot(path=str(tmp_path / ("goal-replanning-" + mode + ".png")), full_page=True)
        browser.close()
