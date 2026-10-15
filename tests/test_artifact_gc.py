"""All files, Git worktrees, processes and databases in these tests are temporary."""
import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import artifact_gc as gc
from app.database import Database
from app.main import create_app
from conftest import FakeRunner, wait_for


def command(*args, cwd=None):
    return subprocess.check_output(args, cwd=cwd, text=True).strip()


def cache(root, now, age=6, name="target"):
    path = root / name
    (path / "debug" / "deps").mkdir(parents=True)
    (path / ".rustc_info.json").write_text('{}')
    (path / "debug" / "deps" / "libdemo.rlib").write_bytes(b'x' * 8192)
    for child in path.rglob('*'):
        if child.is_file():
            os.utime(child, (now - age * gc.DAY, now - age * gc.DAY))
    # Its directory has just been created. That does not make old files recent.
    return path


@pytest.fixture
def environment(settings, db, git_repo, monkeypatch):
    # File ctime is immutable via utime. Advance the test clock instead of weakening the production policy.
    now = time.time() + 10 * gc.DAY
    monkeypatch.setattr(gc.time, "time", lambda: now)
    monkeypatch.setattr("app.database.now_iso", lambda: gc.iso(now))
    root = settings.worktrees_dir / "repo" / "owner"
    root.parent.mkdir()
    command('git', '-C', str(git_repo), 'worktree', 'add', '-q', '-b', 'gc-owner', str(root))
    (root / '.gitignore').write_text('target/\nbuild/\n')
    (root / 'Cargo.toml').write_text('[package]\nname="demo"\nversion="0.1.0"\n')
    command('git', 'add', '.', cwd=root)
    command('git', 'commit', '-qm', 'build project', cwd=root)
    task = db.create_task(id='owner', name='owner', repository=str(git_repo), worktree=str(root), branch='gc-owner',
                         base_ref='main', base_sha=command('git', 'rev-parse', 'HEAD', cwd=root), prompt='ok',
                         created_at=gc.iso(now - 9 * gc.DAY), started_at=gc.iso(now - 7 * gc.DAY),
                         finished_at=gc.iso(now - 6 * gc.DAY), status='completed', task_outcome='success',
                         codex_thread_id='thread-preserved', claimed_by='historical-claim', pid=999999)
    monkeypatch.setattr(gc, 'process_usage', lambda roots: (set(), False))
    return settings, db, root, now, task


def scan(env, **kwargs):
    settings, db, root, now, task = env
    return gc.Scanner(settings.home, db.gc_tasks(), **kwargs).scan()


def test_four_days_kept_six_days_candidate_and_directory_time_ignored(environment):
    _, _, root, now, _ = environment
    old = cache(root, now)
    assert scan(environment)['candidate_count'] == 1
    assert old.stat().st_mtime < now - 5 * gc.DAY  # even newly created directory isn't the evidence
    recent = cache(root, now, age=4, name='recent')
    for p in recent.rglob('*'):
        if p.is_file():
            destination = old / p.relative_to(recent)
            os.utime(destination, (p.stat().st_atime, p.stat().st_mtime))
    row = next(r for r in scan(environment)['items'] if r['path'] == str(old))
    assert not row['candidate'] and 'within_retention' in row['reasons']


@pytest.mark.parametrize('status', ['queued', 'starting', 'running', 'retry_wait', 'waiting_dependencies', 'interrupted', 'waiting-for-quota'])
def test_task_states_protected(environment, status):
    settings, db, root, now, task = environment
    cache(root, now)
    task = {**task, 'status': status}
    report = gc.Scanner(settings.home, [task]).scan()
    assert report['candidate_count'] == 0
    assert 'task_active_or_waiting' in report['items'][0]['reasons']


def test_review_and_unknown_usage_protected(environment):
    _, db, root, now, _ = environment
    cache(root, now)
    db.update_task('owner', task_outcome='needs_review')
    assert 'needs_review' in scan(environment)['items'][0]['reasons']
    db.update_task('owner', task_outcome='success', finished_at=None)
    assert 'unknown_task_last_use' in scan(environment)['items'][0]['reasons']


def test_file_access_time_and_task_use_refresh_retention(environment):
    _, db, root, now, _ = environment
    path = cache(root, now)
    os.utime(path / '.rustc_info.json', (now - gc.DAY, now - 6 * gc.DAY))
    assert 'within_retention' in scan(environment)['items'][0]['reasons']
    os.utime(path / '.rustc_info.json', (now - 6 * gc.DAY, now - 6 * gc.DAY))
    db.update_task('owner', last_turn_at=gc.iso(now - gc.DAY))
    assert 'within_retention' in scan(environment)['items'][0]['reasons']


def test_nested_git_worktree_discovery_preserves_dirty_state(environment):
    _, _, root, now, _ = environment
    nested = root / 'agents' / 'deep' / 'nested'
    nested.parent.mkdir(parents=True)
    command('git', 'worktree', 'add', '-q', '-b', 'gc-nested', str(nested), cwd=root)
    path = cache(nested, now)
    (nested / 'README.md').write_text('uncommitted source')
    report = scan(environment)
    row = next(r for r in report['items'] if r['path'] == str(path))
    assert row['candidate']
    assert [s['path'] for s in row['worktrees']] == [str(root), str(nested)]
    assert row['worktrees'][-1]['dirty']
    assert (nested / 'README.md').read_text() == 'uncommitted source'


def test_symlink_external_and_shared_paths(environment, tmp_path):
    settings, db, root, now, task = environment
    outside = tmp_path / 'outside'
    outside.mkdir()
    external = cache(outside, now)
    (root / 'target').symlink_to(external, target_is_directory=True)
    report = scan(environment)
    assert report['candidate_count'] == 0
    assert 'unmanaged_or_symlink_path' in report['items'][0]['reasons']
    rogue = {**task, 'id': 'rogue', 'worktree': str(outside)}
    report = gc.Scanner(settings.home, [rogue]).scan()
    assert report['discovery_errors'] and report['items'] == []
    (root / 'target').unlink()
    path = cache(root, now)
    (path / 'linked').symlink_to(external, target_is_directory=True)
    assert 'symlink_in_artifact' in scan(environment)['items'][0]['reasons']
    assert external.is_dir()


def test_shared_configuration_and_hardlinks_excluded(environment, tmp_path):
    _, _, root, now, _ = environment
    path = cache(root, now)
    (root / '.cargo').mkdir()
    (root / '.cargo' / 'config.toml').write_text('[build]\ntarget-dir="target"\n')
    assert 'custom_or_shared_build_directory' in scan(environment)['items'][0]['reasons']
    (root / '.cargo' / 'config.toml').unlink()
    os.link(path / 'debug/deps/libdemo.rlib', tmp_path / 'shared.rlib')
    assert 'shared_hardlink' in scan(environment)['items'][0]['reasons']


@pytest.mark.parametrize('protection', ['tracked', 'untracked', 'contract', 'keep', 'log', 'database', 'git'])
def test_sources_explicit_artifacts_logs_and_git_are_preserved(environment, protection):
    _, db, root, now, _ = environment
    path = cache(root, now)
    expected = ''
    if protection == 'tracked':
        (path / 'source.rs').write_text('source')
        command('git', 'add', '-f', 'target/source.rs', cwd=root)
        expected = 'git_tracked_artifact'
    elif protection == 'untracked':
        (root / '.gitignore').write_text('target/*\n!target/source.rs\n')
        (path / 'source.rs').write_text('source')
        expected = 'uncommitted_artifact_content'
    elif protection == 'contract':
        db.update_task('owner', completion_contract=json.dumps({'required_paths': ['target/debug/deps/libdemo.rlib']}))
        expected = 'explicit_artifact'
    elif protection == 'keep':
        (path / '.codex-gui-keep').touch()
        expected = 'explicit_keep'
    else:
        (path / {'log': 'build.log', 'database': 'state.db', 'git': '.git'}[protection]).touch()
        expected = 'protected_content'
    row = scan(environment)['items'][0]
    assert not row['candidate'] and expected in row['reasons']


def test_build_requires_local_cmake_proof(environment):
    _, _, root, now, _ = environment
    build = root / 'build'
    build.mkdir()
    (build / 'object.o').write_text('generated')
    assert 'unconfirmed_build_directory' in scan(environment)['items'][0]['reasons']
    (build / 'CMakeFiles').mkdir()
    (build / 'CMakeCache.txt').write_text(f'CMAKE_HOME_DIRECTORY:INTERNAL={root}\nCMAKE_CACHEFILE_DIR:INTERNAL={build}\n')
    for p in build.rglob('*'):
        if p.is_file():
            os.utime(p, (now - 6 * gc.DAY, now - 6 * gc.DAY))
    assert scan(environment)['candidate_count'] == 1


def snapshot(root):
    return {str(p.relative_to(root)): (p.stat().st_ino, p.stat().st_size, p.stat().st_mtime_ns)
            for p in root.rglob('*') if not p.is_symlink()}


def test_dry_run_files_unchanged_history_persists_and_default_deletion_off(environment):
    settings, db, root, now, _ = environment
    path = cache(root, now)
    before = snapshot(root)
    service = gc.ArtifactGC(settings.home, db)
    report = service.run()
    assert report['candidate_count'] == 1 and report['released_bytes'] == 0
    assert report['dry_run'] and snapshot(root) == before
    with pytest.raises(ValueError, match='Enable'):
        service.run(False)
    assert path.exists()
    second = Database(settings.db_path)
    try:
        assert json.loads(second.gc_history()[0]['report_json'])['candidate_count'] == 1
        assert gc.ArtifactGC(settings.home, second).settings() == {'retention_days': 5, 'auto_delete': False}
    finally:
        second.close()


def test_process_revalidation_prevents_deletion(environment, monkeypatch):
    settings, db, root, now, _ = environment
    path = cache(root, now)
    probes = []
    def probe(roots):
        probes.append(1)
        return ({root} if len(probes) > 1 else set(), False)
    monkeypatch.setattr(gc, 'process_usage', probe)
    service = gc.ArtifactGC(settings.home, db)
    service.configure(5, True)
    report = service.run(False)
    assert len(probes) >= 2 and path.exists() and report['released_bytes'] == 0
    assert 'worktree_process_active' in report['items'][0]['reasons']


def test_queued_during_scan_prevents_deletion(environment, monkeypatch):
    settings, db, root, now, _ = environment
    path = cache(root, now)
    original = gc.Scanner.scan
    count = []
    def race(self):
        report = original(self)
        count.append(1)
        if len(count) == 1:
            db.set_status('owner', 'queued')
        return report
    monkeypatch.setattr(gc.Scanner, 'scan', race)
    service = gc.ArtifactGC(settings.home, db)
    service.configure(5, True)
    assert service.run(False)['released_bytes'] == 0
    assert path.is_dir()


def test_removal_cannot_follow_replaced_ancestor_or_target(environment, tmp_path):
    settings, _, root, now, _ = environment
    path = cache(root, now)
    identity = [path.stat().st_dev, path.stat().st_ino]
    displaced = root / 'old-target'
    path.rename(displaced)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'source').write_text('keep')
    path.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='artifact_changed'):
        gc.remove_verified(path, settings.worktrees_dir, identity)
    assert (outside / 'source').read_text() == 'keep'


def test_launch_gate_prevents_start_until_gc_finishes(environment, make_manager):
    settings, db, root, now, _ = environment
    cache(root, now)
    manager = make_manager()
    async def scenario():
        with gc.launch_guard(settings.home, exclusive=True) as acquired:
            assert acquired
            db.set_status('owner', 'queued', pending_turn='{"prompt":"ok","resume_thread":"thread-preserved"}', claimed_by=None)
            assert not manager._jobs
            assert not manager._dispatch('owner')
        manager._tick_queue()
        assert len(manager._jobs) == 1
        await wait_for(lambda: db.get_task('owner')['status'] == 'completed')
        await manager.shutdown()
    asyncio.run(scenario())


def test_gc_preserves_task_history_git_dirty_files_and_same_thread_resume(environment, make_manager):
    settings, db, root, now, _ = environment
    path = cache(root, now)
    (root / 'README.md').write_text('keep uncommitted edits')
    before = db.get_task('owner')
    git_before = command('git', 'status', '--porcelain', cwd=root)
    service = gc.ArtifactGC(settings.home, db)
    service.configure(5, True)
    report = service.run(False)
    assert report['released_bytes'] > 0 and not path.exists()
    assert db.get_task('owner') == before
    assert command('git', 'status', '--porcelain', cwd=root) == git_before
    assert command('git', 'rev-parse', '--abbrev-ref', 'HEAD', cwd=root) == before['branch']
    manager = make_manager()
    async def scenario():
        await manager.send_instruction('owner', 'ok after gc')
        await wait_for(lambda: db.get_task('owner')['status'] == 'completed')
        assert db.get_task('owner')['codex_thread_id'] == before['codex_thread_id']
        assert db.get_task('owner')['worktree'] == str(root)
        assert (root / 'README.md').read_text() == 'keep uncommitted edits'
        await manager.shutdown()
    asyncio.run(scenario())


def test_daily_gc_idempotent_survives_restart(environment):
    settings, db, root, now, _ = environment
    cache(root, now)
    first = gc.ArtifactGC(settings.home, db)
    assert first.due()
    first.run(trigger='daily')
    second = gc.ArtifactGC(settings.home, db)
    assert not second.due()
    assert second.run(trigger='daily')['status'] == 'not_due'
    assert len(db.gc_history()) == 1


def test_in_progress_collection_is_not_run_twice(environment):
    settings, db, root, now, _ = environment
    service = gc.ArtifactGC(settings.home, db)
    with gc.launch_guard(settings.home, exclusive=True, filename='artifact-gc-run.lock'):
        assert service.run()['status'] == 'busy'
    assert db.gc_history() == []


def test_real_process_in_nested_worktree_is_detected(tmp_path):
    root = tmp_path / 'nested'
    root.mkdir()
    proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], cwd=root)
    try:
        busy, _ = gc.process_usage({root})
        assert root in busy
    finally:
        proc.terminate()
        proc.wait()


def test_web_ui_settings_history_and_dry_run_default(settings):
    with TestClient(create_app(settings, FakeRunner())) as client:
        assert 'Build artifact GC' in client.get('/storage').text
        assert client.get('/static/storage.js').status_code == 200
        assert client.get('/api/artifact-gc').json()['settings'] == {'retention_days': 5, 'auto_delete': False}
        assert client.post('/api/artifact-gc/run', json={'dry_run': False}).status_code == 409
        assert client.put('/api/artifact-gc/settings', json={'retention_days': 0}).status_code == 422
        result = client.post('/api/artifact-gc/run', json={})
        # Startup may already be running its once-daily Dry Run.
        assert result.status_code in (200, 409)
        state = client.get('/api/artifact-gc').json()
        assert all(row['dry_run'] for row in state['history'])
        assert client.put('/api/artifact-gc/settings', json={'retention_days': 10}).json()['retention_days'] == 10


def test_read_only_cli_does_not_modify_db_or_targets(environment, tmp_path):
    settings, db, root, now, _ = environment
    cache(root, now)
    before = snapshot(root)
    database_before = db.list_tasks()
    output = tmp_path / 'report.json'
    subprocess.run([sys.executable, '-m', 'app.artifact_gc', '--home', str(settings.home), '--output', str(output)],
                   check=True, capture_output=True)
    report = json.loads(output.read_text())
    assert report['dry_run'] and report['released_bytes'] == 0
    assert db.list_tasks() == database_before and db.gc_history() == []
    assert snapshot(root) == before
    assert not (settings.home / 'artifact-gc.lock').exists()
    assert not (settings.home / 'artifact-gc-run.lock').exists()


def test_internal_hardlinks_are_reclaimable_and_size_is_not_double_counted(environment):
    _, _, root, now, _ = environment
    path = cache(root, now)
    initial = scan(environment)['candidate_bytes']
    os.link(path / 'debug/deps/libdemo.rlib', path / 'debug/libdemo.rlib')
    report = scan(environment)
    assert report['candidate_count'] == 1 and report['candidate_bytes'] == initial


def test_reverse_symlink_sharing_protects_the_destination(environment):
    _, _, root, now, _ = environment
    path = cache(root, now)
    (root / 'other-project').mkdir()
    (root / 'other-project' / 'shared-cache').symlink_to(path, target_is_directory=True)
    row = scan(environment)['items'][0]
    assert not row['candidate'] and 'referenced_or_shared_directory' in row['reasons']


def test_nested_worktree_below_unknown_build_is_discovered(environment):
    _, _, root, now, _ = environment
    nested = root / 'build' / 'agents' / 'nested'
    nested.parent.mkdir(parents=True)
    command('git', 'worktree', 'add', '-q', '-b', 'gc-below-build', str(nested), cwd=root)
    path = cache(nested, now)
    rows = scan(environment)['items']
    row = next(r for r in rows if r['path'] == str(path))
    assert row['candidate'] and row['worktrees'][-1]['path'] == str(nested)
    assert not next(r for r in rows if r['path'] == str(root / 'build'))['candidate']


def test_pending_scheduled_instruction_protects_completed_task(environment):
    settings, db, root, now, _ = environment
    cache(root, now)
    db.create_scheduled('owner', 'next turn after prerequisites', 'default')
    service = gc.ArtifactGC(settings.home, db)
    report = service.run()
    assert not report['candidate_count']
    assert 'task_active_or_waiting' in report['items'][0]['reasons']


def test_unknown_process_visibility_protects_artifacts(environment, monkeypatch):
    _, _, root, now, _ = environment
    cache(root, now)
    monkeypatch.setattr(gc, 'process_usage', lambda roots: (set(), True))
    assert 'process_inspection_unavailable' in scan(environment)['items'][0]['reasons']


def test_replaced_ancestor_is_not_followed(environment, tmp_path):
    settings, _, root, now, _ = environment
    path = cache(root, now)
    identity = [path.stat().st_dev, path.stat().st_ino]
    saved = root.with_name('saved')
    root.rename(saved)
    external = tmp_path / 'external'
    external.mkdir()
    cache(external, now)
    root.symlink_to(external, target_is_directory=True)
    with pytest.raises(OSError):
        gc.remove_verified(path, settings.worktrees_dir, identity)
    assert (external / 'target').is_dir() and (saved / 'target').is_dir()


def test_real_gc_worker_blocks_dispatch_and_rechecks_queued_task(environment, make_manager, monkeypatch):
    settings, db, root, now, _ = environment
    path = cache(root, now)
    manager = make_manager()
    manager.artifact_gc.configure(5, True)
    entered, release = threading.Event(), threading.Event()
    original = gc.Scanner.scan
    def paused(self):
        report = original(self)
        entered.set()
        assert release.wait(10)
        return report
    monkeypatch.setattr(gc.Scanner, 'scan', paused)
    async def scenario():
        job = manager.start_artifact_gc(False)
        try:
            assert await asyncio.to_thread(entered.wait, 10)
            await manager.send_instruction('owner', 'ok queued during gc')
            assert db.get_task('owner')['status'] == 'queued'
            assert not manager._jobs
        finally:
            release.set()
        report = await job
        assert report['released_bytes'] == 0 and path.is_dir()
        manager._tick_queue()
        await wait_for(lambda: db.get_task('owner')['status'] == 'completed')
        await manager.shutdown()
    asyncio.run(scenario())


def test_concurrent_api_runs_return_conflict_and_worker_completes(settings, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    entered, release = threading.Event(), threading.Event()
    app = create_app(settings, FakeRunner())
    # Keep the automatic startup scan out of this manual-concurrency test.
    monkeypatch.setattr(app.state.manager.artifact_gc, 'due', lambda: False)
    original = app.state.manager.artifact_gc.run
    def paused(*args):
        entered.set()
        assert release.wait(10)
        return original(*args)
    monkeypatch.setattr(app.state.manager.artifact_gc, 'run', paused)
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.post, '/api/artifact-gc/run', json={})
        try:
            assert entered.wait(10)
            assert client.post('/api/artifact-gc/run', json={}).status_code == 409
        finally:
            release.set()
        assert future.result(timeout=10).status_code == 200
