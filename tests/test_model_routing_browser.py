"""Opt-in Chromium checks for routing consent, actual chain labels and responsive metrics."""
import os
import time

import pytest

from test_model_routing import MODELS
from test_ui_attachments_browser import gui_url  # noqa: F401
from test_ui_mobile_browser import browser, no_overflow  # noqa: F401

pytestmark = pytest.mark.skipif(os.environ.get("CODEX_GUI_BROWSER") != "1", reason="opt-in Chromium check")


def test_auto_requires_explicit_selection_and_shows_actual_chain(browser, gui_url):
    from playwright.sync_api import expect
    url, app = gui_url
    # No Sol: the UI must not advertise it or silently switch to Auto.
    models = [{**m, "name": m["slug"], "service_tiers": []} for m in (MODELS[0], MODELS[2])]
    app.state.catalog._cached = dict(models=models, default_model="", default_effort="low",
                                    recommended_model=MODELS[0]["slug"], error="")
    app.state.catalog._fetched_at = time.monotonic()
    page = browser.new_page()
    page.goto(url)
    page.locator("#new-task-btn").click()
    select = page.locator("#model-select")
    expect(select).to_have_value(MODELS[0]["slug"])
    auto = select.locator('option[value="auto"]')
    expect(auto).to_contain_text("terra")
    expect(auto).to_contain_text("astra")
    expect(auto).not_to_contain_text("sol")
    select.select_option("auto")
    expect(select).to_have_value("auto")
    page.close()


@pytest.mark.parametrize("width", [320, 769, 1280])
def test_model_metrics_page_is_responsive_without_script_errors(browser, gui_url, width):
    from playwright.sync_api import expect
    url, _ = gui_url
    page = browser.new_page(viewport={"width": width, "height": 844})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(url + "/model-performance")
    expect(page.locator("#model-perf-rows")).to_contain_text("No attributed attempts yet.")
    no_overflow(page)
    assert errors == []
    page.close()
