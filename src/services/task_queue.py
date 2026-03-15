from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class QueuedTask:
    name: str
    request_id: str
    run: Callable[[], Awaitable[None]]


class AsyncTaskQueue:
    def __init__(self, worker_count: int = 1) -> None:
        self._worker_count = max(worker_count, 1)
        self._queue: asyncio.Queue[QueuedTask | None] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        self._started = False
        self._logger = logging.getLogger("task_queue")

    async def submit(self, task: QueuedTask) -> None:
        await self._ensure_started()
        await self._queue.put(task)

    async def aclose(self) -> None:
        if not self._started:
            return
        for _ in self._workers:
            await self._queue.put(None)
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        self._started = False

    def size(self) -> int:
        return self._queue.qsize()

    async def _ensure_started(self) -> None:
        if self._started:
            return
        self._started = True
        self._workers = [
            asyncio.create_task(self._worker_loop(index))
            for index in range(self._worker_count)
        ]

    async def _worker_loop(self, index: int) -> None:
        while True:
            task = await self._queue.get()
            try:
                if task is None:
                    return
                await task.run()
            except Exception:
                self._logger.exception(
                    "task_queue.worker_failed",
                    extra={
                        "data": {
                            "worker_index": index,
                            "task_name": getattr(task, "name", "unknown"),
                            "request_id": getattr(task, "request_id", ""),
                        }
                    },
                )
            finally:
                self._queue.task_done()
