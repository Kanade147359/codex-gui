"""Preview HTTP gateway and a Unix-socket bridge inside an isolated Agent namespace."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import mimetypes
import os
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

LIMIT = 1024 * 1024
HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "host",
}


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path: Path):
        super().__init__("localhost", timeout=3)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(self.path))


def forward(connection, method, path, body=b"", headers=None):
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        data = response.read(LIMIT + 1)
        if len(data) > LIMIT:
            raise ValueError("API response exceeded preview byte budget")
        return response.status, response.getheaders(), data
    finally:
        connection.close()


class Handler(BaseHTTPRequestHandler):
    # Header values, passwords and query strings are never logged.
    def log_message(self, fmt, *args):
        print(f"{self.command} {urlsplit(self.path).path}", flush=True)

    def send(self, status, data=b"", content_type="application/json", headers=()):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        # Chromium makes form POST Origin opaque under a no-referrer policy.
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")
        for name, value in headers:
            if name.lower() not in HOP_HEADERS | {"content-type", "cache-control"}:
                self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def body(self):
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("chunked requests are not supported by preview")
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 <= length <= LIMIT:
            raise ValueError("request exceeded preview byte budget")
        return self.rfile.read(length)

    def do_GET(self):
        self.handle_request()

    do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = do_GET


class BridgeHandler(Handler):
    def handle_request(self):
        try:
            if not self.path.startswith("/v1/") or self.path.startswith("//"):
                self.send(404)
                return
            headers = {
                k: v for k, v in self.headers.items() if k.lower() not in HOP_HEADERS
            }
            connection = http.client.HTTPConnection(
                "127.0.0.1", self.server.agent_port, timeout=3
            )
            status, response_headers, data = forward(
                connection, self.command, self.path, self.body(), headers
            )
            content_type = next(
                (v for k, v in response_headers if k.lower() == "content-type"),
                "application/json",
            )
            self.send(status, data, content_type, response_headers)
        except (OSError, ValueError, http.client.HTTPException):
            self.send(503, b'{"error":"isolated Agent unavailable"}')


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class ControlHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(2)
        try:
            message = json.loads(self.rfile.readline(8192))
            authorized = hmac.compare_digest(
                message.get("token", ""), self.server.token
            )
            reply = {"id": self.server.ident, "authorized": authorized, "running": True}
            if authorized and message.get("command") == "stop":
                self.server.event.set()
            self.wfile.write(json.dumps(reply).encode() + b"\n")
        except (OSError, ValueError, TypeError):
            return


def backend(entry: Path) -> int:
    """Runs only inside bubblewrap. No host sockets, interfaces or cluster state are mounted."""
    runtime = entry / "runtime"
    binary = entry / "artifacts" / "corriedale"
    data = runtime / "agent-data"
    key = runtime / "operator.key"

    def cli(*args):
        return subprocess.check_output(
            [str(binary), *map(str, args)], text=True
        ).strip()

    # Fresh operator and node identities. Require mode is explicit even for debug builds.
    operator = cli("operator", "init", "--key-file", key)
    identity = json.loads(cli("node", "identity", "--data-directory", data))
    attestation = runtime / "attestation.jws"
    cli(
        "operator",
        "attest-node",
        "--key-file",
        key,
        "--node-id",
        identity["node_id"],
        "--node-public-key",
        identity["node_public_key"],
        "--expires-at-unix-secs",
        int(time.time()) + 86400,
        "--output",
        attestation,
    )
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    config = runtime / "agent.toml"
    config.write_text(
        f'request_auth = "require"\n'
        f"accepted_node_operators = [{json.dumps(operator)}]\n"
        f"node_attestation_file = {json.dumps(str(attestation))}\n"
        f'listen_address = "127.0.0.1:{port}"\n'
        f'advertise_address = "127.0.0.1:{port}"\n'
        f"data_directory = {json.dumps(str(data))}\n"
        f'containerd_socket = "/nonexistent/preview-containerd.sock"\n'
        f"join_addresses = []\nwork_claimer_enabled = false\n"
    )
    agent = subprocess.Popen(
        [str(binary), "agent", "start", "--config", str(config)],
        stdin=subprocess.DEVNULL,
    )
    server = UnixServer(str(runtime / "api.sock"), BridgeHandler)
    os.chmod(runtime / "api.sock", 0o600)
    server.agent_port = port
    event = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: event.set())
    signal.signal(signal.SIGINT, lambda *_: event.set())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        while not event.wait(0.1):
            if agent.poll() is not None:
                print(f"Agent exited: {agent.returncode}", flush=True)
                return 1
    finally:
        server.shutdown()
        server.server_close()
        if agent.poll() is None:
            agent.terminate()
            try:
                agent.wait(timeout=3)
            except subprocess.TimeoutExpired:
                agent.kill()
                agent.wait()
        (runtime / "api.sock").unlink(missing_ok=True)
    return 0


def isolated_command(entry: Path) -> list[str]:
    command = [
        "bwrap",
        "--unshare-user",
        "--unshare-net",
        "--unshare-pid",
        "--unshare-ipc",
        "--die-with-parent",
        "--cap-drop",
        "ALL",
        "--clearenv",
    ]
    for directory in ("/usr", "/bin", "/lib", "/lib64", "/etc"):
        if Path(directory).exists():
            command += ["--ro-bind", directory, directory]
    command += [
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--dir",
        "/preview",
        "--ro-bind",
        str(entry / "artifacts"),
        "/preview/artifacts",
        "--bind",
        str(entry / "runtime"),
        "/preview/runtime",
        "--ro-bind",
        str(Path(__file__).resolve()),
        "/runner.py",
        "--setenv",
        "PATH",
        "/usr/bin:/bin",
        "--setenv",
        "HOME",
        "/tmp",
        "--chdir",
        "/preview",
        "--",
        "/usr/bin/python3",
        "/runner.py",
        "_backend",
        "/preview",
    ]
    return command


def mock(path: str) -> tuple[int, bytes]:
    parsed = urlsplit(path)
    logical = parsed.path.removeprefix("/v1/compat/r1")
    if logical == "/v1/health":
        return 200, b"ok"
    if logical == "/v1/compatibility":
        return 200, json.dumps(
            {
                "node_id": "preview-mock",
                "incarnation": 1,
                "nonce": parse_qs(parsed.query).get("nonce", [""])[0],
                "compatibility": {
                    "revision": 1,
                    "sequence": 1,
                    "contracts": {
                        k: 1
                        for k in (
                            "membership",
                            "operator-api",
                            "work-claims",
                            "reservations",
                            "artifacts-sha256",
                            "signed-records",
                        )
                    },
                },
            }
        ).encode()
    if logical in {
        "/v1/members",
        "/v1/apps",
        "/v1/jobs",
        "/v1/applications",
        "/v1/app-identities",
    }:
        return 200, b"[]"
    return 501, b'{"error":"not implemented by UI mock; use Integrated Preview"}'


class FrontendHandler(Handler):
    def authenticated(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            session = cookie.get("preview_session")
            return bool(
                session
                and hmac.compare_digest(session.value, self.server.auth["session"])
            )
        except Exception:
            return False

    def handle_request(self):
        state = self.server.state
        path = urlsplit(self.path).path
        host = self.headers.get("Host", "")
        local = urlsplit(state["url"]).netloc
        allowed = {
            local,
            local.replace("127.0.0.1", "localhost"),
            state.get("remote_host"),
        }
        if host not in allowed:
            self.send(403, b'{"error":"unapproved preview host"}')
            return
        remote = host == state.get("remote_host")
        origin = ("https://" if remote else "http://") + host
        if (
            self.command not in {"GET", "HEAD", "OPTIONS"}
            and self.headers.get("Origin") != origin
        ):
            self.send(403, b'{"error":"preview Origin check failed"}')
            return
        try:
            if path == "/_preview/health" and self.command == "GET":
                ok, backend_status, smoke, error = True, "MOCK", "NOT_RUN", None
                if state["mode"] == "integrated":
                    try:
                        code, _, body = forward(
                            UnixHTTP(self.server.bridge), "GET", "/v1/health"
                        )
                        identity_code, _, identity = forward(
                            UnixHTTP(self.server.bridge), "GET", "/v1/node-identity"
                        )
                        ok = (
                            code == 200
                            and body.strip() == b"ok"
                            and identity_code == 200
                            and bool(json.loads(identity).get("node_id"))
                        )
                        smoke = "PASS" if ok else "FAIL"
                        backend_status = "RUNNING" if ok else "UNHEALTHY"
                        if not ok:
                            error = "Agent health or node identity smoke failed"
                    except (OSError, ValueError, http.client.HTTPException) as cause:
                        ok, backend_status, smoke, error = (
                            False,
                            "UNAVAILABLE",
                            "FAIL",
                            str(cause),
                        )
                self.send(
                    200 if ok else 503,
                    json.dumps(
                        dict(
                            id=state["id"],
                            ok=ok,
                            backend=backend_status,
                            api_smoke=smoke,
                            last_error=error,
                        )
                    ).encode(),
                )
                return
            if path == "/_preview/login":
                if self.command == "GET":
                    self.send(
                        200,
                        b"<!doctype html><title>Console Preview sign in</title><h1>Console Preview</h1>"
                        b"<p>Use the password file reported by preview start. Existing Agent authentication still applies.</p>"
                        b'<form method="post"><label>Password <input name="password" type="password" required autocomplete="current-password"></label>'
                        b"<button>Sign in</button></form>",
                        "text/html; charset=utf-8",
                    )
                    return
                if self.command == "POST":
                    password = parse_qs(self.body().decode()).get("password", [""])[0]
                    if not hmac.compare_digest(
                        hashlib.sha256(password.encode()).hexdigest(),
                        self.server.auth["password_sha256"],
                    ):
                        self.send(401, b'{"error":"invalid preview password"}')
                        return
                    cookie = f"preview_session={self.server.auth['session']}; Path=/; HttpOnly; SameSite=Strict"
                    if remote:
                        cookie += "; Secure"
                    self.send(303, headers=[("Location", "/"), ("Set-Cookie", cookie)])
                    return
            if not self.authenticated():
                if self.command in {"GET", "HEAD"} and not path.startswith("/v1/"):
                    self.send(303, headers=[("Location", "/_preview/login")])
                else:
                    self.send(401, b'{"error":"preview authentication required"}')
                return
            if path.startswith("/v1/"):
                if state["mode"] == "ui":
                    if self.command not in {"GET", "HEAD"}:
                        self.send(405, b'{"error":"UI mock is read-only"}')
                        return
                    code, body = mock(self.path)
                    self.send(code, body)
                else:
                    # Forward the caller's Agent authorization unchanged. Never inject signatures.
                    headers = {
                        k: v
                        for k, v in self.headers.items()
                        if k.lower() not in HOP_HEADERS
                    }
                    # Keep any existing application cookies; remove only our additional gate cookie.
                    headers["Cookie"] = "; ".join(
                        c.strip()
                        for c in headers.get("Cookie", "").split(";")
                        if c.strip() and not c.strip().startswith("preview_session=")
                    )
                    code, response_headers, body = forward(
                        UnixHTTP(self.server.bridge),
                        self.command,
                        self.path,
                        self.body(),
                        headers,
                    )
                    content_type = next(
                        (v for k, v in response_headers if k.lower() == "content-type"),
                        "application/json",
                    )
                    self.send(code, body, content_type, response_headers)
                return
            if self.command not in {"GET", "HEAD"}:
                self.send(405)
                return
            relative = unquote(path).lstrip("/")
            dist = self.server.dist
            target = (dist / relative).resolve()
            if not target.is_relative_to(dist) or "\x00" in relative:
                self.send(403)
                return
            if not target.is_file():
                if Path(relative).suffix:
                    self.send(404)
                    return
                target = dist / "index.html"
            self.send(
                200,
                target.read_bytes(),
                mimetypes.guess_type(target)[0] or "application/octet-stream",
            )
        except (OSError, ValueError, http.client.HTTPException):
            self.send(503, b'{"error":"preview request failed; inspect logs"}')


def frontend(entry: Path, fd: int) -> int:
    state = json.loads((entry / "manifest.json").read_text())
    child = None
    event = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: event.set())
    signal.signal(signal.SIGINT, lambda *_: event.set())
    try:
        controller = UnixServer(str(entry / "runtime" / "ctl"), ControlHandler)
        os.chmod(entry / "runtime" / "ctl", 0o600)
        controller.token = json.loads((entry / "auth.json").read_text())["session"]
        controller.ident = state["id"]
        controller.event = event
        threading.Thread(target=controller.serve_forever, daemon=True).start()
        with (entry / "logs" / "backend.log").open("a") as log:
            if state["mode"] == "integrated":
                child = subprocess.Popen(
                    isolated_command(entry),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                )
            else:
                log.write("UI Preview: read-only mock; no Agent launched\n")
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), FrontendHandler, bind_and_activate=False
        )
        server.socket.close()
        server.socket = socket.socket(fileno=fd)
        server.server_address = server.socket.getsockname()
        server.state = state
        server.auth = json.loads((entry / "auth.json").read_text())
        server.dist = (entry / "artifacts" / "console").resolve()
        server.bridge = entry / "runtime" / "api.sock"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        print(f"Preview {state['id']} {state['url']}", flush=True)
        while not event.wait(0.1):
            if child and child.poll() is not None:
                print(f"isolated Backend exited: {child.returncode}", flush=True)
                return 1
    finally:
        if "server" in locals():
            server.shutdown()
            server.server_close()
        if child and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=4)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        if "controller" in locals():
            controller.shutdown()
            controller.server_close()
            (entry / "runtime" / "ctl").unlink(missing_ok=True)
        (entry / "runtime" / "api.sock").unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    if sys.argv[1] == "_backend":
        raise SystemExit(backend(Path(sys.argv[2])))
    raise SystemExit(frontend(Path(sys.argv[1]), int(sys.argv[2])))
