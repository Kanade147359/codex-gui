"""Real SQLite, Git, streaming HTTP and file-reading subprocess; all data is temporary."""
import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.attachments import AttachmentError
from app.database import Database, SCHEMA
from app.main import create_app
from app.task_manager import TaskManager, _Turn
from conftest import FakeAppServer, FakeRunner, wait_for
from test_api import wait_done


class ReadingRunner(FakeRunner):
    def build_command(self, task, resume_thread=None):
        return [sys.executable, str(Path(__file__).with_name('fake_files_codex.py'))] + (['resume', resume_thread] if resume_thread else [])


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings, ReadingRunner(), FakeAppServer('fake'))) as c:
        yield c


def upload(c, data=b'{"verified": true}', name='日本語 verification.json'):
    r = c.post('/api/uploads', params={'filename': name}, content=data)
    assert r.status_code == 200, r.text
    return r.json()


def task(c, repo, **kw):
    r = c.post('/api/tasks', json={'repository': str(repo), 'prompt': 'Read attached files', **kw})
    assert r.status_code == 200, r.text
    return r.json()['id']


def test_new_followup_scheduled_read_download_zip_and_restart(settings, git_repo):
    data = bytes(range(256)) * 120
    with TestClient(create_app(settings, ReadingRunner(), FakeAppServer('fake'))) as c:
        first = upload(c, data, 'C:\\unsafe\\../バイナリ.zip')
        assert first['filename'] == 'バイナリ.zip'
        tid = task(c, git_repo, attachment_ids=[first['id']])
        row = wait_done(c, tid)
        assert row['task_outcome'] == 'success'
        thread = row['codex_thread_id']
        store = c.app.state.manager.attachments
        input_path = store.path(store.get(first['id']))
        assert not input_path.is_relative_to(row['worktree'])
        assert input_path.read_bytes() == data
        assert (Path(row['worktree']) / 'received-0.bin').read_bytes() == data
        assert c.get(f'/api/attachments/{first["id"]}').status_code == 404
        response = c.get(f'/api/tasks/{tid}/files/download', params={'path':'received-0.bin'})
        assert response.headers['x-content-type-options'] == 'nosniff'
        assert response.headers['cache-control'] == 'no-store'
        assert hashlib.sha256(response.content).hexdigest() == first['sha256']
        zipped = c.post(f'/api/tasks/{tid}/files/zip', json={'paths':['received-0.bin', 'README.md']})
        assert zipped.status_code == 200, zipped.text
        with zipfile.ZipFile(io.BytesIO(zipped.content)) as z:
            assert z.namelist() == ['received-0.bin', 'README.md']
            assert z.read('received-0.bin') == data
            assert z.read('README.md') == (git_repo / 'README.md').read_bytes()
        second = upload(c, b'{"followup":true}')
        assert c.post(f'/api/tasks/{tid}/messages', json={'prompt':'Read this', 'attachment_ids':[second['id']]}).status_code == 200
        assert wait_done(c, tid)['codex_thread_id'] == thread
        third = upload(c, b'scheduled')
        assert c.post(f'/api/tasks/{tid}/scheduled', json={'prompt':'Read scheduled', 'attachment_ids':[third['id']]}).status_code == 200
        wait_done(c, tid)
        assert (Path(row['worktree']) / 'verification.json').read_bytes() == b'scheduled'
        history = c.get(f'/api/tasks/{tid}').json()['messages']
        assert len(history) == 3
        assert [m['attachment_ids'] for m in history] == [[first['id']], [second['id']], [third['id']]]
        assert c.get(f'/api/tasks/{tid}/inputs/{first["id"]}').content == data
    with TestClient(create_app(settings, ReadingRunner(), FakeAppServer('fake'))) as c:
        assert c.get(f'/api/tasks/{tid}').json()['messages'] == history
        assert c.get(f'/api/tasks/{tid}/inputs/{first["id"]}').content == data
        assert c.post(f'/api/tasks/{tid}/messages', json={'prompt':'Read again', 'attachment_ids':[first['id']]}).status_code == 200
        assert wait_done(c, tid)['codex_thread_id'] == thread


def test_task_isolation_untrusted_ids_and_metadata(client, git_repo):
    a = upload(client)
    tid = task(client, git_repo, attachment_ids=[a['id']])
    wait_done(client, tid)
    other = task(client, git_repo)
    wait_done(client, other)
    assert client.get(f'/api/tasks/{other}/inputs/{a["id"]}').status_code == 404
    assert client.post(f'/api/tasks/{other}/messages', json={'prompt':'read', 'attachment_ids':[a['id']]}).status_code == 400
    assert client.post(f'/api/tasks/{other}/inputs', json={'attachment_ids':[a['id']]}).status_code == 400
    for ids in ([a['id']] * 2, ['../../etc/passwd'], ['0' * 32]):
        assert client.post('/api/tasks', json={'repository':str(git_repo), 'prompt':'read', 'attachment_ids':ids}).status_code == 400
    original = client.app.state.manager.db.get_task(tid)['worktree']
    client.app.state.manager.db.update_task(tid, worktree=client.app.state.manager.get(other)['worktree'])
    assert client.get(f'/api/tasks/{tid}/files').status_code == 403
    client.app.state.manager.db.update_task(tid, worktree=original)


@pytest.mark.parametrize('path', ['../README.md', '/etc/passwd', 'a/../../etc/passwd', 'a\\b', '.git/config', '.env', 'id_rsa', 'secrets.json', 'key.pem', 'a//b', './README.md', 'a\x00b'])
def test_bad_paths_rejected_for_download_preview_zip_and_snapshot(client, git_repo, path):
    tid = task(client, git_repo)
    wait_done(client, tid)
    for endpoint in ('download', 'preview'):
        assert client.get(f'/api/tasks/{tid}/files/{endpoint}', params={'path':path}).status_code >= 400
    assert client.post(f'/api/tasks/{tid}/files/zip', json={'paths':[path]}).status_code >= 400
    assert client.post(f'/api/tasks/{tid}/artifacts', json={'paths':[path]}).status_code >= 400


def test_symlinks_hardlinks_fifo_and_root_escape(client, git_repo, tmp_path):
    tid = task(client, git_repo)
    row = wait_done(client, tid)
    wt = Path(row['worktree'])
    secret = tmp_path / 'host-secret.txt'; secret.write_text('host secret')
    (wt / 'leak.txt').symlink_to(secret)
    (wt / 'directory').symlink_to(tmp_path, target_is_directory=True)
    os.link(secret, wt / 'hard.txt')
    os.mkfifo(wt / 'fifo.txt')
    for path in ('leak.txt', 'directory/host-secret.txt', 'hard.txt', 'fifo.txt'):
        assert client.get(f'/api/tasks/{tid}/files/download', params={'path':path}).status_code >= 400
        assert client.post(f'/api/tasks/{tid}/files/zip', json={'paths':[path]}).status_code >= 400
        assert client.post(f'/api/tasks/{tid}/artifacts', json={'paths':[path]}).status_code >= 400
    names = [f['path'] for f in client.get(f'/api/tasks/{tid}/files').json()['files']]
    assert names == ['README.md']
    moved = wt.with_name(wt.name + '-original')
    wt.rename(moved); wt.symlink_to(tmp_path, target_is_directory=True)
    try:
        assert client.get(f'/api/tasks/{tid}/files/download', params={'path':'host-secret.txt'}).status_code == 404
    finally:
        wt.unlink(); moved.rename(wt)


def test_open_descriptor_stays_safe_after_path_replaced(client, git_repo, tmp_path):
    tid = task(client, git_repo); row = wait_done(client, tid)
    path = Path(row['worktree']) / 'result.txt'; path.write_bytes(b'original')
    file, info = client.app.state.manager.files.open(tid, 'result.txt')
    secret = tmp_path / 'secret'; secret.write_bytes(b'secret')
    path.unlink(); path.symlink_to(secret)
    with file:
        assert file.read() == b'original'
    assert client.get(f'/api/tasks/{tid}/files/download', params={'path':'result.txt'}).status_code == 404


def test_bounded_preview_and_changed_committed_candidates(client, git_repo):
    tid = task(client, git_repo); row = wait_done(client, tid); wt = Path(row['worktree'])
    (wt/'verification.json').write_text('{"ok":true}')
    (wt/'large.log').write_bytes(b'x' * (300 * 1024))
    (wt/'binary.txt').write_bytes(b'a\0b')
    subprocess.run(['git','-C',str(wt),'add','verification.json'],check=True)
    subprocess.run(['git','-C',str(wt),'commit','-qm','artifact'],check=True)
    listed = client.get(f'/api/tasks/{tid}/files').json()['files']
    assert next(f for f in listed if f['path']=='verification.json')['changed']
    assert listed[-1]['path'] == 'README.md'
    preview = client.get(f'/api/tasks/{tid}/files/preview', params={'path':'large.log'}).json()
    assert preview['truncated'] and len(preview['text']) == 256 * 1024
    assert client.get(f'/api/tasks/{tid}/files/preview', params={'path':'binary.txt'}).status_code == 415
    assert [f['path'] for f in client.get(f'/api/tasks/{tid}/files', params={'path':'verification.json'}).json()['files']] == ['verification.json']
    assert [f['path'] for f in client.get(f'/api/tasks/{tid}/files', params={'q':'verification'}).json()['files']] == ['verification.json']


def test_limits_abort_cleanup_and_origin(client, settings):
    settings.file_max_bytes = 4
    assert client.post('/api/uploads', content=b'12345').status_code == 413
    store = client.app.state.manager.attachments
    async def interrupted():
        yield b'ab'
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(store.upload_file(interrupted(), 'partial'))
    async def oversized():
        yield b'123'
        yield b'456'
    with pytest.raises(AttachmentError):
        asyncio.run(store.upload_file(oversized(), 'too-big'))
    assert list(store.root.iterdir()) == []
    settings.file_store_max_count = 1
    upload(client, b'1234')
    assert client.post('/api/uploads', content=b'x').status_code == 413
    assert len(list(store.root.glob('*.bin'))) == 1
    assert client.post('/api/uploads', headers={'origin':'https://attacker.invalid'}, content=b'a').status_code == 403
    assert client.get('/api/uploads/limits', headers={'sec-fetch-site':'cross-site'}).status_code == 403


def test_storage_total_and_message_limits(client, settings, git_repo):
    settings.file_store_max_bytes = 8
    a = upload(client, b'1234')
    b = upload(client, b'5678')
    assert client.post('/api/uploads', content=b'9').status_code == 413
    settings.file_message_max_bytes = 7
    assert client.post('/api/tasks', json={'repository':str(git_repo), 'prompt':'read', 'attachment_ids':[a['id'], b['id']]}).status_code == 400


def test_file_schema_migration_preserves_old_image(tmp_path):
    path = tmp_path/'legacy.db'; con = sqlite3.connect(path); con.executescript(SCHEMA)
    con.execute("INSERT INTO attachments VALUES ('image','name','png','image/png',1,1,1,'now')"); con.commit(); con.close()
    for _ in range(2):
        db = Database(path)
        assert db.get_attachment('image')['kind'] == 'image'
        assert db.get_attachment('image')['task_id'] is None
        db.close()


@pytest.mark.parametrize('prompt,reattach', [('crashearly',True), ('crashunstarted',True), ('crash',False)])
def test_file_retry_reuses_reference_only_for_unconfirmed_turns(git_repo, make_manager, fake_codex_state, prompt, reattach):
    m = make_manager(backend='exec', retry_backoff_seconds=(.02,))
    async def stream(): yield b'retry-input'
    async def scenario():
        upload = await m.attachments.upload_file(stream(), 'retry.bin')
        m.scheduler.start(.02)
        try:
            t = await m.create_task(repository=str(git_repo), prompt=prompt, attachment_ids=[upload['id']])
            await wait_for(lambda:m.get(t['id'])['status']=='completed')
            sent = [json.loads(line)['prompt'] for line in (fake_codex_state/'invocations.jsonl').read_text().splitlines()]
            assert len(sent)==2
            assert upload['id']+'.bin' in sent[0]
            assert (upload['id']+'.bin' in sent[1]) == reattach
            assert len(m.db.list_messages(t['id']))==1
        finally:
            await m.shutdown()
    asyncio.run(scenario())


def test_generic_files_use_text_for_start_resume_and_steer(git_repo, make_manager, fake_codex_state):
    from test_app_server import calls, finished, running
    m = make_manager(backend='app-server')
    async def stream(): yield b'not embedded in prompt'
    async def scenario():
        try:
            a = await m.attachments.upload_file(stream(),'input.log')
            t = await m.create_task(repository=str(git_repo), prompt='ok', attachment_ids=[a['id']])
            thread = (await finished(m,t['id']))['codex_thread_id']
            await m.send_instruction(t['id'],'sleep',attachment_ids=[a['id']])
            await running(m,t['id'])
            await m.send_instruction(t['id'],'steered',attachment_ids=[a['id']])
            await m.stop(t['id']); await finished(m,t['id'])
            params = [r['params'] for r in calls(fake_codex_state,'turn/start')+calls(fake_codex_state,'turn/steer')]
            assert len(params)==3
            for p in params:
                assert p['threadId']==thread
                assert [i['type'] for i in p['input']]==['text']
                assert a['id']+'.bin' in p['input'][0]['text']
                assert 'not embedded in prompt' not in p['input'][0]['text']
        finally: await m.shutdown()
    asyncio.run(scenario())


def test_codex_declared_path_is_candidate_without_extension(client, git_repo):
    from app.logstore import TaskLog
    tid = task(client,git_repo); row = wait_done(client,tid)
    (Path(row['worktree'])/'REPORT').write_text('declared output')
    log=TaskLog(client.app.state.manager.log_path(tid))
    log.add_event('item/completed/agentMessage','Result: [report](' + row['worktree'] + '/REPORT:1)',{})
    log.close()
    candidate=client.get(f'/api/tasks/{tid}/files').json()['files'][0]
    assert candidate['path']=='REPORT' and candidate['declared'] and candidate['category']=='artifact'


def test_new_task_ownership_race_cleans_only_losing_worktree(git_repo,make_manager):
    m=make_manager(backend='exec')
    async def stream(): yield b'unique task input'
    async def scenario():
        try:
            a=await m.attachments.upload_file(stream(),'input.txt')
            results=await asyncio.gather(*(m.create_task(repository=str(git_repo),prompt='ok',attachment_ids=[a['id']]) for _ in range(2)),return_exceptions=True)
            assert len([r for r in results if isinstance(r,dict)])==1
            assert len([r for r in results if isinstance(r,Exception)])==1
            worktrees=subprocess.run(['git','-C',str(git_repo),'worktree','list','--porcelain'],capture_output=True,text=True,check=True).stdout
            assert len([l for l in worktrees.splitlines() if l.startswith('worktree ')])==2
        finally: await m.shutdown()
    asyncio.run(scenario())
