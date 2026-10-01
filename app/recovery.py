"""Failure classification and the retry schedule for automatic recovery.

Only an *unexpected* stop is retried. A stop the user asked for, a quota stop, and anything a retry cannot fix
(authentication, configuration, a bad model, a broken repository, ...) is not. A failure that cannot be classified is
"unknown": it is retried (it may well be transient), but never beyond the task's retry limit.

The classification looks at structured information first (the app-server's codexErrorInfo, JSON-RPC error codes,
how the process ended) and only then at the text of the error, with deliberately narrow patterns.
"""
import re
from dataclasses import dataclass
from typing import Optional

# What Codex is told when a turn is retried. Fixed on purpose: a changing text would change the prompt and could not
# be cached. It never repeats the original instruction (that could do the same work twice); the thread already holds it.
RECOVERY_PROMPT = (
    "Previous turn was interrupted unexpectedly. "
    "Inspect the current worktree and conversation state and continue the task from the current state. "
    "Do not redo work that is already complete. "
    "Before changing anything, run `git status` and `git log` to see what is already done; "
    "never create a commit or push that already exists. "
    "Run the relevant checks before considering the task complete."
)

NO_THREAD_NOTE = "Retry started a new Codex thread because no previous thread ID existed."

RETRYABLE = "retryable"
NON_RETRYABLE = "non_retryable"
UNKNOWN = "unknown"
QUOTA = "quota"  # not retried and does not use up retries: the user resumes it when the usage is available again


@dataclass(frozen=True)
class Failure:
    kind: str        # short slug stored as last_failure_kind
    category: str    # RETRYABLE | NON_RETRYABLE | UNKNOWN | QUOTA
    message: str


@dataclass(frozen=True)
class Decision:
    action: str      # "retry" | "fail" | "quota"
    reason: str      # why: the failure kind, "retry_limit" or "auto_retry_disabled"


# ---------- app-server: codexErrorInfo (CodexErrorInfo in the v2 schema, codex-cli 0.159.2) ----------

CODEX_ERROR_KINDS = {
    # transient on Codex's side or on the network
    "serverOverloaded": ("server_overloaded", RETRYABLE),
    "internalServerError": ("internal_error", RETRYABLE),
    "httpConnectionFailed": ("connection", RETRYABLE),
    "responseStreamConnectionFailed": ("connection", RETRYABLE),
    "responseStreamDisconnected": ("connection", RETRYABLE),
    "responseTooManyFailedAttempts": ("connection", RETRYABLE),
    "flexUnavailable": ("server_overloaded", RETRYABLE),
    # the included usage is used up
    "usageLimitExceeded": ("quota", QUOTA),
    "rateLimitExceeded": ("quota", QUOTA),
    # a retry cannot change these
    "unauthorized": ("auth", NON_RETRYABLE),
    "badRequest": ("bad_request", NON_RETRYABLE),
    "contextWindowExceeded": ("context_window", NON_RETRYABLE),
    "sessionBudgetExceeded": ("budget", NON_RETRYABLE),
    "sandboxError": ("sandbox", NON_RETRYABLE),
    "cyberPolicy": ("policy", NON_RETRYABLE),
    "misalignmentPolicyViolation": ("policy", NON_RETRYABLE),
    "tooManyDenials": ("policy", NON_RETRYABLE),
    "threadRollbackFailed": ("thread", NON_RETRYABLE),
}

# ---------- text patterns (stderr of `codex exec`, error messages) ----------

_TEXT_RULES = [
    ("quota", QUOTA, r"usage limit|rate.?limit|insufficient_quota|exceeded your current quota|\b429\b|too many requests"),
    ("auth", NON_RETRYABLE, r"\b401\b|unauthori[sz]ed|not logged in|please (log ?in|sign in)|codex login|invalid (api key|token|credentials)"
                            r"|authentication (failed|required|error)|token (has )?(expired|been revoked)"),
    ("model", NON_RETRYABLE, r"invalid model|unknown model|unsupported model|model[^\n]{0,60}(not found|does not exist|not supported|not available)"),
    ("config", NON_RETRYABLE, r"invalid (config|configuration)|error (loading|reading|parsing) config|config\.toml|unknown (config )?(key|field)"
                              r"|failed to parse (the )?config"),
    ("args", NON_RETRYABLE, r"unexpected argument|unrecognized (option|argument|subcommand)|invalid value '[^']*' for|required arguments were not provided"),
    ("connection", RETRYABLE, r"connection (reset|refused|closed|aborted|error)|econnreset|etimedout|timed out|timeout|stream (disconnected|closed|error)"
                              r"|error sending request|network (error|is unreachable)|temporary failure in name resolution|\b50[234]\b"
                              r"|service unavailable|bad gateway|overloaded|unexpected eof|tls handshake"),
    ("transient_io", RETRYABLE, r"\beagain\b|\beio\b|resource temporarily unavailable|input/output error|interrupted system call"
                                r"|cannot allocate memory|broken pipe|text file busy|index\.lock"),
    ("internal_error", RETRYABLE, r"internal (server )?error|\bpanic(ked)?\b|\bcrashed\b|segmentation fault|\b500\b"),
]
_COMPILED = [(kind, cat, re.compile(rx, re.I)) for kind, cat, rx in _TEXT_RULES]


def _clip(text: str, limit: int = 500) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def classify_text(text: str) -> Optional[tuple[str, str]]:
    """(kind, category) of the first rule that matches, else None."""
    for kind, category, rx in _COMPILED:
        if rx.search(text or ""):
            return kind, category
    return None


def classify_turn_error(info_kind: str, message: str) -> Failure:
    """A failed turn as the app-server reported it (TurnError: message + codexErrorInfo)."""
    known = CODEX_ERROR_KINDS.get(info_kind)
    if known:
        return Failure(known[0], known[1], _clip(message or info_kind))
    text = classify_text(message)
    if text:
        return Failure(text[0], text[1], _clip(message))
    return Failure("unknown", UNKNOWN, _clip(message or "the turn failed"))


def classify_app_server_error(error: Exception) -> Failure:
    """An AppServerError raised while talking to the app-server (see AppServerError.kind)."""
    message = str(error)
    kind = getattr(error, "kind", "")
    if kind == "spawn":
        return Failure("codex_unavailable", NON_RETRYABLE, _clip(message))
    if kind == "closed":
        return Failure("app_server_exited", RETRYABLE, _clip(message))
    if kind == "timeout":
        return Failure("app_server_timeout", RETRYABLE, _clip(message))
    if re.search(r"no rollout found|thread not found|unknown thread|no such thread", message, re.I):
        return Failure("session_missing", NON_RETRYABLE, _clip(message))
    text = classify_text(message)
    if text:
        return Failure(text[0], text[1], _clip(message))
    if getattr(error, "code", None) in (-32600, -32601, -32602):  # invalid request / method / params
        return Failure("bad_request", NON_RETRYABLE, _clip(message))
    return Failure("unknown", UNKNOWN, _clip(message))


# ---------- `codex exec` ----------

def classify_exit(code: Optional[int], detail: str = "") -> Failure:
    """A `codex exec` process that ended with a non-zero code. `detail` = stderr tail / the turn.failed message."""
    detail = _clip(detail)
    if code is not None and (code < 0 or code in (137, 139, 143)):
        sig = -code if code < 0 else code - 128
        return Failure("process_exited", RETRYABLE, f"Codex process was killed by signal {sig}" + (f": {detail}" if detail else ""))
    text = classify_text(detail)
    if text:
        return Failure(text[0], text[1], detail)
    return Failure("unknown", UNKNOWN, f"codex exited with code {code}" + (f": {detail}" if detail else ""))


def classify_git_error(message: str) -> Failure:
    """Creating the worktree failed. A held lock is transient; everything else is permanent Git state."""
    if re.search(r"index\.lock|\.lock'?: file exists|unable to create .*\.lock|another git process", message, re.I):
        return Failure("transient_io", RETRYABLE, _clip(message))
    return Failure("git_worktree", NON_RETRYABLE, _clip(message))


def process_lost(message: str = "the Codex process disappeared unexpectedly") -> Failure:
    return Failure("process_lost", RETRYABLE, message)


# ---------- decision and schedule ----------

def decide(failure: Failure, *, enabled: bool, retry_count: int, max_retries: int) -> Decision:
    """What to do after a failed run. Quota and permanent failures never retry; the rest retries within the limit."""
    if failure.category == QUOTA:
        return Decision("quota", "quota")
    if failure.category == NON_RETRYABLE:
        return Decision("fail", failure.kind)
    if not enabled:
        return Decision("fail", "auto_retry_disabled")
    if retry_count >= max_retries:
        return Decision("fail", "retry_limit")
    return Decision("retry", failure.kind)


def backoff_seconds(schedule: tuple, retry_number: int) -> float:
    """Wait before retry number `retry_number` (1-based): 10 s, 30 s, 60 s with the default schedule, then 60 s."""
    if not schedule:
        return 0.0
    return float(schedule[min(max(retry_number, 1), len(schedule)) - 1])
