"""A small JSON-RPC client for `codex app-server` (newline-delimited JSON over stdio).

One shared server process serves every task: a task is a Codex *thread*, an instruction is a *turn*.
Protocol facts below were checked against codex-cli 0.159.2 (`codex app-server generate-json-schema`):
- the client sends `initialize` then an `initialized` notification;
- requests are {"id","method","params"}, answered by {"id","result"|"error"};
- notifications carry no id; thread-scoped ones carry `params.threadId` (or `params.thread.id`);
- the server may also *request* things from the client (approvals, user input). Those need an answer or the
  turn stalls. With auto review / policy "never" they should not arrive; if one does, it is declined.
"""
import asyncio
import json
from typing import Callable, Optional

from .ssh_agent import child_env

STREAM_LIMIT = 32 * 1024 * 1024
CLOSED = {"method": "__closed__", "params": {}}

# What the GUI would never hand to Codex when it is meant to use the subscription login.
API_KEY_VARS = ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "OPENAI_ORGANIZATION", "OPENAI_PROJECT")

# Answers for requests the server sends to the client: always "no". This GUI has no approval prompt.
_DECLINES = {
    "item/commandExecution/requestApproval": {"decision": "decline"},
    "item/fileChange/requestApproval": {"decision": "decline"},
    "item/permissions/requestApproval": {"permissions": {}},
    "item/tool/requestUserInput": {"answers": {}},
    "mcpServer/elicitation/request": {"action": "decline"},
    "item/tool/call": {"contentItems": [], "success": False},
    "applyPatchApproval": {"decision": "denied"},
    "execCommandApproval": {"decision": "denied"},
}


class AppServerError(Exception):
    """The server answered with an error, or it is gone. `code` is the JSON-RPC error code when there is one.

    `kind` says how it went wrong, for failure classification: "spawn" (cannot start the binary at all), "closed" (the
    process exited or its pipe broke), "timeout" (no answer in time), "rpc" (the server answered with an error).
    """

    def __init__(self, message: str, code: Optional[int] = None, kind: str = ""):
        super().__init__(message)
        self.code = code
        self.kind = kind


def subscription_env(subscription_only: bool, extra: Optional[dict] = None) -> dict:
    """Environment for a codex child. With subscription_only, API-key variables are removed so that Codex can only
    use the stored ChatGPT login; the GUI never falls back to API billing."""
    env = child_env(extra)
    if subscription_only:
        for name in API_KEY_VARS:
            env.pop(name, None)
    return env


def thread_of(params) -> Optional[str]:
    if not isinstance(params, dict):
        return None
    if isinstance(params.get("threadId"), str):
        return params["threadId"]
    thread = params.get("thread")
    return thread["id"] if isinstance(thread, dict) and isinstance(thread.get("id"), str) else None


class AppServerClient:
    def __init__(self, codex_bin: str = "codex", subscription_only: bool = True):
        self.codex_bin = codex_bin
        self.subscription_only = subscription_only
        self.user_agent = ""
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._reader: Optional[asyncio.Task] = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 0
        self._threads: dict[str, asyncio.Queue] = {}
        self._global: list[Callable[[str, dict], None]] = []
        self._start_lock = asyncio.Lock()
        self.stderr_tail: list[str] = []

    # ---------- process ----------

    def build_command(self) -> list[str]:
        return [self.codex_bin, "app-server"]

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def pid(self) -> Optional[int]:
        return self._proc.pid if self.alive else None

    async def start(self) -> None:
        """Spawn and initialize, unless already running. Safe to call concurrently."""
        async with self._start_lock:
            if self.alive:
                return
            self._fail_all("app-server restarted")
            self.stderr_tail = []
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    *self.build_command(), env=subscription_env(self.subscription_only),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    start_new_session=True, limit=STREAM_LIMIT)
            except OSError as e:
                raise AppServerError(f"cannot start codex app-server: {e}", kind="spawn") from e
            self._reader = asyncio.create_task(self._read_loop(self._proc))
            asyncio.create_task(self._drain_stderr(self._proc))
            try:
                result = await self.request("initialize", {"clientInfo": {"name": "codex-gui", "title": "Codex GUI", "version": "1"}},
                                            timeout=30, _starting=True)
            except AppServerError:
                await self.close()
                raise
            self.user_agent = result.get("userAgent", "") if isinstance(result, dict) else ""
            self._send({"method": "initialized"})

    async def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc and proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        if self._reader:
            self._reader.cancel()
            self._reader = None
        self._fail_all("app-server closed")

    # ---------- requests ----------

    def _send(self, obj: dict) -> None:
        if not self.alive or self._proc.stdin is None:
            raise AppServerError("codex app-server is not running", kind="closed")
        try:
            self._proc.stdin.write((json.dumps(obj) + "\n").encode())
        except (BrokenPipeError, ConnectionResetError) as e:
            raise AppServerError(f"codex app-server went away: {e}", kind="closed") from e

    async def request(self, method: str, params: Optional[dict] = None, timeout: Optional[float] = 60,
                      _starting: bool = False):
        if not _starting:
            await self.start()
        self._next_id += 1
        rid = self._next_id
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        msg = {"id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        try:
            self._send(msg)
            try:
                await self._proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as e:
                raise AppServerError(f"codex app-server went away: {e}", kind="closed") from e
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise AppServerError(f"{method} timed out after {timeout:g}s", kind="timeout") from None
        finally:
            self._pending.pop(rid, None)

    # ---------- notifications ----------

    def subscribe(self, thread_id: str) -> asyncio.Queue:
        """Queue of this thread's notifications as (method, params). Ends with CLOSED if the server dies."""
        q = self._threads.get(thread_id)
        if q is None:
            q = self._threads[thread_id] = asyncio.Queue()
        return q

    def unsubscribe(self, thread_id: str) -> None:
        self._threads.pop(thread_id, None)

    def on_global(self, callback: Callable[[str, dict], None]) -> None:
        """Notifications that belong to no thread (account/rateLimits/updated, ...)."""
        self._global.append(callback)

    # ---------- reader ----------

    async def _drain_stderr(self, proc) -> None:
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            self.stderr_tail = (self.stderr_tail + [line.decode(errors="replace").rstrip()])[-20:]

    async def _read_loop(self, proc) -> None:
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    self._dispatch(msg)
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        finally:
            if self._proc is proc:
                tail = " | ".join(self.stderr_tail[-3:])
                self._fail_all("codex app-server exited" + (f": {tail}" if tail else ""))

    def _dispatch(self, msg: dict) -> None:
        method, mid = msg.get("method"), msg.get("id")
        if method is not None and mid is not None:
            self._answer_server_request(mid, method)
        elif method is not None:
            params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
            tid = thread_of(params)
            if tid is not None and tid in self._threads:
                self._threads[tid].put_nowait((method, params))
            elif tid is None:
                for cb in self._global:
                    try:
                        cb(method, params)
                    except Exception:  # a bad listener must not stop the reader
                        pass
        elif mid is not None:
            fut = self._pending.get(mid)
            if fut is not None and not fut.done():
                if "error" in msg:
                    err = msg["error"] if isinstance(msg["error"], dict) else {}
                    fut.set_exception(AppServerError(str(err.get("message", msg["error"])), err.get("code"), kind="rpc"))
                else:
                    fut.set_result(msg.get("result"))

    def _answer_server_request(self, mid, method: str) -> None:
        try:
            if method in _DECLINES:
                self._send({"id": mid, "result": _DECLINES[method]})
            else:
                self._send({"id": mid, "error": {"code": -32601, "message": f"{method} is not supported by Codex GUI"}})
        except AppServerError:
            pass

    def _fail_all(self, reason: str) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(AppServerError(reason, kind="closed"))
        for q in self._threads.values():
            q.put_nowait((CLOSED["method"], {"reason": reason}))
