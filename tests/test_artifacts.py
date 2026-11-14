"""Snapshot lifetime, ownership, dependency/restart safety and CAS integrity."""
import asyncio
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.artifacts import ArtifactStore
from app.attachments import AttachmentStore
from app.database import Database
from app.task_manager import TaskManager, _Turn
from app.task_files import FileError, TaskFiles
from conftest import FakeAppServer, wait_for
from test_api import wait_done
from test_files import ReadingRunner, client, task, upload
from test_attachments import image_bytes


def snapshot(c, source, paths):
    r = c.post(f'/api/tasks/{source}/artifacts', json={'paths':paths})
    assert r.status_code == 200, r.text
    return r.json()['artifacts']


def bind(c, artifacts, target=None):
    r = c.post('/api/artifacts/inputs', json={'artifact_ids':[a['id'] for a in artifacts], 'task_id':target})
    assert r.status_code == 200, r.text
    return r.json()['inputs']


def test_multiple_artifacts_new_and_existing_thread_cas_delete_and_restart(settings, git_repo):
    data = b'{"verification":"A to B"}'
    with TestClient(create_app(settings, ReadingRunner(), FakeAppServer('fake'))) as c:
        a = upload(c, data)
        source = task(c, git_repo, attachment_ids=[a['id']]); s = wait_done(c, source)
        artifacts = snapshot(c, source, ['verification.json', 'received-0.bin'])
        assert artifacts[0]['sha256'] == artifacts[1]['sha256'] == hashlib.sha256(data).hexdigest()
        assert artifacts[0]['source_task_id'] == source and len(artifacts[0]['revision']) == 40
        assert len(list(c.app.state.manager.artifacts.root.glob('*.bin'))) == 1
        assert snapshot(c, source, ['verification.json'])[0]['id'] == artifacts[0]['id']
        inputs = bind(c, artifacts)
        target = task(c, git_repo, attachment_ids=[a['id'] for a in inputs]); b = wait_done(c, target)
        assert (Path(b['worktree'])/'received-0.bin').read_bytes() == data
        assert (Path(b['worktree'])/'received-1.bin').read_bytes() == data
        # Each destination has its own immutable input, no shared writable inode.
        store = c.app.state.manager.attachments
        assert store.path(store.get(inputs[0]['id'])).stat().st_ino != store.path(store.get(inputs[1]['id'])).stat().st_ino
        (Path(s['worktree'])/'verification.json').write_bytes(b'changed source')
        assert c.delete(f'/api/tasks/{source}/worktree', params={'force':True}).status_code == 200
        assert c.get(f'/api/tasks/{source}/files').status_code == 410
        assert len(c.get(f'/api/tasks/{source}/artifacts').json()['snapshots']) == 2
        continued = bind(c, [artifacts[0]], target)
        assert c.post(f'/api/tasks/{target}/messages', json={'prompt':'Read immutable snapshot', 'attachment_ids':[continued[0]['id']]}).status_code == 200
        b2 = wait_done(c, target)
        assert b2['codex_thread_id'] == b['codex_thread_id']
        assert (Path(b2['worktree'])/'verification.json').read_bytes() == data
    with TestClient(create_app(settings, ReadingRunner(), FakeAppServer('fake'))) as c:
        inputs2 = bind(c, artifacts, target)
        assert c.post(f'/api/tasks/{target}/messages', json={'prompt':'After restart', 'attachment_ids':[a['id'] for a in inputs2]}).status_code == 200
        assert wait_done(c, target)['codex_thread_id'] == b['codex_thread_id']
        assert c.get(f'/api/tasks/{target}/inputs/{inputs[0]["id"]}').content == data


def test_consistent_backup_restore_preserves_nonempty_artifacts_images_and_reservations(settings, git_repo, tmp_path):
    data = b'fixed artifact survives backup and deleted source'
    restored_settings = replace(settings, home=tmp_path / 'restored')
    restored_settings.ensure_dirs()
    with TestClient(create_app(settings, ReadingRunner(), FakeAppServer('fake'))) as c:
        source = task(c, git_repo, attachment_ids=[upload(c, data)['id']])
        wait_done(c, source)
        artifact = snapshot(c, source, ['verification.json'])[0]
        incoming = bind(c, [artifact])[0]
        response = c.post('/api/attachments', content=image_bytes())
        assert response.status_code == 200
        image = response.json()
        ids = [incoming['id'], image['id']]
        target = task(c, git_repo, attachment_ids=ids)
        before = wait_done(c, target)
        m = c.app.state.manager
        m.db.update_task(source, task_outcome='needs_review')
        response = c.post(f'/api/tasks/{target}/scheduled',
                          json={'prompt': 'After source acceptance', 'depends_on': [source], 'attachment_ids': ids})
        assert response.status_code == 200
        reservations = m.db.list_scheduled(target)
        assert len(reservations) == 1 and reservations[0]['status'] == 'waiting_dependencies'
        references = m.db.list_messages(target)
        with m.db._lock, sqlite3.connect(restored_settings.db_path) as dest:
            m.db._conn.backup(dest)
        for directory in ['attachments', 'artifacts']:
            shutil.copytree(settings.home / directory, restored_settings.home / directory)
        assert c.delete(f'/api/tasks/{source}/worktree', params={'force': True}).status_code == 200

    for _ in range(2):
        Database(restored_settings.db_path).close()
    restored = Database(restored_settings.db_path)
    try:
        assert restored.get_task(target)['codex_thread_id'] == before['codex_thread_id']
        assert restored.list_scheduled(target) == reservations
        assert restored.list_messages(target) == references
        store = AttachmentStore(restored_settings.home / 'attachments', restored, restored_settings)
        assert store.resolve(ids, target) == [str(store.path(store.get(image['id'])))]
        assert store.path(store.get(incoming['id'])).read_bytes() == data
        assert store.path(store.get(image['id'])).read_bytes() == image_bytes()
        manager = SimpleNamespace(settings=restored_settings, db=restored, attachments=store, get=restored.get_task)
        manager.files = TaskFiles(manager)
        transferred = ArtifactStore(manager).input(artifact['id'], target)
        assert store.path(store.get(transferred['id'])).read_bytes() == data
        assert restored._conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    finally:
        restored.close()


def test_blocked_source_manual_transfer_is_explicit(client, git_repo):
    source = task(client, git_repo, attachment_ids=[upload(client)['id']]); wait_done(client, source)
    client.app.state.manager.db.update_task(source, task_outcome='blocked', outcome_reason='test blocked')
    artifacts = snapshot(client, source, ['verification.json'])
    inputs = bind(client, artifacts)
    target = task(client, git_repo, attachment_ids=[inputs[0]['id']])
    assert wait_done(client, target)['task_outcome'] == 'success'


def test_deferred_dependency_success_and_duplicate_ticks(settings, db, git_repo, tmp_path):
    async def scenario():
        m = TaskManager(settings, db, ReadingRunner())
        try:
            gate = tmp_path/'gate'
            source = await m.create_task(repository=str(git_repo), prompt=f'gate {gate}')
            await wait_for(lambda:m.get(source['id'])['status']=='running')
            target = await m.create_task(repository=str(git_repo), prompt='Read after source success',
                                         artifact_refs=[{'source_task_id':source['id'], 'path':'verification.json'}])
            assert target['status'] == 'waiting_dependencies' and target['worktree_pending']
            assert not Path(target['worktree']).exists()
            data = b'{"after":"success"}'
            (Path(source['worktree'])/'verification.json').write_bytes(data)
            # Completed process with non-success semantic outcome must still hold the target.
            for _ in range(5): m.tick()
            assert not db.artifact_requests(target['id'])[0]['attachment_id']
            gate.touch()
            await wait_for(lambda:m.get(source['id'])['task_outcome']=='success')
            await wait_for(lambda:m.get(target['id'])['task_outcome']=='success')
            for _ in range(10): m.tick()
            assert len(db.list_attempts(target['id'])) == 1
            assert len(db.list_messages(target['id'])) == 1
            assert (Path(target['worktree'])/'verification.json').read_bytes() == data
            ref = db.artifact_requests(target['id'])[0]
            assert ref['artifact_id'] and ref['attachment_id']
        finally:
            await m.shutdown()
    asyncio.run(scenario())


def test_semantic_block_holds_automatic_transfer(client, git_repo):
    source = task(client, git_repo); wait_done(client, source)
    m = client.app.state.manager
    m.db.update_task(source, task_outcome='blocked')
    target = task(client, git_repo, artifact_refs=[{'source_task_id':source,'path':'verification.json'}])
    assert m.get(target)['status'] == 'waiting_dependencies'
    assert not m.db.artifact_requests(target)[0]['attachment_id']
    assert not m.db.list_attempts(target)


def test_pending_reference_survives_restart_and_attachment_on_waiting_task(settings, git_repo):
    with TestClient(create_app(settings, ReadingRunner(), FakeAppServer('fake'))) as c:
        source = task(c, git_repo); wait_done(c, source)
        m = c.app.state.manager
        (Path(m.get(source)['worktree'])/'verification.json').write_bytes(b'future artifact')
        m.db.update_task(source, task_outcome='needs_review')
        target = task(c, git_repo, artifact_refs=[{'source_task_id':source,'path':'verification.json'}])
        uploaded = upload(c, b'additional pending')
        r = c.post(f'/api/tasks/{target}/inputs', json={'attachment_ids':[uploaded['id']]})
        assert r.status_code == 200 and r.json()['pending_turn_updated']
    with TestClient(create_app(settings, ReadingRunner(), FakeAppServer('fake'))) as c:
        m = c.app.state.manager
        assert m.db.artifact_requests(target)[0]['source_task_id'] == source
        m.db.update_task(source, task_outcome='success')
        c.portal.call(m.tick)
        result = wait_done(c, target)
        assert result['task_outcome'] == 'success'
        assert (Path(result['worktree'])/'received-0.bin').read_bytes() == b'additional pending'
        assert (Path(result['worktree'])/'received-1.bin').read_bytes() == b'future artifact'
        assert len(m.db.list_attempts(target)) == 1


@pytest.mark.parametrize('race_stage', ['before_request', 'during_transaction'])
def test_pending_transfer_rejects_destination_start_race(client, git_repo, monkeypatch, race_stage):
    source = task(client, git_repo)
    wait_done(client, source)
    m = client.app.state.manager
    m.db.update_task(source, task_outcome='needs_review')
    target = task(client, git_repo, depends_on=[source])
    assert m.get(target)['status'] == 'waiting_dependencies'
    client.portal.call(m.scheduler.stop)
    pending_before = m.get(target)['pending_turn']
    uploaded = upload(client, b'not silently delivered')

    def start_destination():
        with m.db._lock:
            m.db.update_task(source, task_outcome='success')
            m.db.set_status(target, 'queued')
            m.db.set_status(target, 'starting', claimed_by='racing-test-worker')
            m.db.set_status(target, 'running')

    if race_stage == 'before_request':
        start_destination()
    else:
        original = m.db.attach_pending_files
        def race(*args):
            start_destination()
            return original(*args)
        monkeypatch.setattr(m.db, 'attach_pending_files', race)

    response = client.post(f'/api/tasks/{target}/inputs',
                           json={'attachment_ids': [uploaded['id']], 'pending_only': True})
    assert response.status_code == 409
    assert m.get(target)['pending_turn'] == pending_before
    assert m.db.get_attachment(uploaded['id'])['task_id'] is None
    assert all(uploaded['id'] not in row['attachment_ids'] for row in m.db.list_messages(target))


def test_missing_snapshot_and_invalid_reference_are_rejected(client, git_repo):
    source = task(client, git_repo, attachment_ids=[upload(client)['id']]); wait_done(client, source)
    artifact = snapshot(client, source, ['verification.json'])[0]
    for aid in ('../../etc/passwd', '0'*32):
        assert client.post('/api/artifacts/inputs', json={'artifact_ids':[aid]}).status_code >= 400
    blob = client.app.state.manager.artifacts.root / (artifact['sha256']+'.bin')
    blob.unlink()
    assert client.post('/api/artifacts/inputs', json={'artifact_ids':[artifact['id']]}).status_code >= 400
    response = client.post('/api/tasks', json={'repository':str(git_repo), 'prompt':'bad', 'artifact_refs':[{'source_task_id':source,'path':'../host'}]})
    assert response.status_code >= 400


def test_transfer_detects_source_race_and_cleans_partial(client, git_repo, monkeypatch):
    source = task(client, git_repo, attachment_ids=[upload(client)['id']]); row = wait_done(client, source)
    m = client.app.state.manager
    from app import artifacts as module
    original = module.chunks
    def racing(file, limit):
        yield from original(file, limit)
        (Path(row['worktree'])/'verification.json').write_bytes(b'mutated while copying')
    monkeypatch.setattr(module, 'chunks', racing)
    response = client.post(f'/api/tasks/{source}/artifacts', json={'paths':['verification.json']})
    assert response.status_code == 409
    assert m.db.list_artifacts() == []
    assert list(m.artifacts.root.iterdir()) == []


def test_artifact_quota(client, git_repo, settings):
    source = task(client, git_repo, attachment_ids=[upload(client)['id']]); wait_done(client, source)
    settings.file_store_max_bytes = 1
    assert client.post(f'/api/tasks/{source}/artifacts', json={'paths':['verification.json']}).status_code == 413


def test_input_claim_is_atomic_under_race(client, git_repo):
    uploaded = upload(client)
    a = task(client, git_repo); b = task(client, git_repo)
    wait_done(client, a); wait_done(client, b)
    def claim(tid):
        try:
            client.app.state.manager.db.claim_files(tid, [uploaded['id']])
            return tid
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, [a,b]))
    assert len([r for r in results if r]) == 1
    assert client.app.state.manager.db.get_attachment(uploaded['id'])['task_id'] in results


def test_snapshot_download_survives_worktree_deletion(client,git_repo):
    data=b'persisted snapshot'
    source=task(client,git_repo,attachment_ids=[upload(client,data)['id']]); wait_done(client,source)
    artifact=snapshot(client,source,['verification.json'])[0]
    assert client.delete(f'/api/tasks/{source}/worktree',params={'force':True}).status_code==200
    r=client.get(f'/api/tasks/{source}/artifacts/{artifact["id"]}/download')
    assert r.status_code==200 and r.content==data
    other=task(client,git_repo); wait_done(client,other)
    assert client.get(f'/api/tasks/{other}/artifacts/{artifact["id"]}/download').status_code==404


def test_batch_transfer_is_atomic_on_bad_second_reference(client,git_repo):
    source=task(client,git_repo,attachment_ids=[upload(client)['id']]); wait_done(client,source)
    artifact=snapshot(client,source,['verification.json'])[0]
    store=client.app.state.manager.attachments
    before=set(store.root.iterdir()); usage=client.app.state.manager.db.upload_usage()
    assert client.post('/api/artifacts/inputs',json={'artifact_ids':[artifact['id'],'0'*32]}).status_code==404
    assert set(store.root.iterdir())==before
    assert client.app.state.manager.db.upload_usage()==usage


def test_pending_association_enforces_combined_message_quota(client,git_repo,settings):
    source=task(client,git_repo); wait_done(client,source)
    m=client.app.state.manager; m.db.update_task(source,task_outcome='needs_review')
    first=upload(client,b'1234'); second=upload(client,b'5678')
    target=task(client,git_repo,depends_on=[source],attachment_ids=[first['id']])
    settings.file_message_max_bytes=7
    assert client.post(f'/api/tasks/{target}/inputs',json={'attachment_ids':[second['id']]}).status_code==400
    assert m.db.get_attachment(second['id'])['task_id'] is None
    assert _Turn.from_json(m.get(target)['pending_turn']).attachment_ids==[first['id']]


def test_existing_snapshot_is_reusable_at_store_count_limit(client,git_repo,settings):
    source=task(client,git_repo,attachment_ids=[upload(client)['id']]); wait_done(client,source)
    original=snapshot(client,source,['verification.json'])[0]
    settings.file_store_max_count=1
    assert snapshot(client,source,['verification.json'])[0]['id']==original['id']


def test_input_filename_collision_never_deletes_existing_upload(client,git_repo,monkeypatch):
    from types import SimpleNamespace
    import uuid
    original=upload(client,b'original immutable input')
    source=task(client,git_repo,attachment_ids=[original['id']]); wait_done(client,source)
    artifact=snapshot(client,source,['verification.json'])[0]
    store=client.app.state.manager.attachments
    path=store.path(store.get(original['id']))
    monkeypatch.setattr(uuid,'uuid4',lambda:SimpleNamespace(hex=original['id']))
    assert client.post('/api/uploads',content=b'replacement').status_code==507
    assert path.read_bytes()==b'original immutable input'
    with pytest.raises(FileExistsError): client.app.state.manager.artifacts.input(artifact['id'])
    assert path.read_bytes()==b'original immutable input'


def test_part_filename_collision_never_deletes_existing_partial(client,git_repo,monkeypatch):
    from types import SimpleNamespace
    import uuid
    aid='a'*32
    root=client.app.state.manager.attachments.root
    partial=root/(aid+'.part'); partial.write_bytes(b'other upload in progress')
    monkeypatch.setattr(uuid,'uuid4',lambda:SimpleNamespace(hex=aid))
    assert client.post('/api/uploads',content=b'new upload').status_code==507
    assert partial.read_bytes()==b'other upload in progress'
