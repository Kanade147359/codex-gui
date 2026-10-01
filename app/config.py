"""Runtime settings. Everything can be overridden by environment variables."""
import os
from dataclasses import dataclass
from pathlib import Path


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

    @classmethod
    def from_env(cls) -> "Settings":
        home = Path(os.environ.get("CODEX_GUI_HOME", "~/.local/share/codex-gui")).expanduser()
        return cls(
            home=home,
            codex_bin=os.environ.get("CODEX_BIN", "codex"),
            max_concurrent=int(os.environ.get("CODEX_GUI_MAX_CONCURRENT", "0")),
        )

    @property
    def db_path(self) -> Path:
        return self.home / "codex-gui.db"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def worktrees_dir(self) -> Path:
        return self.home / "worktrees"

    def ensure_dirs(self) -> None:
        for d in (self.home, self.logs_dir, self.worktrees_dir):
            d.mkdir(parents=True, exist_ok=True)
