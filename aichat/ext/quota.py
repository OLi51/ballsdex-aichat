"""
Quota protection: a per-user cooldown on the free-text path, and a per-day API request budget.

Both exist because the rate limiter only shapes *bursts*. It paces requests to a safe rate but
never says no, so a busy day still walks straight into the daily quota and the bot then fails on
every request until midnight Pacific — the worst possible way to run out, because it happens
without warning and mid-conversation.
"""

import asyncio
import datetime
import logging
import time

from asgiref.sync import sync_to_async
from django.db.models import F, Sum

from ..models import DailyUsage

log = logging.getLogger("ballsdex.packages.aichat")

# Google's free-tier daily quotas reset at midnight US Pacific, so the budget counts Pacific days
# — our counter then clears at the same moment theirs does. Falling back to a fixed -8 offset
# keeps this working on images without a tz database; being an hour out during DST is harmless
# for a day-granularity budget.
try:
    from zoneinfo import ZoneInfo

    _QUOTA_TZ: datetime.tzinfo = ZoneInfo("America/Los_Angeles")
except Exception:  # pragma: no cover - depends on the image having tzdata
    log.debug("tz database unavailable, using a fixed UTC-8 offset for the daily quota window")
    _QUOTA_TZ = datetime.timezone(datetime.timedelta(hours=-8))


def quota_day() -> datetime.date:
    """Today, in the timezone Google's daily quotas actually roll over in."""
    return datetime.datetime.now(_QUOTA_TZ).date()


class BudgetExhaustedError(Exception):
    """Raised when the configured daily API request budget is used up."""


def is_daily_quota_error(exc: Exception) -> bool:
    """
    True only for a 429 that names a PER-DAY quota, not a per-minute one.

    The distinction matters and is the whole reason this parses the payload rather than just
    checking for 429: a per-minute 429 means "wait a moment", a per-day one means "this model is
    finished until Pacific midnight". Treating the first as the second would bench a perfectly good
    model for the rest of the day after one busy moment.

    Google distinguishes them in the QuotaFailure details, e.g.
    `GenerateRequestsPerMinutePerProjectPerModel-FreeTier` versus
    `GenerateRequestsPerDayPerProjectPerModel-FreeTier`. Note the accompanying `retryDelay` is not
    usable for this — a hard daily `limit: 0` still comes back advertising a 15-second retry.
    """
    if getattr(exc, "code", None) != 429:
        return False
    details = getattr(exc, "details", None)
    try:
        violations = []
        for entry in (details or {}).get("error", {}).get("details", []) or []:
            if isinstance(entry, dict) and "violations" in entry:
                violations.extend(entry["violations"] or [])
        if violations:
            return any("perday" in str(v.get("quotaId", "")).lower() for v in violations)
    except (AttributeError, TypeError):
        pass
    # Shape changed or wasn't a dict at all. Fall back to the raw text rather than guessing wrong:
    # an unrecognised 429 is treated as per-minute, the recoverable reading.
    return "perday" in str(details or exc).lower().replace("_", "")


class UserCooldown:
    """
    Per-user minimum interval, for the mention/DM path.

    `/chat` has carried `@app_commands.checks.cooldown(1, 5)` since the beginning, but `on_message`
    had nothing — so the cheapest way to burn the key was the one that needed no command at all.
    In-memory on purpose: a cooldown is about the next few seconds, and losing it on restart costs
    one free request, whereas persisting it would mean a DB round trip on every message the bot
    sees.
    """

    # Stale entries are only culled while adding new ones, so the sweep has to be cheap; anything
    # older than this can never still be blocking.
    _SWEEP_EVERY = 256

    def __init__(self, seconds: float = 5.0):
        self.seconds = seconds
        self._last: dict[int, float] = {}
        self._since_sweep = 0

    def retry_after(self, user_id: int) -> float:
        """Seconds this user must still wait, or 0.0 if they may go ahead."""
        if self.seconds <= 0:
            return 0.0
        last = self._last.get(user_id)
        if last is None:
            return 0.0
        return max(0.0, self.seconds - (time.monotonic() - last))

    def stamp(self, user_id: int):
        """Record that this user just consumed their slot."""
        now = time.monotonic()
        self._last[user_id] = now
        self._since_sweep += 1
        if self._since_sweep >= self._SWEEP_EVERY:
            self._since_sweep = 0
            cutoff = now - max(self.seconds, 1.0)
            for uid in [uid for uid, ts in self._last.items() if ts < cutoff]:
                del self._last[uid]


class DailyBudget:
    """
    Counts real API requests per Pacific day and refuses new turns once the budget is spent.

    Counting lives at the API call site (like the rate limiter, and for the same reason: one turn
    is 1..N requests and N isn't known in advance). The count is persisted per model so it survives
    a restart — an in-memory counter would reset to zero on every `docker compose up`, which is
    exactly when someone restarting to "fix" a quota problem would clear their own guard rail.

    The turn-start check is one aggregate query; the per-request check is served from the cached
    total, so the hot path stays free. A turn already in flight is allowed to finish, so the budget
    can overshoot by at most MAX_TOOL_ROUNDS - 1 requests. That's deliberate: cutting a turn off
    halfway would spend the quota and produce nothing.
    """

    def __init__(self):
        self._day: datetime.date | None = None
        self._count = 0
        self._exhausted: set[str] = set()
        self._lock = asyncio.Lock()

    @property
    def spent(self) -> int:
        return self._count

    @property
    def exhausted_models(self) -> set[str]:
        """Models known to have spent their daily quota today, as of the last refresh."""
        return set(self._exhausted)

    async def refresh(self) -> int:
        """Reconcile the cached total with the database, handling the day rollover."""
        day = quota_day()
        rows = [row async for row in DailyUsage.objects.filter(date=day).values("requests", "model", "exhausted")]
        total = sum(r["requests"] for r in rows)
        async with self._lock:
            self._day = day
            self._count = total
            # Reloaded from the database rather than kept only in memory, so a restart doesn't
            # forget which models are done for the day and go back to burning a rejected request
            # on each of them every turn.
            self._exhausted = {r["model"] for r in rows if r["exhausted"]}
        return total

    async def mark_exhausted(self, model: str):
        """Record that this model reported its daily quota spent; it gets tried last from now on."""
        day = quota_day()
        async with self._lock:
            self._exhausted.add(model)
        try:
            await sync_to_async(_set_exhausted)(day, model)
        except Exception as e:
            log.warning(f"Could not flag {model} as daily-exhausted: {e}")
        else:
            log.info(f"{model} has spent its daily quota; deprioritising it until Pacific midnight")

    async def check(self, limit: int):
        """
        Refresh today's state, and raise BudgetExhaustedError if the budget is spent.

        The refresh happens even when `limit <= 0` disables the cap, because it also loads which
        models are daily-exhausted — and that matters just as much on an instance running with no
        budget configured, which is the default.
        """
        spent = await self.refresh()
        if limit > 0 and spent >= limit:
            raise BudgetExhaustedError(f"daily request budget of {limit} is spent")

    async def record(self, model: str):
        """Count one API request against today's budget, in memory and in the database."""
        day = quota_day()
        async with self._lock:
            if day != self._day:
                # Rolled over mid-turn; the new day starts from whatever is already persisted.
                self._day = day
                self._count = 0
            self._count += 1
        try:
            await sync_to_async(_increment)(day, model)
        except Exception as e:
            # The budget is a safety net, not a correctness requirement — never let bookkeeping
            # take down a reply that the API already answered.
            log.warning(f"Could not record Gemini usage for {model}: {e}")


def _increment(day: datetime.date, model: str):
    """
    Atomic per-(day, model) increment.

    get_or_create then an F() update, rather than a read-modify-write, so two bots sharing one
    database can't lose counts to a race — the whole point of persisting this is that the number
    is trustworthy.
    """
    obj, _ = DailyUsage.objects.get_or_create(date=day, model=model)
    DailyUsage.objects.filter(pk=obj.pk).update(requests=F("requests") + 1)


def _set_exhausted(day: datetime.date, model: str):
    DailyUsage.objects.update_or_create(date=day, model=model, defaults={"exhausted": True})
