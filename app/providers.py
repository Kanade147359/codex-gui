"""Execution providers. CLI options and wire events stay behind these adapters.

No API client, billing fallback, or implicit session replacement is used. Discovery
only runs local metadata commands; it never sends a model prompt.
"""
import asyncio
import base64
from dataclasses import asdict, dataclass
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import re
import time
import uuid
from typing import Protocol

from . import recovery
from .codex_runner import STREAM_LIMIT, parse_line, terminate_process
from .appserver import subscription_env
from .completion import SCHEMA_PATH, RESULT_INSTRUCTION, parse_result
from .ctx_config import task_cwd


@dataclass(frozen=True)
class Capabilities:
    resume: bool = True
    images: bool = True
    structured_output: bool = True
    subagents: bool = True
    steering: bool = False
    compaction: bool = False
    sandbox: bool = True
    usage: bool = True
    quota: bool = False
    fast: bool = False
    effort: bool = True


@dataclass(frozen=True)
class Event:
    kind: str
    data: dict


class ProviderError(RuntimeError):
    def __init__(self, kind, message, category=recovery.NON_RETRYABLE):
        super().__init__(message)
        self.failure = recovery.Failure(kind, category, message)


class ExecutionProvider(Protocol):
    id: str
    capabilities: Capabilities
    requires_result: bool

    async def run(self, manager, task_id, log, turn) -> None: ...
    async def describe(self) -> dict: ...
    async def preflight(self, task: dict) -> None: ...
    async def spawn(self, task: dict, session_id: str | None = None): ...
    def input(self, task: dict, prompt: str, session_id=None) -> bytes: ...
    def events(self, value: dict) -> list[Event]: ...
    def summarize(self, value: dict) -> str: ...
    def classify_exit(self, code: int, detail: str) -> recovery.Failure: ...
    async def stop(self, process, grace: float) -> None: ...


class InvalidEnvelope(ValueError):
    pass


def decode_event(line):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise InvalidEnvelope("duplicate provider event field")
            value[key] = item
        return value
    def constant(value):
        raise InvalidEnvelope("non-finite JSON number")
    return json.loads(line, object_pairs_hook=unique, parse_constant=constant)


def session_id(task):
    return task.get("codex_thread_id") if task.get("provider", "codex") == "codex" else task.get("provider_session_id")


def safe_value(value, label):
    if not isinstance(value, str) or len(value) > 256 or value.startswith("-") or any(ord(c) < 32 for c in value):
        raise ProviderError("configuration", f"invalid {label}")
    return value


def claude_env():
    # An allowlist prevents inherited API routes, key helpers, injected NODE_OPTIONS,
    # cloud credentials and nested Claude/Codex process settings reaching this CLI.
    keys = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TERM", "TMPDIR",
            "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "SSH_AUTH_SOCK", "SSH_AGENT_PID",
            "SSL_CERT_FILE", "SSL_CERT_DIR", "CLAUDE_CONFIG_DIR")
    env = {k: os.environ[k] for k in keys if k in os.environ}
    env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] = "1"
    return env


def redact(text):
    text = str(text)
    for key, value in os.environ.items():
        if len(value) >= 8 and any(s in key.upper() for s in ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "AUTHORIZATION")):
            text = text.replace(value, "[redacted]")
    text = re.sub(r'(?i)(bearer\s+)[^\s"\\]+', r'\1[redacted]', text)
    text = re.sub(r'(?i)("(?:api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|password|authorization)"\s*:\s*)"(?:[^"\\]|\\.)*"', r'\1"[redacted]"', text)
    return re.sub(r"\b(?:sk-ant-[\w-]+|sk-[\w-]{16,}|gh[opusr]_[\w-]{12,})\b", "[redacted]", text)


async def metadata(argv, env=None, *, capture_stderr=False):
    proc = await asyncio.create_subprocess_exec(*argv, env=env, stdin=asyncio.subprocess.DEVNULL,
                                               stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE if capture_stderr else asyncio.subprocess.DEVNULL,
                                               start_new_session=True, limit=STREAM_LIMIT)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), 15)
        return proc.returncode, (out + (err or b"")).decode(errors="replace")
    finally:
        if proc.returncode is None:
            await terminate_process(proc, 0.1)
            await proc.wait()


class CodexProvider:
    id = "codex"
    label = "Codex"
    requires_result = False
    missing_session_message = "no thread.started event with a thread_id was seen in this process's output"

    def __init__(self, runner, backend="exec"):
        self.runner = runner
        self.backend = backend
        self.capabilities = Capabilities(steering=backend == "app-server", compaction=backend == "app-server",
                                         quota=backend == "app-server", fast=True)

    async def run(self, manager, task_id, log, turn):
        if manager.uses_app_server:
            await manager._run_app_server_turn(task_id, log, turn)
        else:
            await manager._run_process(task_id, log, turn)

    async def describe(self):
        from .catalog import ModelCatalog
        try:
            code, version = await metadata([self.runner.codex_bin, "--version"], subscription_env(self.runner.subscription_only))
            auth_code, auth = await metadata([self.runner.codex_bin, "login", "status"], subscription_env(self.runner.subscription_only), capture_stderr=True)
            catalog = await ModelCatalog(self.runner.codex_bin).get()
            return dict(id=self.id, available=code == 0, version=version.strip(), authenticated=auth_code == 0,
                        billing="subscription" if "chatgpt" in auth.lower() else "metered" if "api key" in auth.lower() else "unknown",
                        capabilities=asdict(self.capabilities), **catalog)
        except (OSError, asyncio.TimeoutError):
            return dict(id=self.id, available=False, version=None, authenticated=None, billing="unknown",
                        models=[], capabilities=asdict(self.capabilities))

    async def preflight(self, task):
        pass  # Existing exec/app-server subscription checks remain authoritative.

    async def spawn(self, task, session_id=None):
        return await self.runner.spawn(task, session_id)

    def input(self, task, prompt, session_id=None):
        return prompt.encode()

    def events(self, value):
        kind = value.get("type")
        if kind == "thread.started":
            return [Event("session", {"id": value.get("thread_id")})]
        result = []
        if kind in ("turn.started", "turn.completed") or str(kind).startswith("item."):
            result.append(Event("started", {}))
        item = value.get("item") or {}
        if kind == "item.completed" and item.get("type") == "agent_message" and item.get("phase") in (None, "final_answer"):
            result.append(Event("final", {"text": item.get("text", "")}))
        if kind == "turn.completed":
            result.append(Event("usage", {"event": value}))
        if kind in ("turn.failed", "error"):
            error = value.get("error")
            message = error.get("message") if isinstance(error, dict) else value.get("message") or error
            result.append(Event("error", {"message": str(message or "execution failed")}))
        return result

    def summarize(self, value):
        return parse_line(json.dumps(value))["message"]

    def classify_exit(self, code, detail):
        return recovery.classify_exit(code, detail)

    async def stop(self, process, grace):
        await terminate_process(process, grace)


CLAUDE_FLAGS = ("--resume", "--output-format", "--include-partial-messages", "--json-schema", "--input-format",
                "--permission-mode", "--setting-sources", "--settings", "--strict-mcp-config", "--safe-mode",
                "--append-system-prompt")
PERMISSION_MODES = ("dontAsk", "acceptEdits", "plan", "auto", "default")
CLAUDE_SETTINGS = {
    "disableAllHooks": True, "forceLoginMethod": "claudeai", "enabledPlugins": {},
    "sandbox": {"enabled": True, "autoAllowBashIfSandboxed": True, "allowUnsandboxedCommands": False, "failIfUnavailable": True, "excludedCommands": []},
}


def validate_managed_settings(paths=None):
    paths = paths if paths is not None else (Path("/etc/claude-code/managed-settings.json"),
            Path("/Library/Application Support/ClaudeCode/managed-settings.json"))
    for path in paths:
        if not path.exists():
            continue
        try:
            if path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError()
            policy = json.loads(path.read_text())
            if not isinstance(policy, dict):
                raise ValueError()
            sandbox = policy.get("sandbox") or {}
            permission = policy.get("permissions") or {}
            env = policy.get("env") or {}
            if (policy.get("hooks") or policy.get("apiKeyHelper") or policy.get("forceLoginMethod") not in (None, "claudeai")
                or sandbox.get("enabled") is False or sandbox.get("allowUnsandboxedCommands") is True
                or sandbox.get("failIfUnavailable") is False or sandbox.get("excludedCommands")
                or (sandbox.get("filesystem") or {}).get("disabled") is True
                or permission.get("defaultMode") == "bypassPermissions"
                or any(k.startswith(("ANTHROPIC_", "CLAUDE_CODE_USE_")) for k in env)):
                raise ValueError()
        except (OSError, ValueError, TypeError, AttributeError):
            raise ProviderError("configuration", "Claude managed policy cannot satisfy execution isolation/subscription requirements") from None


class ClaudeCodeProvider:
    id = "claude"
    label = "Claude Code"
    requires_result = True
    missing_session_message = "no provider session id was seen in this process's output"
    capabilities = Capabilities()

    def __init__(self, binary="claude", settings=None):
        self.binary = binary
        self.settings = settings or {}
        self._description = None
        self.instructions = ""

    async def run(self, manager, task_id, log, turn):
        await manager._run_process(task_id, log, turn)

    @staticmethod
    def validate_options(options):
        if not isinstance(options, dict) or set(options) - {"permission_mode", "trusted_repository", "timeout_seconds"}:
            raise ProviderError("configuration", "unknown Claude execution option")
        permission = options.get("permission_mode", "dontAsk")
        if permission not in PERMISSION_MODES:
            raise ProviderError("configuration", "unsupported permission mode; bypassPermissions is prohibited")
        if type(options.get("trusted_repository", False)) is not bool:
            raise ProviderError("configuration", "trusted_repository must be boolean")
        timeout = options.get("timeout_seconds", 3600)
        if type(timeout) is not int or not 1 <= timeout <= 86400:
            raise ProviderError("configuration", "timeout_seconds must be 1..86400")
        return dict(permission_mode=permission, trusted_repository=options.get("trusted_repository", False), timeout_seconds=timeout)

    async def describe(self):
        result = dict(id=self.id, available=False, version=None, authenticated=None, billing="unknown", quota=None,
                      models=[], efforts=[], permission_modes=[], capabilities=asdict(self.capabilities), error="")
        try:
            code, version = await metadata([self.binary, "--version"], claude_env())
            _, help_text = await metadata([self.binary, "--help"], claude_env())
            result.update(available=code == 0, version=version.strip())
            parsed_version = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", version)
            if not parsed_version or tuple(map(int, parsed_version.groups())) < (2, 1, 185):
                result["available"] = False
                result["error"] = "Claude Code 2.1.185+ required for the verified sandbox contract"
            result["capabilities"]["effort"] = "--effort" in help_text
            missing = [flag for flag in CLAUDE_FLAGS if flag not in help_text]
            if missing:
                result["error"] = "CLI lacks required capabilities: " + ", ".join(missing)
                result["available"] = False
            # No stable model-list CLI. Offer a manual model id; help/version is the
            # source for supported effort/permission options, never a fabricated list.
            match = re.search(r"--effort\s+<[^>]+>[^\n]*\n?\s*\(([^)]+)\)", help_text)
            if match:
                result["efforts"] = [v.strip() for v in match[1].split(",") if re.fullmatch(r"[a-z]+", v.strip())]
            permission_help = help_text.split("--permission-mode", 1)[-1].split("--plugin", 1)[0]
            result["permission_modes"] = [m for m in PERMISSION_MODES if f'"{m}"' in permission_help]
            code, status = await metadata([self.binary, "auth", "status"], claude_env())
            auth = json.loads(status)
            if not isinstance(auth, dict):
                raise ValueError("invalid CLI authentication status")
            result["authenticated"] = bool(code == 0 and auth.get("loggedIn") is True)
            if (result["authenticated"] and auth.get("authMethod") == "claude.ai"
                    and auth.get("apiProvider") == "firstParty" and auth.get("subscriptionType") in ("pro", "max", "team", "enterprise")):
                result["billing"] = "subscription"
            elif result["authenticated"] and auth.get("authMethod") in ("api_key", "api_key_helper", "third_party"):
                result["billing"] = "metered"
            result["model_source"] = "manual; official CLI model aliases/full IDs; no model discovery command"
        except (OSError, ValueError, asyncio.TimeoutError):
            result["error"] = "CLI metadata/authentication unavailable (no prompt sent)"
        self._description = result
        return result

    async def preflight(self, task):
        options = self.validate_options(json.loads(task.get("provider_options") or "{}"))
        if not options["trusted_repository"]:
            raise ProviderError("repository_untrusted", "Claude non-interactive execution requires explicit repository trust")
        if self.settings.get("billing_mode") != "subscription" or self.settings.get("extra_usage_disabled_confirmed") is not True:
            raise ProviderError("billing_unknown", "Confirm subscription billing and extra usage disabled in Provider settings before execution")
        validate_managed_settings()
        info = await self.describe()
        if not info["available"]:
            raise ProviderError("cli_unavailable", info["error"] or "Claude Code CLI unavailable")
        if not info["authenticated"]:
            raise ProviderError("auth", "Claude Code CLI login required; use claude auth login in a terminal")
        if info["billing"] != "subscription":
            raise ProviderError("billing_unknown", "Claude subscription route cannot be verified; no paid fallback")
        effort = task.get("reasoning_effort", "default")
        if effort not in ("", "default") and (not info["capabilities"]["effort"] or (info["efforts"] and effort not in info["efforts"])):
            raise ProviderError("configuration", "effort is not supported by the installed Claude CLI")
        if options["permission_mode"] not in info["permission_modes"]:
            raise ProviderError("configuration", "permission mode is not supported by the installed Claude CLI")

    def build_command(self, task, session_id=None):
        options = self.validate_options(json.loads(task.get("provider_options") or "{}"))
        cmd = [self.binary, "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
               "--input-format", "stream-json", "--json-schema", SCHEMA_PATH.read_text(),
               "--permission-mode", options["permission_mode"], "--safe-mode", "--setting-sources", "",
               "--settings", json.dumps(CLAUDE_SETTINGS), "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
               "--disable-slash-commands", "--no-chrome"]
        if task.get("model"):
            cmd += ["--model", safe_value(task["model"], "model")]
        if task.get("reasoning_effort") not in (None, "", "default"):
            cmd += ["--effort", safe_value(task["reasoning_effort"], "effort")]
        if not task.get("allow_subagents"):
            cmd += ["--disallowedTools", "Agent,Task"]
        protocol = (RESULT_INSTRUCTION + " Use the StructuredOutput tool when available. "
                    "The final response must be exactly that JSON object, with no preamble, Markdown, or trailing prose. "
                    "Tool success alone does not establish task success.")
        if self.instructions and not session_id:
            protocol += "\n" + self.instructions
        # Every invocation, including resume, needs the same completion protocol.
        cmd += ["--append-system-prompt", protocol]
        if session_id:
            try:
                uuid.UUID(session_id)
            except (ValueError, TypeError, AttributeError):
                raise ProviderError("session_missing", "Claude resume requires the recorded UUID session id") from None
            cmd += ["--resume", session_id]
        return cmd

    async def spawn(self, task, session_id=None):
        return await asyncio.create_subprocess_exec(*self.build_command(task, session_id), cwd=task_cwd(task), env=claude_env(),
                                                   stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                   stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=STREAM_LIMIT)

    def input(self, task, prompt, session_id=None):
        content = [{"type": "text", "text": prompt}]
        for filename in task.get("image_paths", []):
            path = Path(filename)
            media = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(path.suffix.lower())
            if not media:
                raise ProviderError("invalid_attachment", "unsupported Claude image format")
            content.append({"type": "image", "source": {"type": "base64", "media_type": media,
                                                        "data": base64.b64encode(path.read_bytes()).decode()}})
        return (json.dumps({"type": "user", "session_id": session_id or "", "parent_tool_use_id": None, "message": {"role": "user", "content": content}}) + "\n").encode()

    def events(self, value):
        kind = value.get("type")
        if kind == "system" and value.get("subtype") == "init":
            try:
                uuid.UUID(value.get("session_id"))
            except (ValueError, TypeError, AttributeError):
                raise ProviderError("invalid_output", "Claude emitted an invalid UUID session id") from None
            return [Event("session", {"id": value.get("session_id")}), Event("model", {"model": value.get("model")})]
        if kind == "result":
            if value.get("permission_denials"):
                raise ProviderError("permission_required", "Claude tool permission was denied; review permission mode before retry")
            if value.get("is_error") is not False or value.get("subtype") != "success":
                errors = value.get("errors")
                failure = self.classify_exit(1, "\n".join(e for e in errors if isinstance(e, str)) if isinstance(errors, list) else "")
                if failure.kind != "unknown":
                    raise ProviderError(failure.kind, failure.message, failure.category)
                raise ProviderError("provider_error", "Claude execution incomplete: " + str(value.get("subtype", "unknown")))
            raw = value.get("usage")
            keys = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
            usage = None
            if isinstance(raw, dict) and all(type(raw.get(k)) is int and raw[k] >= 0 for k in keys):
                usage = dict(input_tokens=raw["input_tokens"] + raw["cache_read_input_tokens"] + raw["cache_creation_input_tokens"],
                             cached_input_tokens=raw["cache_read_input_tokens"], output_tokens=raw["output_tokens"],
                             cache_write_input_tokens=raw["cache_creation_input_tokens"])
            result = [Event("result", dict(session_id=value.get("session_id"), usage=usage,
                                          raw_usage=raw if isinstance(raw, dict) else None, duration_ms=value.get("duration_ms"),
                                          reported_cost_usd=value.get("total_cost_usd")))]
            structured = value.get("structured_output")
            if isinstance(structured, dict):
                result.append(Event("final", {"text": json.dumps(structured, ensure_ascii=False)}))
            elif "structured_output" not in value:
                # Installed CLI 2.1.185 may return exact Completion JSON as its
                # terminal result without invoking the structured-output tool.
                # Never score assistant/tool text, prose, or malformed envelopes.
                parsed = parse_result(value.get("result"))
                if parsed is not None:
                    result.append(Event("final", {"text": json.dumps(parsed, ensure_ascii=False)}))
            return result
        if kind in ("assistant", "user", "stream_event"):
            result = [Event("started", {})]
            if value.get("parent_tool_use_id"):
                result.append(Event("subagent", {"id": value["parent_tool_use_id"], "status": "running"}))
            content = (value.get("message") or {}).get("content", []) if kind != "stream_event" else []
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") in ("Agent", "Task"):
                        result.append(Event("subagent", {"id": block.get("id"), "status": "running",
                                                         "description": (block.get("input") or {}).get("description", "")}))
                    elif isinstance(block, dict) and block.get("type") == "tool_result":
                        result.append(Event("tool_finished", {"id": block.get("tool_use_id"),
                                                             "status": "failed" if block.get("is_error") else "completed"}))
            return result
        if kind == "system" and value.get("subtype") in ("task_started", "task_progress", "task_notification"):
            return [Event("subagent", {"id": value.get("task_id") or value.get("tool_use_id"),
                                      "status": value.get("status") or value["subtype"],
                                      "description": value.get("description", "")})]
        if kind == "rate_limit_event" and (value.get("rate_limit_info") or {}).get("isUsingOverage") is True:
            return [Event("failure", {"kind": "billing_violation", "category": recovery.NON_RETRYABLE,
                                      "message": "Claude reported overage usage; execution and provider queue stopped"})]
        if kind == "rate_limit_event" and (value.get("rate_limit_info") or {}).get("status") == "rejected":
            return [Event("failure", {"kind": "quota", "category": recovery.QUOTA, "message": "Claude usage limit reached; resume manually when available"})]
        if kind == "system" and value.get("subtype") == "api_error":
            error = value.get("error") or {}
            code = error.get("type") if isinstance(error, dict) else None
            category = recovery.RETRYABLE if code in ("overloaded_error", "api_error") else recovery.NON_RETRYABLE
            if code == "rate_limit_error":
                category = recovery.QUOTA
            return [Event("failure", {"kind": code or "provider_error", "category": category,
                                      "message": "Claude provider error: " + str(code or "unknown")})]
        return []

    def summarize(self, value):
        kind = value.get("type", "event")
        if kind == "stream_event":
            event = value.get("event") or {}
            delta = event.get("delta") or {}
            return str(delta.get("text") or delta.get("partial_json") or event.get("type", "stream"))[:2000]
        if kind in ("assistant", "user"):
            content = (value.get("message") or {}).get("content") or []
            if isinstance(content, str):
                return content[:2000]
            return "\n".join(str(c.get("text") or c.get("content") or (c.get("name", "") + " " + json.dumps(c.get("input", {}))))
                             for c in content if isinstance(c, dict))[:2000]
        # System init may include configuration/account metadata; only show safe fields.
        if kind == "system" and value.get("subtype") == "init":
            return f"session {value.get('session_id', '')}; model {value.get('model', '')}"
        return json.dumps({k: value[k] for k in ("subtype", "status", "description", "result", "usage", "duration_ms", "task_id") if k in value}, ensure_ascii=False)[:2000]

    def classify_exit(self, code, detail):
        if re.search(r"sandbox[^\n]*(?:unavailable|required|failed|not support)|bwrap:", detail, re.I):
            return recovery.Failure("sandbox_unavailable", recovery.NON_RETRYABLE, "Claude sandbox could not be enforced; no unsandboxed fallback")
        if re.search(r"no conversation found|session (?:not found|does not exist)|no session found|unknown session", detail, re.I):
            return recovery.Failure("session_missing", recovery.NON_RETRYABLE, "Claude session is missing; choose a new session explicitly")
        failure = recovery.classify_exit(code, redact(detail))
        return recovery.Failure(failure.kind, failure.category, failure.message.replace("Codex process", "Claude process").replace("codex exited", "Claude exited"))

    async def stop(self, process, grace):
        await terminate_process(process, grace)


class ProviderRegistry:
    def __init__(self, db, settings, codex_runner):
        self.db, self.runtime = db, settings
        self.providers = {"codex": CodexProvider(codex_runner, settings.backend),
                          "claude": ClaudeCodeProvider(settings.claude_bin)}
        self._cache = None
        self._semaphores = {}
        self._cache_at = 0
        self.reload()

    @asynccontextmanager
    async def slot(self, provider):
        limit = self.config(provider)["max_concurrent"]
        if not limit:
            yield
            return
        semaphore = self._semaphores.setdefault((provider, limit), asyncio.Semaphore(limit))
        async with semaphore:
            yield

    def config(self, provider):
        if provider not in self.providers:
            raise ProviderError("configuration", "unknown provider")
        defaults = dict(binary=self.runtime.codex_bin if provider == "codex" else self.runtime.claude_bin,
                        default_model="", default_effort="default", default_subagents=False,
                        permission_mode="dontAsk" if provider == "claude" else "auto_review",
                        max_concurrent=0, billing_mode="unknown", extra_usage_disabled_confirmed=False)
        defaults.update(json.loads(self.db.get_settings().get("provider." + provider, "{}")))
        return defaults

    def save(self, provider, values):
        current = self.config(provider)
        if not isinstance(values, dict) or set(values) - set(current):
            raise ProviderError("configuration", "unknown provider settings")
        current.update(values)
        for key in ("binary", "default_model", "default_effort"):
            safe_value(current[key], key)
        if not current["binary"] or current["binary"].strip() != current["binary"]:
            raise ProviderError("configuration", "binary must be an executable path, without arguments")
        if not re.fullmatch(r"[a-z]{1,16}", current["default_effort"]):
            raise ProviderError("configuration", "invalid default effort")
        for key in ("default_subagents", "extra_usage_disabled_confirmed"):
            if type(current[key]) is not bool:
                raise ProviderError("configuration", f"{key} must be boolean")
        if type(current["max_concurrent"]) is not int or not 0 <= current["max_concurrent"] <= 128:
            raise ProviderError("configuration", "max_concurrent must be 0..128")
        if current["billing_mode"] not in ("unknown", "subscription"):
            raise ProviderError("configuration", "only an explicitly confirmed subscription route is supported")
        if provider == "claude" and current["permission_mode"] not in PERMISSION_MODES:
            raise ProviderError("configuration", "unsupported permission mode")
        if provider == "codex" and current["permission_mode"] not in ("auto_review", "never"):
            raise ProviderError("configuration", "unsupported Codex permission mode")
        self.db.set_setting("provider." + provider, json.dumps(current))
        self.reload()
        return current

    def reload(self):
        self._cache = None
        for name, provider in self.providers.items():
            config = self.config(name)
            if name == "claude":
                provider.binary, provider.settings = config["binary"], config
                self.runtime.claude_bin = config["binary"]
            elif "provider.codex" in self.db.get_settings():
                provider.runner.codex_bin = config["binary"]
                self.runtime.codex_bin = config["binary"]

    def get(self, name):
        if name not in self.providers:
            raise ProviderError("configuration", "unknown provider")
        return self.providers[name]

    async def describe(self):
        if self._cache is None or time.monotonic() - self._cache_at > 30:
            self._cache = await asyncio.gather(*(p.describe() for p in self.providers.values()))
            self._cache_at = time.monotonic()
        results = self._cache
        for info in results:
            info["settings"] = self.config(info["id"])
            info["paused_for_quota"] = self.paused(info["id"])
        return results

    def paused(self, provider):
        return self.db.get_settings().get("provider_paused." + provider) == "1"

    def pause(self, provider, paused=True):
        self.db.set_setting("provider_paused." + provider, "1" if paused else "0")
