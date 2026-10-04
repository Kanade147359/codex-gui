"""Graph reads manual settings only. All fixtures are isolated; no Codex process is launched."""
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.database import Database
from app.dependency_graph import graph_view
from app.main import create_app


def row(db, tid, status='waiting_dependencies', repo='/repo', prompt='work'):
    return db.create_task(id=tid, name=tid.upper(), repository=repo, worktree='/uncreated', branch=tid,
                          base_ref='main', base_sha='abc', prompt=prompt, last_prompt=prompt,
                          status=status, created_at='2026-01-01T00:00:00Z')


def pairs(view):
    return {(e['parent'], e['child']) for e in view['edges']}


def test_manual_fork_join_independent_cross_repo(db):
    for tid in 'abcde': row(db, tid, repo='/other' if tid == 'd' else '/repo',
                            prompt='Aの完了後に全タスクを実行 同じfile.py')
    db.replace_dependencies('b', ['a'])
    db.replace_dependencies('c', ['a'])
    sid = db.create_scheduled('d', 'integrate B and C', 'default', ['b', 'c'])['id']
    view = graph_view(db)
    assert pairs(view) == {('a','b'), ('a','c'), ('b','d'), ('c','d')}
    assert not any(e['parent']=='e' or e['child']=='e' for e in view['edges'])
    assert len(view['tasks']) == 5 and view['errors'] == []
    assert next(e for e in view['edges'] if e['child']=='d')['settings'][0]['id'] == sid
    assert db.dependencies_of('d') == []  # scheduled follow-up does not become an initial-task dependency


def test_same_names_numbers_instructions_do_not_infer_edges(db):
    for tid in 'abc': row(db,tid,prompt='タスクA完了後、同じファイルを編集')
    assert graph_view(db)['edges'] == []
    db.create_scheduled('c', 'after all tasks', 'default', [])
    assert graph_view(db)['edges'] == []


def test_settings_change_cancel_and_no_duplicates(db):
    for tid in 'abc': row(db,tid)
    db.replace_dependencies('c',['a'])
    first = db.create_scheduled('c','same instruction','default',['a','b'])
    second = db.create_scheduled('c','another turn','priority',['a'])
    view = graph_view(db)
    assert pairs(view) == {('a','c'),('b','c')}
    assert len(next(e for e in view['edges'] if e['parent']=='a')['settings']) == 3
    db.cancel_scheduled(first['id'])
    assert pairs(graph_view(db)) == {('a','c')}
    db.cancel_scheduled(second['id'])
    db.replace_dependencies('c',['b'])
    assert pairs(graph_view(db)) == {('b','c')}


def test_state_update_reads_saved_state_and_never_mutates_settings(db):
    row(db,'a'); row(db,'b')
    db.replace_dependencies('b',['a'])
    before = db._conn.total_changes
    original = graph_view(db)
    assert db._conn.total_changes == before
    db.set_status('a','queued'); db.set_status('a','starting'); db.set_status('a','running',status_detail='progress')
    db.update_task('b',status_detail='waiting for a')
    updated = graph_view(db)
    assert updated['edges'] == original['edges']
    assert next(t for t in updated['tasks'] if t['id']=='a')['status'] == 'running'
    assert next(t for t in updated['tasks'] if t['id']=='b')['status_detail'] == 'waiting for a'


def test_persistence_and_existing_database(db,settings):
    row(db,'a'); row(db,'b')
    db.replace_dependencies('b',['a'])
    db.create_scheduled('b','follow-up','default',['a'])
    before = graph_view(db)
    reloaded = Database(settings.db_path)
    try: assert graph_view(reloaded) == before
    finally: reloaded.close()
    assert not db._conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'dependency_analysis%'").fetchall()


def test_invalid_references_are_reported_without_invented_nodes(db):
    row(db,'a'); row(db,'b')
    # Model a damaged older database. Reading must not repair it or silently manufacture tasks.
    db._conn.execute("INSERT INTO task_dependencies(task_id,depends_on_task_id,created_at) VALUES('b','missing','x')")
    schedule = db.create_scheduled('b','follow-up','default',['a'])
    db._conn.execute("UPDATE scheduled_instruction_dependencies SET depends_on_task_id='missing-two' WHERE scheduled_instruction_id=?",(schedule['id'],))
    view = graph_view(db)
    assert view['edges'] == [] and {t['id'] for t in view['tasks']} == {'a','b'}
    assert len(view['errors']) == 2
    assert 'missing-two' in view['errors'][1]['message']


def test_projected_schedule_cycle_is_visible_without_scheduler_change(db):
    row(db,'a'); row(db,'b')
    db.create_scheduled('b','later','default',['a'])
    db.create_scheduled('a','later','default',['b'])
    before = db._conn.total_changes
    view = graph_view(db)
    assert pairs(view) == {('a','b'),('b','a')}
    assert '循環' in view['errors'][0]['message']
    assert db._conn.total_changes == before


def test_ai_provenance_is_excluded_not_migrated_to_manual(db):
    for tid in 'abc': row(db,tid)
    db.replace_dependencies('b',['a']); db.replace_dependencies('c',['a'])
    db._conn.execute("ALTER TABLE task_dependencies ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'")
    db._conn.execute("UPDATE task_dependencies SET source='ai_confirmed' WHERE task_id='c'")
    db.create_scheduled('c','AI suggestion','default',['b'],created_by='ai')
    db._conn.execute("CREATE TABLE dependency_candidates(prerequisite TEXT,successor TEXT)")
    db._conn.execute("INSERT INTO dependency_candidates VALUES('b','c')")
    assert pairs(graph_view(db)) == {('a','b')}
    assert db.dependencies_of('c') == ['a']  # read-only: existing settings remain untouched


@pytest.mark.parametrize("backend", ["exec", "app-server"])
def test_api_is_read_only_and_ai_calls_zero(settings,monkeypatch,backend):
    settings.backend = backend
    app = create_app(settings)
    m = app.state.manager
    spawn = AsyncMock(side_effect=AssertionError('graph must not launch Codex'))
    monkeypatch.setattr(m.runner,'spawn',spawn)
    client_call = AsyncMock(side_effect=AssertionError('graph must not contact app-server'))
    monkeypatch.setattr(m,'client',client_call)
    monkeypatch.setattr(m,'_dispatch',lambda task_id:False)
    monkeypatch.setattr(m.scheduler,'_tick',lambda:None)
    for tid in 'abc': row(m.db,tid)
    m.db.replace_dependencies('b',['a'])
    with TestClient(app) as client:
        for _ in range(3):
            assert client.get('/dependencies').status_code == 200
            assert pairs(client.get('/api/dependency-graph').json()) == {('a','b')}
        page = client.get('/dependencies').text
        assert 'graph-analyze' not in page and 'graph-auto' not in page and 'graph-add-form' not in page
        # Existing schedule API remains the editing path. Dependencies have not completed, so no work runs.
        result=client.post('/api/tasks/c/scheduled',json={'prompt':'follow-up','depends_on':['a','b'],'service_tier':'default'})
        assert result.status_code == 200
        sid=result.json()['id']
        assert pairs(client.get('/api/dependency-graph').json()) == {('a','b'),('a','c'),('b','c')}
        m.db.set_status('a','queued'); m.db.set_status('a','starting'); m.db.set_status('a','running')
        assert client.get('/api/dependency-graph?project=/repo&search=a').status_code == 200
        assert client.delete(f'/api/tasks/c/scheduled/{sid}').status_code == 200
        assert pairs(client.get('/api/dependency-graph').json()) == {('a','b')}
        for endpoint in ('analyze','settings','edges','candidates/test'):
            assert client.post('/api/dependency-graph/'+endpoint,json={}).status_code == 404
        spawn.assert_not_awaited()
        client_call.assert_not_awaited()


def test_orphan_schedule_reference_is_reported(db):
    row(db,'a')
    db._conn.execute("INSERT INTO scheduled_instruction_dependencies VALUES(999,'a')")
    view=graph_view(db)
    assert not view['edges']
    assert '#999' in view['errors'][0]['message']


def test_completed_schedule_is_labelled_and_cancelled_is_not_projected(db):
    row(db,'a'); row(db,'b')
    active = db.create_scheduled('b','completed follow-up','default',['a'])
    cancelled = db.create_scheduled('b','cancelled follow-up','default',['a'])
    db._conn.execute("UPDATE scheduled_instructions SET status='completed' WHERE id=?",(active['id'],))
    db.cancel_scheduled(cancelled['id'])
    view = graph_view(db)
    assert len(view['edges']) == 1
    assert view['edges'][0]['settings'][0]['status'] == 'completed'
    assert len(view['edges'][0]['settings']) == 1
