from __future__ import annotations

import asyncio
import logging

from app.run_lifecycle.service import RunLifecycle

log = logging.getLogger(__name__)


class RunLifecycleWorker:
    """从 PostgreSQL 权威队列领取 due work；不拥有第二套状态机。"""

    def __init__(
        self,
        lifecycle: RunLifecycle,
        *,
        poll_interval: float = 0.25,
    ) -> None:
        self._lifecycle = lifecycle
        self._poll_interval = poll_interval
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self._lifecycle._run_due_work_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("RunLifecycle worker tick failed")
            try:
                await asyncio.wait_for(
                    self._stopping.wait(),
                    timeout=self._poll_interval,
                )
            except TimeoutError:
                continue
