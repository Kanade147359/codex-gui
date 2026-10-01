"""Runtime settings. Everything can be overridden by environment variables."""
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class Settings:
    home: Path
    codex_bin: str = "codex"
    # 0 = unlimited. Tasks beyond the limit wait in the "queued" status.
    max_concurrent: int = 0
    # Seconds between SIGTERM and SIGKILL when stopping a task.
    stop_grace_seconds: float = 10.0
    # Seconds between background git-summary refreshes while a task runs.
    git_refresh_seconds: float = 5.0
    # "app-server": one shared `codex app-server`, a Codex thread per task, turn/start per instruction.
    # "exec": the older `codex exec --json` / `codex exec resume` processes (no rate limits, no steering, no compaction).
    backend: str = "app-server"
    # Refuse to run when Codex is not signed in with ChatGPT (subscription) auth, and never hand API keys to Codex.
    subscription_only: bool = True
    # Context Guard: warn when a thread's context reaches this share of the model's window.
    context_warn_percent: int = 80
    # The rate-limit snapshot shown on the dashboard is re-read from Codex at most this often (seconds).
    rate_limit_cache_seconds: float = 30.0
    # Rate-limit history rows from push notifications are written at most this often (seconds).
    rate_limit_history_seconds: float = 300.0
    # Automatic recovery of a task that stopped unexpectedly. New tasks start with these (the form can change them).
    default_auto_retry: bool = True
    default_max_retries: int = 3
    # Seconds to wait before retry 1, 2, 3, ... (the last value is used for any later retry). Never retried back to back.
    retry_backoff_seconds: tuple = (10.0, 30.0, 60.0)
    # How often the scheduler looks at waiting / queued / retry_wait tasks (it also wakes up on every status change).
    scheduler_interval_seconds: float = 2.0
    # Login. from_env() turns it on by default; the bare dataclass default is off so library / test use stays open.
    auth_enabled: bool = False
    session_hours: float = 168.0
    # Session cookie "Secure" flag: None = decide per request (https => Secure), True / False = always / never.
    cookie_secure: Optional[bool] = None

    @classmethod
    def from_env(cls) -> "Settings":
        home = Path(os.environ.get("CODEX_GUI_HOME", "~/.local/share/codex-gui")).expanduser()
        return cls(
            home=home,
            codex_bin=os.environ.get("CODEX_BIN", "codex"),
            max_concurrent=int(os.environ.get("CODEX_GUI_MAX_CONCURRENT", "0")),
            backend=os.environ.get("CODEX_GUI_BACKEND", "app-server"),
            subscription_only=os.environ.get("CODEX_GUI_SUBSCRIPTION_ONLY", "1") != "0",
            context_warn_percent=int(os.environ.get("CODEX_GUI_CONTEXT_WARN_PERCENT", "80")),
            default_auto_retry=os.environ.get("CODEX_GUI_AUTO_RETRY", "1") != "0",
            default_max_retries=int(os.environ.get("CODEX_GUI_MAX_RETRIES", "3")),
            retry_backoff_seconds=tuple(float(x) for x in os.environ.get("CODEX_GUI_RETRY_BACKOFF", "10,30,60").split(",") if x.strip()),
            scheduler_interval_seconds=float(os.environ.get("CODEX_GUI_SCHEDULER_INTERVAL", "2")),
            auth_enabled=os.environ.get("CODEX_GUI_AUTH", "1") != "0",
            session_hours=float(os.environ.get("CODEX_GUI_SESSION_HOURS", "168")),
            cookie_secure={"1": True, "0": False}.get(os.environ.get("CODEX_GUI_COOKIE_SECURE", "auto")),
        )

    @property
    def db_path(self) -> Path:
        return self.home / "codex-gui.db"

    @property
    def instructions_path(self) -> Path:
        return self.home / "instructions.md"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def worktrees_dir(self) -> Path:
        return self.home / "worktrees"

    def ensure_dirs(self) -> None:
        for d in (self.home, self.logs_dir, self.worktrees_dir):
            d.mkdir(parents=True, exist_ok=True)
