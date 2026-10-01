"""A scripted fake of the OpenAI Responses API, for driving the REAL `codex` binary without any model or quota.

Codex is pointed at it with a custom model provider (see tool_probe.provider_overrides), so what we see is exactly what
Codex would send to the model: the tool catalog (the `additional_tools` input item), the instructions, the AGENTS.md
chain and the (truncated) tool outputs it feeds back. Used by the tool-profile verification and by the integration tests;
no request ever leaves the machine and no quota is spent. Every request is recorded in `requests`.

A step of the script is one model response:
    ("text", "done")                          an assistant message
    ("exec", "js source")                     a call of the `exec` code-mode tool (nested tools via `tools.*`)
    ("call", name, {"args": ...}, namespace)  a plain function call
Each step may carry usage numbers: ("text", "done", {"input": 1000, "cached": 800, "write": 100, "output": 20}).
When the script runs out, every further request is answered with a plain "ok".
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional


def _usage(u: Optional[dict]) -> dict:
    u = u or {}
    inp, cached, write, out = u.get("input", 10), u.get("cached", 0), u.get("write", 0), u.get("output", 2)
    return {"input_tokens": inp, "input_tokens_details": {"cached_tokens": cached, "cache_write_tokens": write},
            "output_tokens": out, "output_tokens_details": {"reasoning_tokens": u.get("reasoning", 0)},
            "total_tokens": inp + out}


def step_usage(step: tuple) -> Optional[dict]:
    pos = 4 if step[0] == "call" else 2
    return step[pos] if len(step) > pos and isinstance(step[pos], dict) else None


def _item(step: tuple, n: int) -> dict:
    kind = step[0]
    if kind == "exec":
        return {"type": "custom_tool_call", "call_id": f"call_{n}", "name": "exec", "input": step[1]}
    if kind == "call":
        item = {"type": "function_call", "call_id": f"call_{n}", "name": step[1], "arguments": json.dumps(step[2] if len(step) > 2 else {})}
        if len(step) > 3 and step[3]:
            item["namespace"] = step[3]
        return item
    return {"type": "message", "role": "assistant", "id": f"msg_{n}", "content": [{"type": "output_text", "text": step[1] if len(step) > 1 else "ok"}]}


class MockResponses:
    def __init__(self, script: Optional[list] = None):
        self.script = list(script or [])
        self.requests: list[dict] = []
        self._lock = threading.Lock()
        self._server: Optional[ThreadingHTTPServer] = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):  # Codex refreshes /models now and then; an empty list keeps its cached catalog
                body = b'{"models": []}'
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("content-length") or 0))
                try:
                    obj = json.loads(body)
                except ValueError:
                    obj = {}
                with owner._lock:
                    owner.requests.append(obj)
                    n = len(owner.requests)
                    step = owner.script.pop(0) if owner.script else ("text", "ok")
                usage = step_usage(step)
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()

                def ev(kind, data):
                    self.wfile.write(f"event: {kind}\ndata: {json.dumps(data)}\n\n".encode())
                    self.wfile.flush()

                ev("response.created", {"type": "response.created", "response": {"id": f"resp_{n}", "status": "in_progress"}})
                ev("response.output_item.done", {"type": "response.output_item.done", "output_index": 0, "item": _item(step, n)})
                ev("response.completed", {"type": "response.completed", "response": {
                    "id": f"resp_{n}", "status": "completed", "usage": _usage(usage)}})

        self._handler = Handler

    def start(self) -> "MockResponses":
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    # ----- what the tests look at -----

    def tools(self, request_index: int = 0) -> list[dict]:
        """The tool catalog of a request: the `tools` of its `additional_tools` input item (or a top-level `tools`)."""
        return tools_of(self.requests[request_index])

    def tool_output(self, request_index: int = 1) -> str:
        """The text of the first tool-call output fed back in a request."""
        return tool_output_of(self.requests[request_index])


def tools_of(body: dict) -> list[dict]:
    for item in body.get("input", []):
        if isinstance(item, dict) and item.get("type") == "additional_tools":
            return item.get("tools", [])
    return body.get("tools", [])


def tool_names(tools: list[dict]) -> list[str]:
    """Flat names: `namespace.name` for namespaced tools."""
    out = []
    for t in tools:
        if t.get("type") == "namespace":
            out += [f"{t['name']}.{x.get('name')}" for x in t.get("tools", [])]
        else:
            out.append(t.get("name") or t.get("type"))
    return out


def tool_output_of(body: dict) -> str:
    for item in body.get("input", []):
        if isinstance(item, dict) and item.get("type") in ("custom_tool_call_output", "function_call_output"):
            out = item.get("output")
            if isinstance(out, list):
                return "".join(p.get("text", "") for p in out if isinstance(p, dict))
            return out if isinstance(out, str) else json.dumps(out)
    return ""
