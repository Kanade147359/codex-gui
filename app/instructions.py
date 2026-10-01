"""The common working instructions the GUI hands to Codex.

They are given once, as `developerInstructions` of `thread/start` (or `-c developer_instructions=` of the first
`codex exec`), never re-inserted into a later turn: the thread keeps them, so they do not cost input tokens
per turn and cannot change the cached prompt prefix. The text is a constant on purpose -- nothing in it may vary
between tasks (no dates, ids, paths, quota), or every new thread would start with a different prefix.

Override: put your own text in $CODEX_GUI_HOME/instructions.md. An empty file switches the instructions off.
"""
from pathlib import Path
from typing import Optional

DEFAULT_INSTRUCTIONS = """\
Avoid dumping entire large files or logs into model context.

Prefer:
- rg
- targeted sed ranges
- head/tail
- focused tests
- concise command output

Read only the portions necessary for the current task.
"""


def load_instructions(path: Optional[Path] = None) -> str:
    """The text to send, or "" for none. A missing file means the default; an unreadable one too."""
    if path is not None:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            pass
        except (OSError, UnicodeDecodeError):
            pass
    return DEFAULT_INSTRUCTIONS.strip()
