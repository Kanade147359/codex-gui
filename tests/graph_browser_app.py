"""Isolated browser fixture. All model/process calls are counted and rejected."""
import os
from pathlib import Path
from unittest.mock import AsyncMock

from app.config import Settings
from app.main import create_app


def create_fixture():
    application = create_app(Settings(home=Path(os.environ['GRAPH_FIXTURE_HOME']), backend='exec'))
    m = application.state.manager
    m._dispatch = lambda task_id: False
    m.scheduler._tick = lambda: None
    m.runner.spawn = AsyncMock(side_effect=AssertionError('browser fixtures never launch Codex'))
    ids = ['a0000001','b0000002','c0000003','d0000004','e0000005']
    def add(tid,name,repo):
        if m.db.get_task(tid):return
        m.db.create_task(id=tid,name=name,repository=repo,worktree='/uncreated',branch=tid,base_ref='main',
                         base_sha='abc',prompt='A完了後に実行 同じファイル',last_prompt='A完了後に実行 同じファイル',
                         status='waiting_dependencies',created_at='2026-01-01T00:00:00Z')
    for tid,name in zip(ids,['A 前提成果物','B 並列作業','C 並列作業','D 統合作業 · とても長いタイトルでも省略され詳細で全文を表示','E 独立タスク']):
        add(tid,name,'/fixture/repo-two' if tid==ids[3] else '/fixture/repo-one')
    m.db.replace_dependencies(ids[1],[ids[0]])
    m.db.replace_dependencies(ids[2],[ids[0]])
    schedule=m.db.create_scheduled(ids[3],'BとCの成果を統合する','default',ids[1:3])

    @application.get('/fixture/calls')
    async def calls():return dict(calls=m.runner.spawn.await_count,schedule_id=schedule['id'])

    @application.post('/fixture/status')
    async def status():
        m.db.set_status(ids[0],'queued'); m.db.set_status(ids[0],'starting'); m.db.set_status(ids[0],'running')
        return {'ok':True}

    @application.post('/fixture/stress')
    async def stress():
        for i in range(95):add(f'f{i:07}',f'独立タスク {i}','/fixture/repo-one')
        return {'ok':True}
    return application
