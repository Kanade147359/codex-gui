"""Files and artifacts API. Existing task/attachment APIs remain compatible."""
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from .attachments import AttachmentError
from .task_files import FileError, MAX_DOWNLOAD, MAX_PREVIEW, MAX_ZIP_FILES, chunks


def same_origin(request: Request):
    origin = request.headers.get('origin')
    if request.headers.get('sec-fetch-site') == 'cross-site' or (origin and (urlsplit(origin).scheme, urlsplit(origin).netloc) != (request.url.scheme, request.url.netloc)):
        raise HTTPException(403, detail={'message': '同一オリジンから操作してください。', 'code': 'file_origin'})


router = APIRouter(dependencies=[Depends(same_origin)])
HEADERS = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'Content-Security-Policy': "sandbox; default-src 'none'"}


def download(file, name, size=None, media='application/octet-stream'):
    headers = dict(HEADERS, **{'Content-Disposition': "attachment; filename*=UTF-8''" + quote(name, safe='')})
    if size is not None:
        headers['Content-Length'] = str(size)
    return StreamingResponse(chunks(file, size if size is not None else MAX_DOWNLOAD + 1024 * 1024), media_type=media,
                             headers=headers, background=BackgroundTask(file.close))


@router.get('/api/uploads/limits')
def limits(request: Request):
    s = request.app.state.manager.settings
    return dict(max_file_bytes=s.file_max_bytes, max_files=16, max_message_bytes=s.file_message_max_bytes,
                max_store_bytes=s.file_store_max_bytes, max_store_count=s.file_store_max_count,
                max_zip_files=MAX_ZIP_FILES, max_download_bytes=MAX_DOWNLOAD, preview_bytes=MAX_PREVIEW)


@router.post('/api/uploads')
async def upload(request: Request, filename: str = 'input.bin'):
    m = request.app.state.manager
    length = request.headers.get('content-length', '')
    if length.isdigit() and int(length) > m.settings.file_max_bytes:
        raise FileError('ファイルサイズ上限を超えています。', 413)
    try:
        return await m.attachments.upload_file(request.stream(), filename)
    except AttachmentError as e:
        raise FileError(str(e), 413) from e
    except OSError as e:
        raise FileError('ファイルを保存できません。空き容量と権限を確認してください。', 507) from e


class InputSelection(BaseModel):
    attachment_ids: list[str] = Field(max_length=16)
    pending_only: bool = False


@router.post('/api/tasks/{task_id}/inputs')
async def associate(request: Request, task_id: str, body: InputSelection):
    m = request.app.state.manager
    m.get(task_id)
    await run_in_threadpool(m.attachments.resolve, body.attachment_ids, task_id)
    task = m.get(task_id)
    pending = m.db.attach_pending_files(task_id, body.attachment_ids, m.settings)
    if not pending and (body.pending_only or task['status'] in ('waiting_dependencies', 'queued', 'retry_wait')):
        raise FileError('タスクが開始されました。追加指示から添付してください。', 409)
    m.db.claim_files(task_id, body.attachment_ids)
    return dict(inputs=m.attachments.views(body.attachment_ids), pending_turn_updated=pending)


@router.get('/api/tasks/{task_id}/inputs')
def inputs(request: Request, task_id: str):
    m = request.app.state.manager
    m.get(task_id)
    return dict(inputs=m.attachments.views([r['id'] for r in m.db.input_files(task_id)]))


@router.get('/api/tasks/{task_id}/inputs/{input_id}')
def input_download(request: Request, task_id: str, input_id: str):
    m = request.app.state.manager
    m.get(task_id)
    row = m.attachments.get(input_id)
    if row['kind'] != 'file' or row['task_id'] != task_id:
        raise FileError('このタスクの入力ファイルではありません。', 404)
    try:
        file = m.attachments.open_file(row)
    except OSError as e:
        raise FileError('保存済み入力が欠損しています。', 404) from e
    return download(file, row['filename'], row['size'])


@router.get('/api/tasks/{task_id}/files')
def files(request: Request, task_id: str, path: str = '', offset: int = 0, q: str = ''):
    if offset < 0 or offset > 20000 or len(q) > 200:
        raise FileError('検索条件が不正です。')
    return request.app.state.manager.files.list(task_id, path, offset, q)


@router.get('/api/tasks/{task_id}/files/download')
def file_download(request: Request, task_id: str, path: str):
    file, info = request.app.state.manager.files.open(task_id, path)
    return download(file, Path(path).name, info.st_size)


@router.get('/api/tasks/{task_id}/files/preview')
def preview(request: Request, task_id: str, path: str):
    from fastapi.responses import JSONResponse
    return JSONResponse(request.app.state.manager.files.preview(task_id, path), headers=HEADERS)


class Paths(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=100)


@router.post('/api/tasks/{task_id}/files/zip')
def zip_download(request: Request, task_id: str, body: Paths):
    archive = request.app.state.manager.files.zip(task_id, body.paths)
    archive.seek(0, 2)
    size = archive.tell()
    archive.seek(0)
    return download(archive, f'{task_id}-files.zip', size, media='application/zip')


@router.get('/api/tasks/{task_id}/artifacts')
def artifacts(request: Request, task_id: str, path: str = '', q: str = '', offset: int = 0):
    m = request.app.state.manager
    task = m.get(task_id)
    saved = m.db.list_artifacts(task_id)
    if task['worktree_removed'] or task['worktree_pending']:
        return dict(files=[], snapshots=saved, unavailable=True)
    result = m.files.list(task_id, path, offset, q)
    return dict(result, snapshots=saved, unavailable=False)


@router.post('/api/tasks/{task_id}/artifacts')
def register_artifacts(request: Request, task_id: str, body: Paths):
    if len(set(body.paths)) != len(body.paths) or len(body.paths) > 16:
        raise FileError('Artifactは重複なしで16ファイルまでです。')
    m = request.app.state.manager
    return dict(artifacts=[m.artifacts.snapshot(task_id, path) for path in body.paths])


@router.get('/api/tasks/{task_id}/artifacts/{artifact_id}/download')
def snapshot_download(request: Request, task_id: str, artifact_id: str):
    m = request.app.state.manager
    m.get(task_id)
    file, row = m.artifacts.open_snapshot(task_id, artifact_id)
    return download(file, Path(row['relative_path']).name, row['size'])


class ArtifactInputs(BaseModel):
    artifact_ids: list[str] = Field(min_length=1, max_length=16)
    task_id: str | None = None


@router.post('/api/artifacts/inputs')
def artifact_inputs(request: Request, body: ArtifactInputs):
    if len(set(body.artifact_ids)) != len(body.artifact_ids):
        raise FileError('Artifact IDが重複しています。')
    return dict(inputs=request.app.state.manager.artifacts.inputs(body.artifact_ids, body.task_id))
