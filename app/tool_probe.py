"""Measuring what Codex really hands to the model, with the real `codex` binary and no model: nothing is assumed.

* `catalog()`: runs `codex exec --ephemeral` against the local fake Responses endpoint and reads the tool catalog out of
  the request (top-level tools) plus `ALL_TOOLS` of the code-mode `exec` tool (every nested tool the model can reach,
  including MCP tools whose declarations are deferred). This is how a tool profile is *verified*.
* `measure_tool_output()`: the size of a huge command output as the model gets it back, for a given
  `tool_output_token_limit`.
* `prompt_input()`: `codex debug prompt-input`, the model-visible static prefix (skills catalog, AGENTS.md chain...).

The probe uses the user's real CODEX_HOME read-only in effect: `--ephemeral` keeps the session out of the history, the model
provider is overridden with `-c` (so no config file is written and no auth is touched), and nothing leaves the machine.
"""
import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .fake_responses import MockResponses, tool_names, tool_output_of
from .tokens import est_tokens

ALL_TOOLS_JS = "text(JSON.stringify(ALL_TOOLS.map(t => t.name)))"
BIG_OUTPUT_JS = ('const r = await tools.exec_command({cmd: "seq 1 400000"}); text(r.output);')
SKILLS_RE = re.compile(r"<skills_instructions>.*?</skills_instructions>", re.S)


def provider_overrides(base_url: str) -> list[str]:
    """`-c` arguments that make Codex talk to the fake endpoint (no auth needed, nothing written to config.toml)."""
    return ["-c", 'model_provider="gui_probe"',
            "-c", f'model_providers.gui_probe={{name="gui_probe", base_url="{base_url}", wire_api="responses", '
                  'requires_openai_auth=false}']


def config_args(overrides: Optional[dict]) -> list[str]:
    """{"a.b": value} -> ["-c", "a.b=<JSON>"...] (a JSON scalar or list is valid TOML)."""
    args: list[str] = []
    for key, value in (overrides or {}).items():
        args += ["-c", f"{key}={json.dumps(value)}"]
    return args


def probe_env(codex_home: Optional[Path] = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("OPENAI_API_KEY", "CODEX_API_KEY")}
    if codex_home is not None:
        env["CODEX_HOME"] = str(codex_home)
    return env


@dataclass
class ProbeResult:
    ok: bool
    error: str = ""
    requests: list = field(default_factory=list)
    returncode: Optional[int] = None

    @property
    def first(self) -> dict:
        return self.requests[0] if self.requests else {}


async def run_probe(codex_bin: str, cwd, overrides: Optional[dict], script: list, *, codex_home: Optional[Path] = None,
                    timeout: float = 90.0, extra_args: Optional[list] = None) -> ProbeResult:
    """One `codex exec` turn against the fake endpoint driven by `script` (see fake_responses). Never raises."""
    server = MockResponses(script).start()
    try:
        cmd = [codex_bin, "exec", "--json", "--ephemeral", "--skip-git-repo-check", "-s", "read-only",
               *provider_overrides(server.base_url), *config_args(overrides), *(extra_args or []), "probe"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=str(cwd), env=probe_env(codex_home), stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                _, err = await asyncio.wait_for(proc.communicate(), timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return ProbeResult(False, f"codex did not finish within {timeout:g}s", list(server.requests))
        except OSError as e:
            return ProbeResult(False, f"cannot run codex: {e}")
        lines = [ln for ln in err.decode(errors="replace").strip().splitlines() if ln.strip()]
        error = "" if proc.returncode == 0 else (" | ".join(lines[-2:])[:400] if lines else f"codex exited with code {proc.returncode}")
        if not error and not server.requests:
            error = "no request reached the model"
        return ProbeResult(proc.returncode == 0 and bool(server.requests), error, list(server.requests), proc.returncode)
    finally:
        server.stop()


def parse_name_list(text: str) -> Optional[list[str]]:
    i = text.find("Output:")
    m = re.search(r"\[.*\]", text[i:] if i >= 0 else text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    return data if isinstance(data, list) and all(isinstance(x, str) for x in data) else None


def group_nested(names: list[str]) -> dict:
    """{"builtin": n, "<mcp server>": n}: mcp__<server>__<tool> is grouped by its server."""
    groups: dict[str, int] = {}
    for n in names:
        key = n.split("__")[1] if n.startswith("mcp__") and n.count("__") >= 2 else "builtin"
        groups[key] = groups.get(key, 0) + 1
    return groups


async def catalog(codex_bin: str, cwd, overrides: Optional[dict] = None, *, codex_home: Optional[Path] = None,
                  timeout: float = 90.0) -> dict:
    """What the model can use under `overrides`: {"ok", "error", "nested_tools", "nested_count", "groups", "top_tools",
    "catalog_bytes", "catalog_tokens_est"}. `nested_tools` comes from ALL_TOOLS, so it counts deferred MCP tools too."""
    res = await run_probe(codex_bin, cwd, overrides, [("exec", ALL_TOOLS_JS), ("text", "ok")], codex_home=codex_home, timeout=timeout)
    out = {"ok": False, "error": res.error, "nested_tools": None, "nested_count": None, "groups": {}, "top_tools": [],
           "catalog_bytes": None, "catalog_tokens_est": None}
    if not res.requests:
        return out
    from .fake_responses import tools_of
    tools = tools_of(res.first)
    blob = json.dumps(tools, ensure_ascii=False)
    out.update(top_tools=tool_names(tools), catalog_bytes=len(blob.encode()), catalog_tokens_est=est_tokens(blob))
    nested = parse_name_list(tool_output_of(res.requests[1])) if len(res.requests) > 1 else None
    if nested is None:
        out["error"] = out["error"] or "could not read ALL_TOOLS from the code-mode tool"
        return out
    out.update(ok=True, error="", nested_tools=nested, nested_count=len(nested), groups=group_nested(nested))
    return out


async def measure_tool_output(codex_bin: str, cwd, limit: Optional[int], *, codex_home: Optional[Path] = None,
                              timeout: float = 120.0) -> dict:
    """The command output a huge `seq` returns to the model under `tool_output_token_limit=limit` (None = Codex default)."""
    overrides = {"tool_output_token_limit": limit} if limit else None
    res = await run_probe(codex_bin, cwd, overrides, [("exec", BIG_OUTPUT_JS), ("text", "ok")], codex_home=codex_home, timeout=timeout)
    if len(res.requests) < 2:
        return {"ok": False, "error": res.error or "the model never received a tool output", "chars": None, "tokens_est": None}
    text = tool_output_of(res.requests[1])
    return {"ok": True, "error": "", "chars": len(text), "tokens_est": est_tokens(text)}


async def prompt_input(codex_bin: str, cwd, overrides: Optional[dict] = None, *, codex_home: Optional[Path] = None,
                       timeout: float = 60.0) -> Optional[list]:
    """`codex debug prompt-input` as a list of messages, or None."""
    try:
        proc = await asyncio.create_subprocess_exec(
            codex_bin, "debug", "prompt-input", *config_args(overrides), "probe", cwd=str(cwd), env=probe_env(codex_home),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        data = json.loads(out)
    except (OSError, ValueError, asyncio.TimeoutError):
        return None
    return data if isinstance(data, list) else None


def message_text(msg: dict) -> str:
    content = msg.get("content")
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content if isinstance(content, str) else ""


def skills_catalog_stats(messages: Optional[list]) -> Optional[dict]:
    """The `<skills_instructions>` block of a prompt: its size and the number of skills listed in it."""
    for msg in messages or []:
        m = SKILLS_RE.search(message_text(msg))
        if m:
            block = m.group(0)
            skills = re.findall(r"^- ([^\n:]+): ", block.split("### Available skills", 1)[-1], re.M)
            return {"chars": len(block), "tokens_est": est_tokens(block), "skills": len(skills), "names": skills}
    return None


def agents_block(messages: Optional[list]) -> Optional[str]:
    """The project-doc text Codex puts in the prompt (the `<INSTRUCTIONS>` of the AGENTS.md message), or None."""
    for msg in messages or []:
        text = message_text(msg)
        if text.startswith("# AGENTS.md instructions") and "<INSTRUCTIONS>" in text:
            return text.split("<INSTRUCTIONS>\n", 1)[1].split("\n</INSTRUCTIONS>", 1)[0]
    return None
