"""The background scheduler: calls `tick` every few seconds, and at once when something changed.

It holds no state of its own. What `tick` does (evaluate waiting tasks, start due retries, claim queued tasks, notice
orphaned processes) is decided from the database each time, and every state change in it is one atomic UPDATE, so a
tick that runs twice, or at the same moment as a request handler, cannot start a task twice.
"""
import asyncio
import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, tick: Callable[[], None], interval: float = 2.0):
        self._tick = tick
        self.interval = interval
        self._wakeup: Optional[asyncio.Event] = None
        self._task: Optional[asyncio.Task] = None
        self._stopping = False

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, interval: Optional[float] = None) -> None:
        if self.running:
            return
        if interval is not None:
            self.interval = interval
        self._stopping = False
        self._wakeup = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="codex-gui-scheduler")

    def wake(self) -> None:
        """Run the next tick now instead of waiting for the interval (status changes call this)."""
        if self._wakeup is not None:
            self._wakeup.set()

    async def stop(self) -> None:
        task, self._task = self._task, None
        self._stopping = True
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        # Not asyncio.wait_for: before Python 3.12 it can swallow a cancellation that arrives just as the awaited event
        # is set (a status change wakes the loop at the very moment of stop()), and the loop would then never end.
        while not self._stopping:
            try:
                self._tick()
            except Exception:  # one bad tick must not end scheduling for good
                logger.exception("scheduler tick failed")
            waiter = asyncio.ensure_future(self._wakeup.wait())
            try:
                await asyncio.wait({waiter}, timeout=self.interval)
            finally:
                waiter.cancel()
            self._wakeup.clear()
