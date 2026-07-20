import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

log = logging.getLogger("ballsdex.packages.aichat")


class QueueFullError(Exception):
    """Raised when a job is submitted while the queue is already at capacity."""


@dataclass
class _Job:
    coro_factory: Callable[[], Awaitable[Any]]
    future: "asyncio.Future[Any]"


class GeminiQueue:
    """
    Serializes every call to the Gemini API behind a single worker, so a shared API key
    (potentially reused across several bots/servers) is never hit with concurrent requests
    and never bursts past its per-minute quota. Callers `await queue.submit(...)` and
    transparently wait their turn; the queue enforces a minimum spacing between calls.
    """

    def __init__(self, requests_per_minute: int = 12, max_queue_size: int = 30, job_timeout: float = 60.0):
        self.set_rate(requests_per_minute)
        self.max_queue_size = max_queue_size
        self.job_timeout = job_timeout
        self._queue: asyncio.Queue[_Job] = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None
        self._last_call = 0.0

    def set_rate(self, requests_per_minute: int):
        self.min_interval = 60.0 / max(requests_per_minute, 1)

    def start(self):
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.get_running_loop().create_task(self._worker())

    def stop(self):
        if self._worker_task is not None:
            self._worker_task.cancel()
            self._worker_task = None

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    async def submit(self, coro_factory: Callable[[], Awaitable[Any]]) -> Any:
        """Queue a job and wait for its result, respecting the configured request rate."""
        if self._queue.qsize() >= self.max_queue_size:
            raise QueueFullError("Too many people are waiting on the AI right now, try again in a bit.")

        future: "asyncio.Future[Any]" = asyncio.get_running_loop().create_future()
        await self._queue.put(_Job(coro_factory, future))
        return await future

    async def _worker(self):
        while True:
            job = await self._queue.get()
            try:
                wait = self.min_interval - (time.monotonic() - self._last_call)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_call = time.monotonic()

                result = await asyncio.wait_for(job.coro_factory(), timeout=self.job_timeout)
                if not job.future.done():
                    job.future.set_result(result)
            except asyncio.CancelledError:
                if not job.future.done():
                    job.future.cancel()
                raise
            except Exception as e:
                log.error(f"Queued Gemini request failed: {e}")
                if not job.future.done():
                    job.future.set_exception(e)
            finally:
                self._queue.task_done()
