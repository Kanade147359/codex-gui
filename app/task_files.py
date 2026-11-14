"""Task-scoped reads. Descriptors stay pinned from validation through streaming.

Linux/openat O_NOFOLLOW is required: no resolve-then-reopen fallback.
"""
import json
import os
import re
import stat
import subprocess
import tempfile
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .models import worktree_path

MAX_DOWNLOAD = 512 * 1024 * 1024
MAX_ZIP_FILES = 100
MAX_PREVIEW = 256 * 1024
MAX_SCAN = 20000
MAX_LIST = 1000
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
TEXT_SUFFIXES = {'.json', '.jsonl', '.md', '.markdown', '.txt', '.log', '.csv', '.yaml', '.yml', '.toml', '.xml', '.py', '.js', '.ts', '.css', '.html', '.sh', '.rs', '.go', '.c', '.h', '.sql'}
ARTIFACT_SUFFIXES = {'.json', '.jsonl', '.md', '.markdown', '.log', '.txt', '.csv', '.pdf', '.zip', '.tar', '.gz'}


class FileError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def parts(path, allow_empty=False):
    if allow_empty and path == '':
        return []
    if not isinstance(path, str) or not path or path.startswith('/') or '\\' in path or len(path) > 4096:
        raise FileError('相対パスを指定してください。')
    result = path.split('/')
    for name in result:
        lower = name.lower()
        if (name in ('', '.', '..') or name.startswith('.') or ':' in name or
                any(ord(c) < 32 or ord(c) == 127 for c in name) or
                lower in {'auth.json', 'credentials', 'credentials.json', 'secrets.json', 'secrets.yaml', 'id_rsa', 'id_ed25519'} or
                lower.endswith(('.pem', '.key', '.p12', '.pfx', '.keystore'))):
            raise FileError('非公開ファイルまたは不正なパスです。', 403)
    return result


def signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def chunks(file, limit=MAX_DOWNLOAD):
    """Stream the already-open file. The response background task also closes on disconnect."""
    remaining = limit
    while remaining:
        chunk = file.read(min(1024 * 1024, remaining))
        if not chunk:
            return
        remaining -= len(chunk)
        yield chunk


class TaskFiles:
    def __init__(self, manager):
        self.manager = manager
        self.root = manager.settings.worktrees_dir.resolve()

    def declared_paths(self, task):
        """Candidate hints only, never permission to publish or snapshot a file.

        Include paths linked/backticked by Codex in a bounded tail of assistant logs.
        Only existing, safely browsable worktree files can become candidates.
        """
        declared = set(json.loads(task['completion_contract'] or '{}').get('required_paths', []))
        try:
            with self.manager.log_path(task['id']).open('rb') as log:
                log.seek(0, os.SEEK_END)
                log.seek(max(0, log.tell() - 256 * 1024))
                tail = log.read(256 * 1024)
            for line in tail.splitlines():
                try:
                    entry = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if 'agent' not in entry.get('type', '').lower():
                    continue
                message = entry.get('message', '')[:32000]
                for match in re.finditer(r'`([^`\n]{1,1024})`|\]\(([^)\n]{1,1024})\)', message):
                    path = (match.group(1) or match.group(2)).removeprefix(task['worktree'] + '/')
                    path = re.sub(r':\d+(?::\d+)?$', '', path)
                    try:
                        parts(path)
                        declared.add(path)
                    except FileError:
                        pass
        except OSError:
            pass
        return declared

    @contextmanager
    def root_fd(self, task_id):
        task = self.manager.get(task_id)
        expected = worktree_path(self.root, task['repository'], task_id)
        if Path(task['worktree']).absolute() != expected:
            raise FileError('タスクのworktreeパスが一致しません。', 403)
        if task['worktree_removed'] or task['worktree_pending']:
            raise FileError('worktreeは未作成または削除済みです。保存済み入力・Artifactは引き続き利用できます。', 410)
        fd = None
        try:
            fd = os.open(self.root, DIR_FLAGS)
            for name in expected.relative_to(self.root).parts:
                child = os.open(name, DIR_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = child
            yield fd, task
        except OSError as e:
            raise FileError('worktreeまたはファイルに安全にアクセスできません。', 404) from e
        finally:
            if fd is not None:
                os.close(fd)

    def open_at(self, root, path, limit=MAX_DOWNLOAD):
        names = parts(path)
        parent = os.dup(root)
        try:
            for name in names[:-1]:
                child = os.open(name, DIR_FLAGS, dir_fd=parent)
                os.close(parent)
                parent = child
            fd = os.open(names[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
                os.close(fd)
                raise FileError('通常ファイル以外、ハードリンク、またはサイズ上限超過のため取得できません。', 413)
            return os.fdopen(fd, 'rb'), info
        except OSError as e:
            raise FileError('ファイルが存在しないか、安全に読み取れません。', 404) from e
        finally:
            os.close(parent)

    def open(self, task_id, path, limit=MAX_DOWNLOAD):
        with self.root_fd(task_id) as (fd, _):
            return self.open_at(fd, path, limit)

    @staticmethod
    def git(fd, *args):
        # Git only reads metadata; a pinned root avoids a path swap before subprocess execution.
        with tempfile.TemporaryFile() as output:
            try:
                result = subprocess.run(['git', '--no-optional-locks', '-C', f'/proc/self/fd/{fd}', *args],
                                        pass_fds=(fd,), stdout=output, stderr=subprocess.DEVNULL, timeout=10)
                if result.returncode:
                    return b''
                output.seek(0)
                return output.read(2 * 1024 * 1024)
            except (OSError, subprocess.TimeoutExpired):
                return b''

    def list(self, task_id, path='', offset=0, query=''):
        names = parts(path, allow_empty=True)
        with self.root_fd(task_id) as (root, task):
            changed = set(self.git(root, 'diff', '--name-only', '-z', task['base_sha'], '--').decode('utf-8', 'replace').split('\0'))
            changed.update(self.git(root, 'ls-files', '--others', '--exclude-standard', '-z').decode('utf-8', 'replace').split('\0'))
            explicit = self.declared_paths(task)
            files, scanned, truncated = [], 0, False

            def add(relative, info):
                if not query or query.casefold() in relative.casefold():
                    files.append(dict(path=relative, filename=Path(relative).name, size=info.st_size,
                                      modified_at=datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(),
                                      changed=relative in changed, declared=relative in explicit,
                                      category='artifact' if relative in explicit or (relative in changed and Path(relative).suffix.lower() in ARTIFACT_SUFFIXES) else 'source',
                                      preview=Path(relative).suffix.lower() in TEXT_SUFFIXES))

            def walk(fd, prefix, depth=0):
                nonlocal scanned, truncated
                if depth > 32:
                    truncated = True
                    return
                with os.scandir(fd) as entries:
                    for entry in entries:
                        scanned += 1
                        if scanned > MAX_SCAN:
                            truncated = True
                            return
                        relative = prefix + entry.name
                        try:
                            parts(relative)
                            info = entry.stat(follow_symlinks=False)
                            if stat.S_ISDIR(info.st_mode):
                                # Hidden metadata/secrets and large dependency/cache trees aren't browsable.
                                if entry.name in ('node_modules', '__pycache__', 'venv'):
                                    continue
                                child = os.open(entry.name, DIR_FLAGS, dir_fd=fd)
                                try:
                                    walk(child, relative + '/', depth + 1)
                                finally:
                                    os.close(child)
                            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                                add(relative, info)
                        except (FileError, OSError):
                            continue  # files can disappear or be replaced while the task runs
                        if truncated:
                            return

            folder = os.dup(root)
            try:
                for i, name in enumerate(names):
                    try:
                        child = os.open(name, DIR_FLAGS, dir_fd=folder)
                    except NotADirectoryError:
                        if i != len(names) - 1:
                            raise FileError('フォルダが見つかりません。', 404)
                        file, info = self.open_at(root, path)
                        file.close()
                        add(path, info)
                        break
                    os.close(folder)
                    folder = child
                else:
                    walk(folder, path + '/' if path else '')
            finally:
                os.close(folder)
            files.sort(key=lambda r: (not r['declared'], not r['changed'], r['path'].casefold()))
            return dict(files=files[offset:offset + MAX_LIST], total=len(files), truncated=truncated,
                        next_offset=offset + MAX_LIST if len(files) > offset + MAX_LIST else None,
                        storage='worktree', inputs=self.manager.attachments.views([r['id'] for r in self.manager.db.input_files(task_id)]))

    def preview(self, task_id, path):
        if Path(path).suffix.lower() not in TEXT_SUFFIXES:
            raise FileError('この形式はプレビューできません。', 415)
        file, info = self.open(task_id, path)
        with file:
            data = file.read(MAX_PREVIEW + 1)
            if signature(info) != signature(os.fstat(file.fileno())):
                raise FileError('読み取り中に変更されました。再試行してください。', 409)
        if b'\0' in data:
            raise FileError('バイナリファイルはプレビューできません。', 415)
        try:
            text = data[:MAX_PREVIEW].decode('utf-8', errors='strict' if len(data) <= MAX_PREVIEW else 'replace')
        except UnicodeDecodeError as e:
            raise FileError('UTF-8テキストではありません。', 415) from e
        return dict(text=text, truncated=len(data) > MAX_PREVIEW, size=info.st_size, limit=MAX_PREVIEW)

    def zip(self, task_id, paths):
        if not paths or len(paths) > MAX_ZIP_FILES or len(set(paths)) != len(paths):
            raise FileError(f'ZIPは重複なしで1〜{MAX_ZIP_FILES}ファイルを選択してください。')
        archive = tempfile.TemporaryFile()
        try:
            total = 0
            with self.root_fd(task_id) as (fd, _), zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as out:
                for path in paths:
                    file, before = self.open_at(fd, path)
                    with file:
                        total += before.st_size
                        if total > MAX_DOWNLOAD:
                            raise FileError('ZIPの合計容量上限512 MiBを超えています。', 413)
                        size = 0
                        with out.open(path, 'w', force_zip64=True) as dest:
                            for chunk in chunks(file, before.st_size + 1):
                                size += len(chunk)
                                if size > before.st_size:
                                    raise FileError('ZIP生成中に変更されました。再試行してください。', 409)
                                dest.write(chunk)
                        if size != before.st_size or signature(before) != signature(os.fstat(file.fileno())):
                            raise FileError('ZIP生成中に変更されました。再試行してください。', 409)
            archive.seek(0)
            return archive
        except BaseException:
            archive.close()
            raise
