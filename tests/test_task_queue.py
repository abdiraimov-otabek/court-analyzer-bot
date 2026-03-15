import asyncio

import pytest

from src.services.task_queue import AsyncTaskQueue, QueuedTask


@pytest.mark.asyncio
async def test_task_queue_runs_submitted_tasks():
    queue = AsyncTaskQueue(worker_count=1)
    seen: list[str] = []
    finished = asyncio.Event()

    async def run_task() -> None:
        seen.append("ran")
        finished.set()

    await queue.submit(QueuedTask(name="test", request_id="req-1", run=run_task))
    await asyncio.wait_for(finished.wait(), timeout=1)
    await queue.aclose()

    assert seen == ["ran"]
