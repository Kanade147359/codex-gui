"""Opt-in real subscription model, isolated GUI DB/repositories/threads/CODEX_HOME.

CODEX_GUI_REAL_FILES=1 pytest tests/test_real_files.py -q
Only auth/model metadata are copied; never use the production GUI or its thread state.
"""
import asyncio
import hashlib
import os
from pathlib import Path
import shutil
import uuid

import pytest

from app.task_manager import TaskManager
from app.database import Database
from conftest import wait_for

pytestmark=pytest.mark.skipif(os.environ.get('CODEX_GUI_REAL_FILES')!='1',reason='opt-in real subscription file test')


def test_real_model_reads_upload_then_artifact_and_resumes(settings,db,git_repo,tmp_path,monkeypatch,request):
    auth=Path(os.environ.get('CODEX_HOME','~/.codex')).expanduser()/'auth.json'
    if not auth.is_file(): pytest.skip('No file-based subscription auth for isolated CODEX_HOME')
    home=tmp_path/'isolated-codex'; home.mkdir(mode=0o700)
    request.addfinalizer(lambda: shutil.rmtree(home, ignore_errors=True))
    shutil.copy2(auth,home/'auth.json')
    cache=auth.parent/'models_cache.json'
    if cache.is_file(): shutil.copy2(cache,home/'models_cache.json')
    monkeypatch.setenv('CODEX_HOME',str(home))
    runtime=tmp_path/'runtime'; runtime.mkdir(mode=0o700)
    monkeypatch.setenv('TMPDIR',str(runtime)); monkeypatch.setenv('XDG_RUNTIME_DIR',str(runtime))
    settings.backend='app-server'; settings.codex_bin=os.environ.get('CODEX_BIN','codex')
    m=TaskManager(settings,db)
    data=('{"verification":"' + uuid.uuid4().hex + '"}').encode()
    expected=hashlib.sha256(data).hexdigest()
    prompt='Use a shell tool to read the attached local input file. Copy its exact bytes to verification.json in the current worktree. Do not copy any other files. Then return the required SUCCESS JSON.'
    async def stream(): yield data
    async def idle(tid): return await wait_for(lambda:m.get(tid)['status'] not in ('queued','starting','running','waiting_dependencies') and m.get(tid),timeout=240)
    async def scenario():
        nonlocal m
        reopened = None
        m.scheduler.start(.05)
        try:
            a=await m.attachments.upload_file(stream(),'verification-input.json')
            source=await m.create_task(repository=str(git_repo),prompt=prompt,attachment_ids=[a['id']],
                                       model=os.environ.get('CODEX_GUI_REAL_MODEL','gpt-6.1-sol'),reasoning_effort='low',auto_retry=False,
                                       completion_contract={'required_paths':['verification.json'],'require_any_change':True})
            target=await m.create_task(repository=str(git_repo),prompt=prompt,artifact_refs=[{'source_task_id':source['id'],'path':'verification.json'}],
                                       model=os.environ.get('CODEX_GUI_REAL_MODEL','gpt-6.1-sol'),reasoning_effort='low',auto_retry=False,
                                       completion_contract={'required_paths':['verification.json'],'require_any_change':True})
            a_done=await idle(source['id']); assert a_done['task_outcome']=='success',a_done['status_detail']+' '+a_done['outcome_reason']
            b_done=await idle(target['id']); assert b_done['task_outcome']=='success',b_done['status_detail']+' '+b_done['outcome_reason']
            assert hashlib.sha256((Path(b_done['worktree'])/'verification.json').read_bytes()).hexdigest()==expected
            (Path(a_done['worktree'])/'verification.json').write_bytes(b'changed after snapshot')
            await m.delete_worktree(source['id'],force=True)
            await m.shutdown()
            db.close()
            reopened = Database(settings.db_path)
            m = TaskManager(settings, reopened)
            m.recover()
            await m.send_instruction(target['id'],'Read the same attached input path from the preceding instruction and write its SHA-256 hex digest to reread.sha256. Return SUCCESS only after checking it.')
            resumed=await idle(target['id'])
            assert resumed['codex_thread_id']==b_done['codex_thread_id']
            assert resumed['task_outcome']=='success',resumed['outcome_reason']
            assert (Path(resumed['worktree'])/'reread.sha256').read_text().strip()==expected
        finally:
            await m.shutdown()
            if reopened is not None:
                reopened.close()
    asyncio.run(scenario())
