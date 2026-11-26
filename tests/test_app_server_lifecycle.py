"""Owned app-server descendants must not survive shutdown or an exited leader."""
import asyncio
import os
from pathlib import Path
import signal
import sys

import pytest

from app.appserver import AppServerClient
from conftest import wait_for


@pytest.mark.parametrize("leader_exits", [False, True])
@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux /proc process-state evidence")
def test_close_reaps_descendants_and_closes_pipes(tmp_path, leader_exits):
    pidfile = tmp_path / "child.pid"
    child = ("import os, signal, sys, time; from pathlib import Path; "
             "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
             "Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)")
    server = f"""
import json, os, subprocess, sys, time
from pathlib import Path
subprocess.Popen([sys.executable, '-c', {child!r}, sys.argv[1]])
while not Path(sys.argv[1]).exists(): time.sleep(.01)
for line in sys.stdin:
    message = json.loads(line)
    if message.get('method') == 'initialize':
        print(json.dumps({{'id': message['id'], 'result': {{}}}}), flush=True)
    elif message.get('method') == 'initialized' and {leader_exits!r}:
        os._exit(0)
"""

    async def scenario():
        client = AppServerClient()
        client.build_command = lambda: [sys.executable, "-u", "-c", server, str(pidfile)]
        proc = None
        try:
            await client.start()
            proc = client._proc
            if leader_exits:
                await wait_for(lambda: proc.returncode is not None)
            await asyncio.wait_for(client.close(), timeout=8)
            assert proc.returncode is not None
            assert proc.stdin.is_closing()
            status = Path(f"/proc/{int(pidfile.read_text())}/status")
            def child_stopped():
                try:
                    return "\nState:\tZ" in status.read_text()
                except FileNotFoundError:
                    return True
            assert await wait_for(child_stopped, timeout=1)
            assert client._reader is None and client._stderr_reader is None
            await client.close()  # Idempotent even after leader exit.
        finally:
            if proc is not None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await proc.wait()
                proc.stdin.close()
                await proc.stdin.wait_closed()
            await client.close()

    asyncio.run(scenario())
