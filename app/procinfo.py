"""Process identity (Linux /proc), so that a recorded pid is only trusted if it is still the same process.

A bare pid proves nothing: after the GUI (or the machine) restarts, the number may belong to an unrelated process.
The identity is "<boot id>:<start time in clock ticks>" taken from /proc/<pid>/stat right after the process was
started. A reused pid has a different start time; a reboot has a different boot id.
"""
from typing import Optional


def boot_id() -> str:
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except OSError:
        return ""


def _start_ticks(pid: int) -> Optional[str]:
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read().decode(errors="replace")
    except OSError:
        return None
    # "pid (comm) state ppid ...": comm may contain spaces and parentheses, so split after the LAST ")".
    rest = data[data.rfind(")") + 2:].split()
    # rest[0] is field 3 (state); starttime is field 22.
    if len(rest) < 20:
        return None
    if rest[0] == "Z":  # a zombie has exited; it only waits to be reaped
        return None
    return rest[19]


def process_identity(pid: Optional[int]) -> Optional[str]:
    """The identity of a live process, or None when there is no such process."""
    if not pid:
        return None
    ticks = _start_ticks(pid)
    return f"{boot_id()}:{ticks}" if ticks else None


def cmdline(pid: Optional[int]) -> str:
    if not pid:
        return ""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        return ""


def same_process(pid: Optional[int], identity: Optional[str], needle: str = "codex") -> bool:
    """True only if `pid` is alive, was started at the recorded time, and its command line still mentions `needle`.

    Without a recorded identity (rows written before it existed) only the pid and the command line can be checked.
    That is the weaker test: it errs towards "still running", which means the task is never started twice.
    """
    if not pid:
        return False
    now = process_identity(pid)
    if now is None:
        return False
    if identity and now != identity:
        return False  # the pid was reused (or the machine rebooted)
    return needle in cmdline(pid)
