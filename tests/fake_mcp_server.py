"""A tiny MCP server over stdio with N tools, for checking what Codex puts in the model's tool catalog.

Usage: python fake_mcp_server.py [NAME_PREFIX] [COUNT]   (tools: <prefix>_1 ... <prefix>_<COUNT>, each takes {"q": string})
Newline-delimited JSON-RPC, enough of MCP for `initialize`, `tools/list` and `tools/call`.
"""
import json
import sys

PREFIX = sys.argv[1] if len(sys.argv) > 1 else "tool"
COUNT = int(sys.argv[2]) if len(sys.argv) > 2 else 4


def tools() -> list[dict]:
    return [{"name": f"{PREFIX}_{i}", "description": f"Fake tool number {i} of the {PREFIX} server.",
             "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}}} for i in range(1, COUNT + 1)]


def reply(mid, result) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        continue  # notifications (notifications/initialized, ...)
    if method == "initialize":
        reply(mid, {"protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-06-18"),
                    "capabilities": {"tools": {}}, "serverInfo": {"name": PREFIX, "version": "1"}})
    elif method == "tools/list":
        reply(mid, {"tools": tools()})
    elif method == "tools/call":
        reply(mid, {"content": [{"type": "text", "text": f"called {msg['params'].get('name')}"}], "isError": False})
    else:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "unsupported"}}) + "\n")
        sys.stdout.flush()
