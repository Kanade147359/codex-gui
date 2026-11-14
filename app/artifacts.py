"""Immutable, content-addressed snapshots, copied to task-owned input IDs.

Never give a consumer a source worktree path or the shared CAS blob's path.
"""
import hashlib
import json
import os
import threading
import uuid
from pathlib import Path

from .attachments import ID_RE
from .models import now_iso
from .task_files import DIR_FLAGS, FileError, chunks, signature


class ArtifactStore:
    def __init__(self, manager):
        self.manager = manager
        self.db = manager.db
        self.root = manager.settings.home.resolve() / 'artifacts'
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock = threading.RLock()
        for path in self.root.glob('*.part'):
            if ID_RE.fullmatch(path.stem):
                path.unlink(missing_ok=True)
        known = {r['sha256'] for r in self.db.list_artifacts()}
        for path in self.root.glob('*.bin'):
            if len(path.stem) == 64 and all(c in '0123456789abcdef' for c in path.stem) and path.stem not in known:
                path.unlink(missing_ok=True)

    def snapshot(self, source_task_id, path):
        with self.lock:
            settings = self.manager.settings
            source, before = self.manager.files.open(source_task_id, path, settings.file_max_bytes)
            temp = self.root / (uuid.uuid4().hex + '.part')
            digest, size = hashlib.sha256(), 0
            created_blob = None
            temporary_created = False
            try:
                with source, temp.open('xb') as out:
                    temporary_created = True
                    os.chmod(temp, 0o600)
                    for chunk in chunks(source, before.st_size + 1):
                        size += len(chunk)
                        if size > before.st_size:
                            raise FileError('転送中に元ファイルが変更されました。再試行してください。', 409)
                        digest.update(chunk)
                        out.write(chunk)
                    if size != before.st_size or signature(before) != signature(os.fstat(source.fileno())):
                        raise FileError('転送中に元ファイルが変更されました。再試行してください。', 409)
                    # Also detect replacement/rename of the original path while copying.
                    current, current_info = self.manager.files.open(source_task_id, path, settings.file_max_bytes)
                    current.close()
                    if signature(before) != signature(current_info):
                        raise FileError('転送中に元ファイルが置換されました。再試行してください。', 409)
                    out.flush()
                    os.fsync(out.fileno())
                sha = digest.hexdigest()
                rows = self.db.list_artifacts()
                existing = next((r for r in rows if r['source_task_id'] == source_task_id and
                                 r['relative_path'] == path and r['sha256'] == sha), None)
                if existing:
                    self._verify_blob(sha, size)
                    return existing
                known = {r['sha256']: r['size'] for r in rows}
                if len(rows) >= settings.file_store_max_count or (sha not in known and sum(known.values()) + size > settings.file_store_max_bytes):
                    raise FileError('Artifact保存件数または総容量上限に達しました。', 413)
                blob = self.root / f'{sha}.bin'
                if sha not in known:
                    try:
                        os.link(temp, blob)
                        created_blob = blob
                        temp.unlink()
                    except FileExistsError:
                        # An orphan left before a DB commit is never trusted without verification.
                        self._verify_blob(sha, size)
                    os.chmod(blob, 0o400, follow_symlinks=False)
                    directory = os.open(self.root, DIR_FLAGS)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                else:
                    self._verify_blob(sha, size)
                with self.manager.files.root_fd(source_task_id) as (fd, _):
                    revision = self.manager.files.git(fd, 'rev-parse', 'HEAD').decode('ascii', 'replace').strip()
                return self.db.add_artifact(dict(id=uuid.uuid4().hex, source_task_id=source_task_id,
                                                relative_path=path, sha256=sha, size=size, created_at=now_iso(), revision=revision))
            except BaseException:
                if created_blob and not any(r['sha256'] == created_blob.stem for r in self.db.list_artifacts()):
                    created_blob.unlink(missing_ok=True)
                raise
            finally:
                if temporary_created:
                    temp.unlink(missing_ok=True)

    def _open_blob(self, sha):
        if len(sha) != 64 or any(c not in '0123456789abcdef' for c in sha):
            raise FileError('Artifactハッシュが不正です。')
        # Reuse descriptor-based regular-file/hardlink checks for the CAS as well.
        fd = os.open(self.root, DIR_FLAGS)
        try:
            return self.manager.files.open_at(fd, sha + '.bin', self.manager.settings.file_max_bytes)[0]
        finally:
            os.close(fd)

    def _verify_blob(self, sha, expected):
        with self._open_blob(sha) as blob:
            digest, size = hashlib.sha256(), 0
            for chunk in chunks(blob, expected + 1):
                digest.update(chunk)
                size += len(chunk)
            if size != expected or digest.hexdigest() != sha:
                raise FileError('Artifactが欠損または変更されています。', 409)

    def open_snapshot(self, source_task_id, artifact_id):
        row = self.db.get_artifact(artifact_id)
        if row is None or row['source_task_id'] != source_task_id:
            raise FileError('このタスクのArtifactではありません。', 404)
        file = self._open_blob(row['sha256'])
        try:
            before = os.fstat(file.fileno())
            digest = hashlib.sha256()
            size = 0
            for chunk in chunks(file, row['size'] + 1):
                digest.update(chunk)
                size += len(chunk)
            if (size != row['size'] or digest.hexdigest() != row['sha256'] or
                    signature(before) != signature(os.fstat(file.fileno()))):
                raise FileError('Artifactが欠損または変更されています。', 409)
            file.seek(0)
            return file, row
        except BaseException:
            file.close()
            raise

    def input(self, artifact_id, task_id=None):
        return self.inputs([artifact_id], task_id)[0]

    def inputs(self, artifact_ids, task_id=None):
        staged = []
        with self.lock:
            try:
                for aid in artifact_ids:
                    staged.append(self._stage_input(aid, task_id))
                self.db.add_file_attachments(staged, self.manager.settings)
                return self.manager.attachments.views([row['id'] for row in staged])
            except BaseException:
                for row in staged:
                    self.manager.attachments.path(row).unlink(missing_ok=True)
                raise

    def _stage_input(self, artifact_id, task_id=None):
        if not isinstance(artifact_id, str) or not ID_RE.fullmatch(artifact_id):
            raise FileError('Artifact IDが不正です。')
        artifact = self.db.get_artifact(artifact_id)
        if artifact is None:
            raise FileError('Artifactが見つかりません。', 404)
        if task_id:
            self.manager.get(task_id)
        store = self.manager.attachments
        aid = uuid.uuid4().hex
        row = dict(id=aid, filename=Path(artifact['relative_path']).name, extension='bin',
                   media_type='application/octet-stream', width=0, height=0, size=artifact['size'],
                   created_at=now_iso(), kind='file', sha256=artifact['sha256'], task_id=task_id, artifact_id=artifact_id)
        target = store.path(row)
        saved = created = False
        try:
            with self._open_blob(artifact['sha256']) as source, target.open('xb') as out:
                created = True
                os.chmod(target, 0o600)
                digest, size = hashlib.sha256(), 0
                for chunk in chunks(source, artifact['size'] + 1):
                    size += len(chunk)
                    if size > artifact['size']:
                        raise FileError('Artifactサイズが一致しません。', 409)
                    digest.update(chunk)
                    out.write(chunk)
                if size != artifact['size'] or digest.hexdigest() != artifact['sha256']:
                    raise FileError('Artifactが欠損または変更されています。', 409)
                out.flush()
                os.fsync(out.fileno())
            os.chmod(target, 0o400)
            directory = os.open(store.root, DIR_FLAGS)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            saved = True
            return row
        finally:
            if not saved and created:
                target.unlink(missing_ok=True)

    def prepare_requests(self, task_id, turn):
        """After dependency success, before sending the first turn. Progress and pending_turn commit together.

        A restart may leave an unreferenced snapshot/input, but can never send the turn twice or lose a committed binding.
        """
        for request in self.db.artifact_requests(task_id):
            if request['attachment_id']:
                if request['attachment_id'] not in turn.attachment_ids:
                    turn.attachment_ids.append(request['attachment_id'])
                continue
            source = self.manager.get(request['source_task_id'])
            if source['status'] != 'completed' or source['task_outcome'] != 'success' or source['completion_pending']:
                raise FileError('Artifact依存元はまだ成功していません。', 409)
            artifact = self.snapshot(source['id'], request['relative_path'])
            attachment = self.input(artifact['id'], task_id)
            turn.attachment_ids.append(attachment['id'])
            with self.db._lock, self.db._conn:
                self.db._conn.execute('UPDATE artifact_requests SET artifact_id = ?, attachment_id = ? WHERE id = ? AND attachment_id IS NULL',
                                      (artifact['id'], attachment['id'], request['id']))
                self.db._conn.execute('UPDATE tasks SET pending_turn = ? WHERE id = ?', (turn.to_json(), task_id))
                # Initial message is the input manifest persisted for recovery/UI, not a new conversation turn.
                self.db._conn.execute("UPDATE task_messages SET attachment_ids = ? WHERE task_id = ? AND kind = 'initial'",
                                      (json.dumps(turn.attachment_ids), task_id))
