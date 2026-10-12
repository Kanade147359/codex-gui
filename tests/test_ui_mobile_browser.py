"""Real Chromium UI + HTTP/SQLite with fake Codex, isolated data and repositories.

CODEX_GUI_BROWSER=1 pytest tests/test_ui_mobile_browser.py
Requires playwright and `playwright install chromium`; no production server is used.
"""
import os
import time

import pytest

from app.logstore import TaskLog
from test_attachments import image_bytes
from test_ui_attachments_browser import gui_url  # noqa: F401 (shared isolated server fixture)

pytestmark = pytest.mark.skipif(os.environ.get("CODEX_GUI_BROWSER") != "1", reason="opt-in Chromium check")


@pytest.fixture
def browser():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        instance = p.chromium.launch()
        yield instance
        instance.close()


def no_overflow(page):
    # Inspect the actual document and any open modal; local code/table scrollers are intentional.
    result = page.evaluate("""() => {
      const w = document.documentElement.clientWidth;
      const dialog = document.querySelector('dialog[open]');
      return {width:w, document:document.documentElement.scrollWidth,
        body:document.body.scrollWidth, dialog:dialog ? [dialog.clientWidth, dialog.scrollWidth] : null,
        offenders: document.documentElement.scrollWidth > w + 1 ? [...document.querySelectorAll('main > *, dl, table')]
          .filter(el => el.getBoundingClientRect().right > w + 1 || el.scrollWidth > el.clientWidth + 1)
          .slice(0, 10).map(el => [el.tagName, el.id, el.className, el.clientWidth, el.scrollWidth]) : []};
    }""")
    assert result["document"] <= result["width"] + 1, str(result)
    assert result["body"] <= result["width"] + 1, result
    if result["dialog"]:
        assert result["dialog"][1] <= result["dialog"][0] + 1, result


def state(page, tid, status):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        task = page.request.get(f"/api/tasks/{tid}").json()
        if task["status"] == status and (status != "running" or task["codex_thread_id"]):
            return task
        time.sleep(.05)
    raise AssertionError(f"{tid}: expected {status}, got {task['status']}")


def create(page, repo, **fields):
    response = page.request.post("/api/tasks", data={"repository": str(repo), "prompt": "ok", **fields})
    assert response.ok, response.text()
    return response.json()["id"]


@pytest.mark.parametrize("width", [320, 375, 390, 430, 768, 769, 1280])
def test_viewports_navigation_create_attach_continue_schedule_settings(browser, gui_url, git_repo, width, tmp_path):
    from playwright.sync_api import expect
    url, app = gui_url
    mobile = width <= 768
    context = browser.new_context(base_url=url, viewport={"width": width, "height": 844}, is_mobile=mobile, has_touch=mobile)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(url)
    expect(page.locator(".mobile-nav")).to_be_visible() if mobile else expect(page.locator(".mobile-nav")).not_to_be_visible()
    no_overflow(page)
    if mobile:
        for tab in ("queue", "logs", "settings", "tasks"):
            page.locator(f'[data-mobile-tab="{tab}"]').click()
            expect(page.locator(f'[data-mobile-tab="{tab}"]')).to_have_attribute("aria-current", "page")
            no_overflow(page)
    page.locator("#new-task-btn").click()
    page.locator('[name="repository"]').fill(str(git_repo))
    page.locator('[name="name"]').fill("スマートフォン確認 " + "long-name-" * 12)
    page.locator('[name="prompt"]').fill("ok 日本語で実行")
    picker = page.locator("#new-task-attachments input[type=file]")
    picker.set_input_files([
        {"name": "写真.png", "mimeType": "image/png", "buffer": image_bytes()},
        {"name": "参考.jpg", "mimeType": "image/jpeg", "buffer": image_bytes("JPEG")},
    ])
    expect(page.locator("#new-task-attachments .attachment-card")).to_have_count(2)
    page.wait_for_function("!document.querySelector('#new-task-attachments').textContent.includes('Uploading…')")
    no_overflow(page)
    page.locator("#run-btn").click()
    expect(page.locator("#new-task-dialog")).not_to_be_visible()
    if mobile:
        card = page.locator("#task-cards .task-card").first
        expect(card).to_contain_text("Service tier")
        expect(card).to_contain_text("Dependencies")
        expect(card).to_contain_text("Updated")
        expect(card).to_contain_text("Standard")
        no_overflow(page)
        page.screenshot(path=str(tmp_path / f"tasks-{width}.png"))
        card.locator("summary").click()
        expect(card.locator(".card-menu")).to_be_visible()
        card.locator("summary").click()
        card.locator(".task-card-link").click()
    else:
        expect(page.locator("#tasks-table")).to_be_visible()
        expect(page.locator("#task-cards")).not_to_be_visible()
        page.locator('#tasks-body a[href^="/tasks/"]').first.click()
    page.wait_for_url("**/tasks/*")
    tid = page.url.split("/")[-1]
    initial = state(page, tid, "completed")
    page.wait_for_function("!document.querySelector('#send-btn').disabled")
    if not mobile:
        expect(page.locator('#instruction-composer a[href="#task-controls"]')).not_to_be_visible()
    page.locator("#image-message-history").evaluate("el => el.closest('details').open = true")
    expect(page.locator("#image-message-history .image-preview")).to_have_count(2)
    page.locator("details.fold").evaluate_all("els => els.forEach(el => el.open = true)")
    no_overflow(page)
    page.screenshot(path=str(tmp_path / f"task-detail-{width}.png"))
    page.locator("#image-message-history .image-preview").first.click()
    expect(page.locator("dialog.image-lightbox")).to_be_visible()
    no_overflow(page)
    page.locator("dialog.image-lightbox button").click()
    # Exercise continuation through the real endpoint, preserving the thread and worktree.
    page.locator("#instruction").fill("ok 継続")
    page.locator("#instruction-attachments input[type=file]").set_input_files(
        {"name": "継続.webp", "mimeType": "image/webp", "buffer": image_bytes("WEBP")})
    page.wait_for_function("!document.querySelector('#instruction-attachments').textContent.includes('Uploading…')")
    if mobile:
        # Chromium does not show an OS keyboard in CI. Verify the resizes-content viewport path.
        page.set_viewport_size({"width": width, "height": 420})
        page.locator("#instruction").focus()
        page.wait_for_function("document.body.classList.contains('keyboard-open')")
        send = page.locator("#send-btn").bounding_box()
        assert 0 <= send["y"] and send["y"] + send["height"] <= 420
        no_overflow(page)
    with page.expect_response(f"**/api/tasks/{tid}/messages") as sent:
        page.locator("#send-btn").click()
    assert sent.value.ok, sent.value.text()
    expect(page.locator("#instruction")).to_have_value("")
    after = state(page, tid, "completed")
    assert after["codex_thread_id"] == initial["codex_thread_id"]
    assert after["worktree"] == initial["worktree"]
    assert len(app.state.manager.db.list_messages(tid)) == 2
    assert app.state.manager.runner.sent[-1][1] == initial["codex_thread_id"]
    page.set_viewport_size({"width": width, "height": 844})
    page.locator("#instruction").blur()
    # Reserve an instruction against an actual unfinished dependency, then cancel it.
    blocker = create(page, git_repo, prompt="sleep", name="依存タスク")
    state(page, blocker, "running")
    page.reload()  # reload also refreshes the dependency picker immediately
    page.locator("#instruction").fill("ok 予約指示")
    page.locator("#delivery-after").check()
    page.locator(f'#sched-deps input[value="{blocker}"]').check()
    page.locator("#schedule-btn").click()
    expect(page.locator("#scheduled-items")).to_contain_text("依存タスク")
    expect(page.locator("#scheduled-items")).to_contain_text("WAITING")
    no_overflow(page)
    if mobile:
        page.locator('[data-mobile-tab="queue"]').click()
        expect(page.locator("#mobile-queue")).to_contain_text("予約指示")
        expect(page.locator("#mobile-queue")).to_contain_text("Scheduled time")
        expect(page.locator("#mobile-queue")).to_contain_text("依存タスク")
        no_overflow(page)
        page.locator("#mobile-queue a.button").last.click()
        expect(page.locator("#scheduled-items")).to_contain_text("予約指示")
    page.once("dialog", lambda d: d.accept())
    page.locator("#scheduled-items .sched-cancel").click()
    expect(page.locator("#scheduled-items .sched-cancel")).to_have_count(0)
    assert page.request.post(f"/api/tasks/{blocker}/stop").ok
    page.goto(url + ("/#settings" if mobile else "/"))
    page.locator("#ctx-settings-btn").click()
    expect(page.locator("#ctx-settings-dialog")).to_be_visible()
    no_overflow(page)
    page.locator("#ctx-settings-save").click()
    expect(page.locator("#ctx-settings-msg")).to_contain_text("Saved")
    page.locator("#ctx-settings-close").click()
    assert errors == []
    page.screenshot(path=str(tmp_path / f"viewport-{width}.png"))
    context.close()


def test_mobile_stop_resume_retry_approval_dependencies_logs_and_breakpoint(browser, gui_url, git_repo):
    from playwright.sync_api import expect
    url, app = gui_url
    page = browser.new_page(base_url=url, viewport={"width": 320, "height": 844}, has_touch=True)
    page.goto(url)
    tid = create(page, git_repo, prompt="sleep", auto_retry=False)
    initial = state(page, tid, "running")
    expect(page.locator(f'#task-cards [data-task-card="{tid}"]')).to_be_visible()
    expect(page.locator(f'#task-cards [data-task-card="{tid}"] progress')).to_be_visible()
    page.locator("#new-task-btn").click()
    page.locator('[name="repository"]').fill(str(git_repo))
    page.locator('[name="name"]').fill("依存待ち")
    page.locator('[name="prompt"]').fill("ok 依存待ち")
    page.locator('[name="run_mode"][value="after"]').check()
    page.locator(f'#deps-list input[value="{tid}"]').check()
    with page.expect_response("**/api/tasks") as created:
        page.locator("#run-btn").click()
    assert created.value.ok
    dependent = created.value.json()["id"]
    assert state(page, dependent, "waiting_dependencies")["dependencies"][0]["id"] == tid
    page.goto(f"{url}/tasks/{tid}")
    page.once("dialog", lambda d: d.dismiss())
    page.locator("#stop-btn").click()
    assert state(page, tid, "running")
    page.once("dialog", lambda d: d.accept())
    page.locator("#stop-btn").click()
    state(page, tid, "stopped")
    page.goto(f"{url}/tasks/{dependent}")
    expect(page.locator("#deps-items")).to_contain_text("stopped")
    no_overflow(page)
    # Simulate a recovered, interrupted thread in the isolated database.
    app.state.manager.db._execute("UPDATE tasks SET status = 'interrupted' WHERE id = ?", (tid,))
    page.goto(f"{url}/tasks/{tid}")
    page.locator("#resume-btn").click()
    resumed = state(page, tid, "completed")
    assert resumed["codex_thread_id"] == initial["codex_thread_id"]
    assert resumed["worktree"] == initial["worktree"]
    failed = create(page, git_repo, prompt="err 1 transient failure", auto_retry=False)
    failed_task = state(page, failed, "failed")
    page.goto(f"{url}/tasks/{failed}")
    page.locator("#recovery-section").evaluate("el => el.open = true")
    page.locator("#retry-btn").click()
    state(page, failed, "completed")  # recovery uses the existing thread's recovery instruction
    assert app.state.manager.runner.sent[-1][1] == failed_task["codex_thread_id"]
    review = create(page, git_repo, completion_contract={"manual_approval": True})
    state(page, review, "completed")
    page.goto(f"{url}/tasks/{review}")
    page.on("dialog", lambda d: d.accept("mobile review") if d.type == "prompt" else d.accept())
    page.locator("#completion-success-btn").click()
    expect(page.locator("#task-status")).to_contain_text("SUCCESS")
    no_overflow(page)
    # Real log polling: prose wraps, command lines scroll locally, follow stops at older entries.
    with_log = app.state.manager.log_path(review)
    initial_lines = len(page.request.get(f"/api/tasks/{review}/log").json()["entries"])
    log = TaskLog(with_log)
    for i in range(100):
        log.add_system(f"line {i}: " + "長い説明" * 40)
    log.add_event("item/completed/commandExecution", "$ echo " + "a/very/long/path/" * 40, {})
    log.close()
    page.goto(f"{url}/tasks/{review}#logs")
    expect(page.locator("#log .entry")).to_have_count(initial_lines + 101)
    assert page.locator("#log .entry.literal .msg").evaluate("el => el.scrollWidth > el.clientWidth")
    assert page.locator("#log .entry.system .msg").evaluate_all("els => els.every(el => el.scrollWidth <= el.clientWidth + 1)")
    page.wait_for_function("document.querySelector('#log').scrollTop > 0")
    no_overflow(page)
    page.locator("#log").evaluate("el => el.scrollTop = 0")
    expect(page.locator("#log-latest")).to_be_visible()
    TaskLog.note(with_log, "after scrolling up")
    expect(page.locator("#log")).to_contain_text("after scrolling up")
    assert page.locator("#log").evaluate("el => el.scrollTop") == 0
    page.locator("#log-latest").click()
    expect(page.locator("#log-latest")).not_to_be_visible()
    TaskLog.note(with_log, "following latest")
    expect(page.locator("#log")).to_contain_text("following latest")
    page.wait_for_function("(()=>{const el=document.querySelector('#log');return el.scrollTop+el.clientHeight>=el.scrollHeight-2})()")
    # Restore original control positions on desktop, including their click handlers.
    page.set_viewport_size({"width": 1280, "height": 900})
    expect(page.locator(".title-row #del-wt-btn")).to_be_visible()
    expect(page.locator("#instruction-composer #new-session-btn")).to_be_visible()
    expect(page.locator(".mobile-nav")).not_to_be_visible()
    no_overflow(page)
    page.close()


@pytest.mark.parametrize("width", [320, 390, 768])
def test_touch_graph_pan_pinch_select(browser, gui_url, git_repo, width):
    from playwright.sync_api import expect
    url, _ = gui_url
    context = browser.new_context(base_url=url, viewport={"width": width, "height": 844}, has_touch=True, is_mobile=True)
    page = context.new_page()
    page.goto(url)
    parent = create(page, git_repo, prompt="sleep", name="前提")
    state(page, parent, "running")
    create(page, git_repo, depends_on=[parent], name="後続")
    page.goto(f"{url}/dependencies")
    expect(page.locator(".graph-node")).to_have_count(2)
    expect(page.locator(".graph-edge")).to_have_count(1)
    no_overflow(page)
    rect = page.locator("#graph-viewport").bounding_box()
    x, y = rect["x"] + rect["width"] / 2, rect["y"] + 80
    cdp = context.new_cdp_session(page)
    transform = lambda: page.locator("#graph-world").get_attribute("transform")
    before = transform()
    initial_scale = float(before.split("scale(")[1].rstrip(")"))
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y, "id": 0}]})
    cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x + 40, "y": y + 30, "id": 0}]})
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    assert transform() != before
    assert float(transform().split("scale(")[1].rstrip(")")) == initial_scale
    before = transform()
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [
        {"x": x - 20, "y": y, "id": 0}, {"x": x + 20, "y": y, "id": 1}]})
    cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [
        {"x": x - 60, "y": y, "id": 0}, {"x": x + 60, "y": y, "id": 1}]})
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    assert transform() != before
    assert float(transform().split("scale(")[1].rstrip(")")) > initial_scale * 1.5
    assert page.locator("#graph-detail").is_hidden()  # gestures must not select a node
    page.locator("#graph-fit").click()
    page.locator(".graph-node .graph-card").first.tap()
    expect(page.locator("#graph-detail")).to_be_visible()
    expect(page.locator("#graph-detail")).to_contain_text("タスク詳細へ")
    no_overflow(page)
    page.locator("#graph-detail-close").click()
    expect(page.locator("#graph-detail")).not_to_be_visible()
    assert page.request.post(f"/api/tasks/{parent}/stop").ok
    context.close()
