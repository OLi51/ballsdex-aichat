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


class RateLimiter:
    """
    Minimum-interval gate around individual Gemini API requests.

    This deliberately sits at the API call site rather than around a whole chat turn. One turn
    issues one request per tool round (up to MAX_TOOL_ROUNDS), plus a fresh set for every model
    in the fallback chain that gets tried — so pacing per turn under-counts actual API usage by
    however many tools the model decides to call, which is exactly the number we don't know in
    advance. Gating here makes `requests_per_minute` mean requests per minute.
    """

    def __init__(self, requests_per_minute: int = 12):
        self._lock = asyncio.Lock()
        self._last_call = 0.0
        self.set_rate(requests_per_minute)

    def set_rate(self, requests_per_minute: int):
        self.min_interval = 60.0 / max(requests_per_minute, 1)

    async def acquire(self):
        """Block until it's safe to issue the next API request."""
        async with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()


class GeminiQueue:
    """
    Serializes every chat turn behind a single worker, so a shared API key (potentially reused
    across several bots/servers) is never hit with concurrent requests. Callers
    `await queue.submit(...)` and transparently wait their turn.

    The queue provides ordering, concurrency of one, backpressure and a timeout. Per-minute
    pacing is NOT done here — it belongs to `self.limiter`, which the chat client acquires
    before each individual API request (see RateLimiter for why).
    """

    def __init__(self, requests_per_minute: int = 12, max_queue_size: int = 30, job_timeout: float = 180.0):
        self.limiter = RateLimiter(requests_per_minute)
        self.max_queue_size = max_queue_size
        # Generous, because a turn's duration now includes the limiter waiting out the interval
        # before each of its requests: at 12/minute a six-round turn can legitimately spend most
        # of a minute just holding the rate down. This is a ceiling for a wedged request, not a
        # target.
        self.job_timeout = job_timeout
        self._queue: asyncio.Queue[_Job] = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None

    def set_rate(self, requests_per_minute: int):
        self.limiter.set_rate(requests_per_minute)

    def start(self):
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.get_running_loop().create_task(self._worker())

    def stop(self):
        if self._worker_task is not None:
            self._worker_task.cancel()
            self._worker_task = None
        # Cancelling the worker only settles whichever job it was actively processing (see
        # _worker's CancelledError handler). Anything still sitting in the queue was never
        # touched, so without this its future would never resolve and the caller waiting on
        # `submit()` would hang forever (e.g. on cog reload with more than one request queued).
        while not self._queue.empty():
            try:
                job = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if not job.future.done():
                job.future.cancel()
            self._queue.task_done()

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
