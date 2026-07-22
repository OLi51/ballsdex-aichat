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

    def wait_time(self) -> float:
        """
        Seconds until this bucket would let a request through, without taking the slot.

        A peek, so the caller can compare buckets and pick the least congested one before
        committing to a model. Never blocks and never reserves anything.
        """
        return max(0.0, self.min_interval - (time.monotonic() - self._last_call))

    async def acquire(self):
        """Block until it's safe to issue the next API request."""
        async with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()


class ModelLimiters:
    """
    One RateLimiter per model ID, plus congestion-aware ordering of the fallback chain.

    The free tier meters requests per *model*, not per key — a 429 names its quota
    `GenerateRequestsPerMinutePerProjectPerModel-FreeTier` and carries the model in
    `quotaDimensions`. So two models in the chain are two independent 15/min buckets, and pacing
    them through one shared gate threw away half the available throughput.

    `order()` exploits that: when the primary's bucket is busy, a fallback whose bucket is idle
    answers immediately instead of the turn stalling. Daily totals don't change (each model
    already had its own daily pool) — the gain is entirely in requests per minute.
    """

    # Buckets are compared at whole-second resolution so that a trivially-sooner fallback never
    # displaces the primary. Models are only reordered when the primary is congested enough that
    # waiting for it would be visible to the user — below that, chain order (i.e. quality) wins.
    ORDER_GRANULARITY = 1.0

    def __init__(self, requests_per_minute: int = 12):
        self._rpm = max(requests_per_minute, 1)
        self._limiters: dict[str, RateLimiter] = {}

    def set_rate(self, requests_per_minute: int):
        self._rpm = max(requests_per_minute, 1)
        for limiter in self._limiters.values():
            limiter.set_rate(self._rpm)

    def get(self, model: str) -> RateLimiter:
        limiter = self._limiters.get(model)
        if limiter is None:
            limiter = RateLimiter(self._rpm)
            self._limiters[model] = limiter
        return limiter

    def order(self, models: list[str], exhausted: set[str] | None = None) -> list[str]:
        """
        The chain re-sorted least-congested-first, ties keeping their original (quality) order.

        `sorted` is stable, so equal buckets — the normal case on a quiet instance — leave the
        list exactly as the owner configured it.

        `exhausted` names models that have already reported their *daily* quota spent; they sort
        behind everything else regardless of congestion. They are demoted rather than dropped,
        because that belief comes from a single API response and could be wrong (a reset we
        haven't seen, a transient mislabelled upstream) — last place costs one wasted request in
        the rare case it's stale, while removing them outright could leave the chain empty and the
        bot mute.
        """
        exhausted = exhausted or set()
        return sorted(
            models,
            key=lambda m: (m in exhausted, int(self.get(m).wait_time() / self.ORDER_GRANULARITY)),
        )


class GeminiQueue:
    """
    Serializes every chat turn behind a single worker, so a shared API key (potentially reused
    across several bots/servers) is never hit with concurrent requests. Callers
    `await queue.submit(...)` and transparently wait their turn.

    The queue provides ordering, concurrency of one, backpressure and a timeout. Per-minute
    pacing is NOT done here — it belongs to `self.limiters`, which the chat client acquires from
    before each individual API request (see RateLimiter and ModelLimiters for why).
    """

    def __init__(self, requests_per_minute: int = 12, max_queue_size: int = 30, job_timeout: float = 180.0):
        self.limiters = ModelLimiters(requests_per_minute)
        self.max_queue_size = max_queue_size
        # Generous, because a turn's duration now includes the limiter waiting out the interval
        # before each of its requests: at 12/minute a six-round turn can legitimately spend most
        # of a minute just holding the rate down. This is a ceiling for a wedged request, not a
        # target.
        self.job_timeout = job_timeout
        self._queue: asyncio.Queue[_Job] = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None

    def set_rate(self, requests_per_minute: int):
        self.limiters.set_rate(requests_per_minute)

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
