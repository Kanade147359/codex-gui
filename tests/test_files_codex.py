"""Installed Codex app-server + real sandbox/tools + local scripted Responses, no model or quota."""
import asyncio
import hashlib
import json
from pathlib import Path
import shlex
import shutil
import sys

import pytest

from conftest import wait_for
from test_ctx_e2e import stack, finished  # noqa: F401

pytestmark = pytest.mark.skipif(shutil.which('codex') is None, reason='installed Codex required')


def script_for(mock, path, expected):
    code = 'from pathlib import Path; import hashlib; p=Path(' + repr(str(path)) + '); b=p.read_bytes(); assert hashlib.sha256(b).hexdigest()==' + repr(expected) + '; Path("verification.json").write_bytes(b); print(hashlib.sha256(b).hexdigest())'
    cmd = shlex.quote(sys.executable) + ' -c ' + shlex.quote(code)
    mock.script = [('exec', 'const r = await tools.exec_command(' + json.dumps({'cmd':cmd}) + '); text(r.output);'),
                   ('text', '{"status":"SUCCESS","reason":"Read input file and wrote verification.json"}')]


def test_installed_app_server_reads_upload_and_snapshot_on_same_thread(git_repo, stack, tmp_path, monkeypatch):
    runtime = tmp_path / "private-runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("TMPDIR", str(runtime))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    m, mock, make = stack
    data = b'{"opaque":"This payload must never appear in the prompt: file-transfer-8c97"}'
    digest = hashlib.sha256(data).hexdigest()
    async def stream():
        yield data
    async def scenario():
        try:
            upload = await m.attachments.upload_file(stream(), 'verification-input.json')
            path = m.attachments.path(m.attachments.get(upload['id']))
            script_for(mock, path, digest)
            source = await make(git_repo, prompt='Read the attached file and copy it to verification.json.', attachment_ids=[upload['id']])
            done = await finished(m, source['id'])
            assert done['task_outcome'] == 'success', done['outcome_reason']
            assert (Path(done['worktree'])/'verification.json').read_bytes() == data
            assert 'file-transfer-8c97' not in json.dumps(mock.requests[0])
            assert str(path) in json.dumps(mock.requests[0])
            artifact = m.artifacts.snapshot(source['id'], 'verification.json')
            attachment = m.artifacts.input(artifact['id'])
            transferred_path = m.attachments.path(m.attachments.get(attachment['id']))
            script_for(mock, transferred_path, digest)
            target = await make(git_repo, prompt='Read attached snapshot', attachment_ids=[attachment['id']])
            target_done = await finished(m, target['id'])
            assert target_done['task_outcome'] == 'success', target_done['outcome_reason']
            assert (Path(target_done['worktree'])/'verification.json').read_bytes() == data
            thread = target_done['codex_thread_id']
            await m.delete_worktree(source['id'], force=True)
            script_for(mock, transferred_path, digest)
            await m.send_instruction(target['id'], 'Read the snapshot again', attachment_ids=[attachment['id']])
            resumed = await finished(m, target['id'])
            assert resumed['codex_thread_id'] == thread
            assert resumed['task_outcome'] == 'success'
            assert (Path(resumed['worktree'])/'verification.json').read_bytes() == data
            # Direct source path is metadata only: the tool call reads the consumer's input copy.
            assert m.attachments.file_prompt('', [attachment['id']]).find('source_task_id') >= 0
        finally:
            await m.shutdown()
    asyncio.run(scenario())
