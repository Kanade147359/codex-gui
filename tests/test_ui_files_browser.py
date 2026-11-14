"""PC/mobile browser flow: upload -> reading subprocess -> download -> A to B -> resumed instruction.

CODEX_GUI_BROWSER=1 PLAYWRIGHT_BROWSERS_PATH=... pytest tests/test_ui_files_browser.py
"""
import base64
import hashlib
import io
import os
import shutil
from pathlib import Path
import socket
import threading
import time
import zipfile
from urllib.parse import urlsplit

import pytest
import uvicorn

from app.main import create_app
from conftest import FakeAppServer
from test_files import ReadingRunner
from test_ui_mobile_browser import no_overflow

pytestmark = pytest.mark.skipif(os.environ.get('CODEX_GUI_BROWSER') != '1', reason='opt-in Chromium E2E')


@pytest.fixture
def files_gui_url(settings, git_repo, tmp_path, monkeypatch, request):
    if os.environ.get('CODEX_GUI_REAL_FILES_BROWSER') == '1':
        auth = Path(os.environ.get('CODEX_HOME', '~/.codex')).expanduser()/'auth.json'
        if not auth.is_file(): pytest.skip('No subscription auth for isolated browser acceptance')
        home = tmp_path/'isolated-codex'; home.mkdir(mode=0o700)
        request.addfinalizer(lambda:shutil.rmtree(home, ignore_errors=True))
        shutil.copy2(auth,home/'auth.json')
        cache=auth.parent/'models_cache.json'
        if cache.is_file(): shutil.copy2(cache,home/'models_cache.json')
        monkeypatch.setenv('CODEX_HOME',str(home))
        settings.backend='app-server'
        app = create_app(settings)
    else:
        app = create_app(settings, ReadingRunner(), FakeAppServer('fake'))
    app.state.catalog._cached = dict(models=[], default_model='', default_effort='low', recommended_model='', error='')
    app.state.catalog._fetched_at = time.monotonic()
    app.state.manager.db.touch_repo(str(git_repo), 'now')
    sock = socket.socket(); sock.bind(('127.0.0.1',0))
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=sock.getsockname()[1], log_level='error'))
    thread = threading.Thread(target=server.run, kwargs={'sockets':[sock]}, daemon=True); thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline: time.sleep(.01)
    assert server.started
    yield f'http://127.0.0.1:{sock.getsockname()[1]}', app
    server.should_exit = True; thread.join(10); sock.close()
    assert not thread.is_alive()


def idle(page):
    page.wait_for_function('document.querySelector("#task-status")?.classList.contains("success")', timeout=240000)


def run_new(page, playwright):
    page.locator('[name="prompt"]').fill('Use a shell tool to read the attached local input file and copy its exact bytes to verification.json in the current worktree. Do not copy other files. Return the required SUCCESS JSON after checking the file.')
    page.locator('#run-btn').click()
    playwright.expect(page.locator('#new-task-dialog')).not_to_be_visible()
    page.wait_for_function('document.querySelector("#tasks-body a[href^=\\"/tasks/\\"]")')
    page.locator('#tasks-body a[href^="/tasks/"]:visible, #task-cards .task-card-link:visible').first.click()
    idle(page)
    return page.url.split('/tasks/')[1].split('#')[0]


@pytest.mark.parametrize('viewport', [{'width':1280,'height':900}, {'width':390,'height':844}], ids=['desktop','mobile'])
def test_upload_download_preview_zip_artifact_transfer_and_resume(files_gui_url, tmp_path, viewport):
    playwright = pytest.importorskip('playwright.sync_api')
    url, app = files_gui_url
    data = b'{"verified":true,"display":"<script>unsafe()</script>"}'
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(); page = browser.new_page(viewport=viewport, accept_downloads=True)
        errors=[]; page.on('pageerror', lambda e:errors.append(str(e)))
        page.goto(url); page.locator('#new-task-btn').click()
        picker = page.locator('#new-task-attachments input[type=file]')
        # Server rejection preserves the selected file; Retry succeeds without reselecting.
        page.route('**/api/uploads?*', lambda route:route.fulfill(status=413, json={'detail':{'message':'test size limit'}}), times=1)
        picker.set_input_files({'name':'verification-input.json','mimeType':'application/json','buffer':data})
        playwright.expect(page.locator('#new-task-attachments')).to_contain_text('test size limit')
        page.locator('#new-task-attachments [data-retry-image]').click()
        page.wait_for_function('!document.querySelector("#new-task-attachments").textContent.includes("Uploading") && !document.querySelector("#new-task-attachments").textContent.includes("test size limit")')
        # Generic drag/drop, separately from the existing image clipboard flow.
        page.locator('[name="prompt"]').evaluate('''(el, encoded) => {
          const d = new DataTransfer(); d.items.add(new File([Uint8Array.from(atob(encoded), c=>c.charCodeAt(0))], "dropped.bin", {type:"application/octet-stream"}));
          el.dispatchEvent(new DragEvent("drop", {dataTransfer:d,bubbles:true,cancelable:true}));
        }''', base64.b64encode(data).decode())
        playwright.expect(page.locator('#new-task-attachments .attachment-card')).to_have_count(2)
        page.wait_for_function('!document.querySelector("#new-task-attachments").textContent.includes("Uploading")')
        no_overflow(page)
        source = run_new(page, playwright)
        page.locator('#git-tabs [data-tab=files]').click()
        row = page.locator('.task-file-row[data-path="verification.json"]')
        playwright.expect(row).to_be_visible()
        row.locator('[data-preview]').click()
        playwright.expect(page.locator('.file-preview')).to_have_text(data.decode())
        assert page.locator('.file-dialog script').count()==0
        page.locator('.file-dialog [data-close]').click()
        # Failed download is visible and the page/log remains usable.
        page.route('**/files/download?*', lambda route:route.fulfill(status=500, json={'detail':{'message':'test download failure'}}), times=1)
        row.locator('[data-download]').click()
        playwright.expect(page.locator('#task-files .file-action-error')).to_contain_text('test download failure')
        with page.expect_download() as download:
            row.locator('[data-download]').click()
        assert hashlib.sha256(Path(download.value.path()).read_bytes()).digest()==hashlib.sha256(data).digest()
        row.locator('.file-check').check()
        page.locator('.task-file-row[data-path="README.md"] .file-check').check()
        with page.expect_download() as zipped:
            page.locator('#files-zip').click()
        with zipfile.ZipFile(zipped.value.path()) as z:
            assert set(z.namelist())=={'verification.json','README.md'}
            assert z.read('verification.json')==data
        no_overflow(page)
        page.screenshot(path=str(tmp_path/f'files-{viewport["width"]}.png'), full_page=True)
        # Source -> new target must use server-side transfer, without a browser byte round trip.
        transfer_requests = []
        def record_transfer(request):
            transfer_requests.append((request.method, urlsplit(request.url).path))
        page.on('request', record_transfer)
        row.locator('[data-send]').click()
        playwright.expect(page.locator('.transfer-target')).to_be_visible()
        no_overflow(page)
        page.locator('.transfer-confirm').click()
        page.wait_for_url('**/?attach_artifacts=1')
        playwright.expect(page.locator('#new-task-dialog')).to_be_visible()
        playwright.expect(page.locator('#new-task-attachments .attachment-card')).to_have_count(1)
        target = run_new(page, playwright)
        page.remove_listener('request', record_transfer)
        assert ('POST', f'/api/tasks/{source}/artifacts') in transfer_requests
        assert ('POST', '/api/artifacts/inputs') in transfer_requests
        assert not any(method == 'POST' and path in ('/api/uploads', '/api/attachments')
                       or method == 'GET' and (path.endswith('/download') or '/inputs/' in path)
                       for method, path in transfer_requests), transfer_requests
        assert target!=source
        result = app.state.manager.get(target)
        assert (Path(result['worktree'])/'verification.json').read_bytes()==data
        thread = result['codex_thread_id']
        # Attach from Task in an existing thread's Additional Instruction.
        transfer_requests.clear()
        page.on('request', record_transfer)
        page.locator('#instruction-attachments .attach-from-task').click()
        page.locator('.artifact-source').select_option(source)
        choice = page.locator('.artifact-options [data-current][value="verification.json"]')
        playwright.expect(choice).to_be_visible(); choice.check()
        no_overflow(page)
        page.locator('.artifact-attach').click()
        playwright.expect(page.locator('.file-dialog')).to_have_count(0)
        page.locator('#instruction').fill('Read the immutable snapshot again')
        page.locator('#send-btn').click()
        page.wait_for_function('!document.querySelector("#instruction").value')
        idle(page)
        page.remove_listener('request', record_transfer)
        assert ('POST', '/api/artifacts/inputs') in transfer_requests
        assert not any(method == 'POST' and path in ('/api/uploads', '/api/attachments')
                       or method == 'GET' and (path.endswith('/download') or '/inputs/' in path)
                       for method, path in transfer_requests), transfer_requests
        assert app.state.manager.get(target)['codex_thread_id']==thread
        assert len(app.state.manager.db.list_messages(target))==2
        assert (Path(result['worktree'])/'verification.json').read_bytes()==data
        assert not errors, errors
        browser.close()


@pytest.mark.skipif(os.environ.get('CODEX_GUI_REAL_FILES_BROWSER')!='1',reason='opt-in real-model browser acceptance')
@pytest.mark.parametrize('viewport',[{'width':1280,'height':900},{'width':390,'height':844}],ids=['real-desktop','real-mobile'])
def test_real_browser_upload_model_read_transfer_and_download(files_gui_url,tmp_path,viewport):
    playwright=pytest.importorskip('playwright.sync_api')
    url,app=files_gui_url
    import uuid
    data=('{"verification":"'+uuid.uuid4().hex+'"}').encode()
    with playwright.sync_playwright() as p:
        browser=p.chromium.launch(); page=browser.new_page(viewport=viewport,accept_downloads=True)
        errors=[]; page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(url); page.locator('#new-task-btn').click()
        page.locator('#new-task-attachments input[type=file]').set_input_files({'name':'input.json','mimeType':'application/json','buffer':data})
        page.wait_for_function('!document.querySelector("#new-task-attachments").textContent.includes("Uploading")')
        source=run_new(page,playwright)
        page.locator('#git-tabs [data-tab=files]').click()
        row=page.locator('.task-file-row[data-path="verification.json"]')
        playwright.expect(row).to_be_visible(timeout=10000)
        with page.expect_download() as original:
            row.locator('[data-download]').click()
        assert Path(original.value.path()).read_bytes()==data
        row.locator('[data-send]').click(); page.locator('.transfer-confirm').click()
        page.wait_for_url('**/?attach_artifacts=1')
        playwright.expect(page.locator('#new-task-dialog')).to_be_visible()
        target=run_new(page,playwright)
        assert source!=target
        page.locator('#git-tabs [data-tab=files]').click()
        result=page.locator('.task-file-row[data-path="verification.json"]')
        playwright.expect(result).to_be_visible(timeout=10000)
        with page.expect_download() as copied:
            result.locator('[data-download]').click()
        assert hashlib.sha256(Path(copied.value.path()).read_bytes()).digest()==hashlib.sha256(data).digest()
        no_overflow(page)
        page.screenshot(path=str(tmp_path/f'real-files-{viewport["width"]}.png'),full_page=True)
        assert app.state.manager.get(target)['task_outcome']=='success'
        assert not errors,errors
        browser.close()
