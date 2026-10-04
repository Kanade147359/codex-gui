"""Optional Chromium end-to-end check using real uploads, temporary GUI data and fake Codex.

pip install playwright; playwright install chromium
CODEX_GUI_BROWSER=1 pytest tests/test_ui_attachments_browser.py
"""
import base64
import os
import socket
import threading
import time

import pytest
import uvicorn

from app.main import create_app
from conftest import FakeAppServer
from test_attachments import ImageRunner, image_bytes

pytestmark = pytest.mark.skipif(os.environ.get("CODEX_GUI_BROWSER") != "1", reason="opt-in Playwright browser check")


@pytest.fixture
def gui_url(settings, git_repo):
    # The dashboard reads account/limits even with the exec backend. Keep that client fake too.
    app = create_app(settings, ImageRunner(), FakeAppServer("fake", settings.subscription_only))
    app.state.catalog._cached = dict(models=[], default_model="", default_effort="low", recommended_model="", error="")
    app.state.catalog._fetched_at = time.monotonic()
    app.state.manager.db.touch_repo(str(git_repo), "now")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(.01)
    assert server.started
    yield f"http://127.0.0.1:{port}", app
    server.should_exit = True
    thread.join(10)
    sock.close()
    assert not thread.is_alive()


def paste_image(page, selector, data):
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    page.evaluate("""async encoded => {
      const data = Uint8Array.from(atob(encoded), c => c.charCodeAt(0));
      await navigator.clipboard.write([new ClipboardItem({'image/png': new Blob([data], {type: 'image/png'})})]);
    }""", base64.b64encode(data).decode())
    page.locator(selector).focus()
    page.keyboard.press("Control+V")


def drop_image(page, selector, data):
    page.locator(selector).evaluate("""(el, encoded) => {
      const transfer = new DataTransfer();
      transfer.items.add(new File([Uint8Array.from(atob(encoded), c => c.charCodeAt(0))], 'ドロップ.webp', {type: 'image/webp'}));
      el.dispatchEvent(new DragEvent('drop', {dataTransfer: transfer, bubbles: true, cancelable: true}));
    }""", base64.b64encode(data).decode())


def test_composers_paste_drop_select_remove_enlarge_history_and_failure(gui_url, tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    url, app = gui_url
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(url)
        page.locator("#new-task-btn").click()
        text = page.locator('[name="prompt"]')
        text.fill("ok 日本語の指示を保持")
        # Plain clipboard text keeps native paste behaviour; no keyboard/IME handlers are installed.
        assert text.evaluate("""el => { const d = new DataTransfer(); d.setData('text/plain', '通常のテキスト');
          return el.dispatchEvent(new ClipboardEvent('paste', {clipboardData:d, cancelable:true, bubbles:true})); }""")
        page.context.grant_permissions(["clipboard-read", "clipboard-write"])
        page.evaluate("navigator.clipboard.writeText('通常テキスト')")
        text.focus()
        page.keyboard.press("End")
        page.keyboard.press("Control+V")
        playwright.expect(text).to_have_value("ok 日本語の指示を保持通常テキスト")
        text.fill("ok 日本語の指示を保持")
        text.dispatch_event("compositionstart")
        text.dispatch_event("compositionend", {"data": "日本語"})
        assert text.input_value() == "ok 日本語の指示を保持"
        paste_image(page, '[name="prompt"]', image_bytes())
        drop_image(page, '[name="prompt"]', image_bytes("WEBP"))
        picker = page.locator("#new-task-attachments input[type=file]")
        picker.set_input_files({"name": "選択.jpg", "mimeType": "image/jpeg", "buffer": image_bytes("JPEG")})
        cards = page.locator("#new-task-attachments .attachment-card")
        playwright.expect(cards).to_have_count(3)
        page.wait_for_function("!document.querySelector('#new-task-attachments').textContent.includes('Uploading…')")
        cards.nth(1).locator("[data-remove-image]").click()
        playwright.expect(cards).to_have_count(2)
        cards.first.locator(".image-preview").click()
        playwright.expect(page.locator("dialog.image-lightbox")).to_be_visible()
        page.keyboard.press("Escape")
        playwright.expect(page.locator("dialog.image-lightbox")).to_have_count(0)
        page.screenshot(path=str(tmp_path / "image-composer.png"))
        page.locator("#run-btn").click()
        playwright.expect(page.locator("#new-task-dialog")).not_to_be_visible()
        page.wait_for_function("document.querySelector('#tasks-body a[href^=\"/tasks/\"]')")
        page.locator('#tasks-body a[href^="/tasks/"]').first.click()
        page.wait_for_function("document.querySelector('#send-btn').disabled === false")
        playwright.expect(page.locator("#image-message-history .image-preview")).to_have_count(2)
        assert page.locator("#image-message-history").text_content().find("ok 日本語の指示を保持") >= 0
        # Follow-up composer also accepts all three input routes, in order.
        page.locator("#instruction").fill("ok")
        paste_image(page, "#instruction", image_bytes())
        drop_image(page, "#instruction", image_bytes("WEBP"))
        page.locator("#instruction-attachments input[type=file]").set_input_files(
            {"name": "継続.jpg", "mimeType": "image/jpeg", "buffer": image_bytes("JPEG")})
        page.wait_for_function("document.querySelectorAll('#instruction-attachments .attachment-card').length === 3 && !document.querySelector('#instruction-attachments').textContent.includes('Uploading…')")
        # Simulated HTTP rejection: both text and attachments survive.
        page.route("**/api/tasks/*/messages", lambda route: route.fulfill(status=400, json={"detail": {"message": "test rejection"}}))
        page.locator("#send-btn").click()
        playwright.expect(page.locator("#action-msg")).to_contain_text("test rejection")
        assert page.locator("#instruction").input_value() == "ok"
        playwright.expect(page.locator("#instruction-attachments .attachment-card")).to_have_count(3)
        page.unroute("**/api/tasks/*/messages")
        # LocalStorage restores the uploaded draft on page reload.
        page.reload()
        playwright.expect(page.locator("#instruction-attachments .attachment-card")).to_have_count(3)
        assert page.locator("#instruction").input_value() == "ok"
        page.wait_for_function("document.querySelector('#send-btn').disabled === false")
        page.locator("#send-btn").click()
        playwright.expect(page.locator("#instruction-attachments .attachment-card")).to_have_count(0)
        page.wait_for_function("document.querySelectorAll('#image-message-history .image-preview').length === 5")
        tid = page.url.split("/")[-1]
        history = app.state.manager.db.list_messages(tid)
        assert [len(m["attachment_ids"]) for m in history] == [2, 3]
        assert app.state.manager.runner.sent[1][1] == app.state.manager.db.get_task(tid)["codex_thread_id"]
        assert errors == []
        page.locator("#image-message-history").evaluate("el => el.closest('details').open = true")
        page.screenshot(path=str(tmp_path / "image-history.png"), full_page=True)
        browser.close()
