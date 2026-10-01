"""Per-task JSONL log files. One JSON object per line:
{"ts", "stream": stdout|stderr|system, "type", "message", "event": parsed codex event or null}
Readers use a byte offset as the cursor, so polling is cheap and incremental.
"""
import json
from pathlib import Path

from .codex_runner import parse_line
from .models import now_iso

READ_CHUNK_BYTES = 2 * 1024 * 1024


class TaskLog:
    """Append-only writer. Each entry is a single write() so concurrent coroutines never interleave."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._f = open(self.path, "a", encoding="utf-8")

    def _write(self, entry: dict) -> None:
        self._f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._f.flush()

    def add_stdout(self, line: str) -> dict:
        """Write one stdout line; returns parse_line()'s result so callers need not parse it twice."""
        parsed = parse_line(line)
        self._write({"ts": now_iso(), "stream": "stdout", **parsed})
        return parsed

    def add_stderr(self, line: str) -> None:
        self._write({"ts": now_iso(), "stream": "stderr", "type": "stderr",
                     "message": line.rstrip("\n"), "event": None})

    def add_system(self, message: str) -> None:
        self._write({"ts": now_iso(), "stream": "system", "type": "system",
                     "message": message, "event": None})

    def close(self) -> None:
        self._f.close()


def read_log(path: Path, offset: int = 0) -> tuple[list[dict], int]:
    """Return (entries, next_offset) for complete lines starting at byte `offset`."""
    path = Path(path)
    if not path.exists():
        return [], offset
    with open(path, "rb") as f:
        f.seek(offset)
        chunk = f.read(READ_CHUNK_BYTES)
    end = chunk.rfind(b"\n") + 1  # ignore a partially written trailing line
    entries = []
    for raw in chunk[:end].splitlines():
        try:
            entries.append(json.loads(raw))
        except ValueError:
            entries.append({"ts": "", "stream": "system", "type": "raw",
                            "message": raw.decode(errors="replace"), "event": None})
    return entries, offset + end
