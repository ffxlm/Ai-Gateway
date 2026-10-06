"""Runtime-adjustable concurrency controls.

The gateway limits load in two independent dimensions:

* **Per-upstream pools** — free traffic (9Router) and premium traffic (the
  billed provider) have separate pools, because each upstream has its own
  capacity (9Router absorbs far more parallel requests than the premium
  provider's advertised rate limit).
* **Per-user cap** — a single account can never occupy more than a few slots of
  a pool, so one heavy client cannot starve everyone else.

Every limit is read from the live settings store (editable in the Admin panel)
rather than being frozen at import time, so retuning needs no restart. A short
TTL cache keeps the settings read off the request hot path.

Acquisition order is always *per-user first, then pool*. Every request follows
the same order, so there is no circular wait and therefore no deadlock; a user
never holds a scarce pool slot while waiting on their own cap.
"""

import asyncio
import contextlib
import time
from typing import Dict

from app.core.config import settings
from app.core.database import get_setting

# Settings are cached for this many seconds to avoid a SQLite read per request.
_CACHE_TTL_SECONDS = 5.0

_cache: Dict[str, int] = {}
_cache_at = 0.0


def _positive(raw, fallback: int) -> int:
    """Parse a limit, falling back to a sane value and never going below 1.

    A limit of 0 (or a bad value) would block every request forever, so it is
    clamped up to 1 instead.
    """
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        value = int(fallback)
    return max(value, 1)


def _limits() -> Dict[str, int]:
    """Return the current free/premium/per-user limits, cached briefly."""
    global _cache, _cache_at
    now = time.time()
    if now - _cache_at > _CACHE_TTL_SECONDS or not _cache:
        _cache = {
            "free": _positive(
                get_setting("free_concurrency_limit", str(settings.FREE_CONCURRENCY_LIMIT)),
                settings.FREE_CONCURRENCY_LIMIT,
            ),
            "premium": _positive(
                get_setting("premium_concurrency_limit", str(settings.PREMIUM_CONCURRENCY_LIMIT)),
                settings.PREMIUM_CONCURRENCY_LIMIT,
            ),
            "per_user": _positive(
                get_setting("per_user_concurrency_limit", str(settings.PER_USER_CONCURRENCY_LIMIT)),
                settings.PER_USER_CONCURRENCY_LIMIT,
            ),
        }
        _cache_at = now
    return _cache


class _LoopBound:
    """Holds an ``asyncio.Condition`` that rebinds if the event loop changes.

    Production runs a single long-lived loop, but a test harness (or any embedder
    that calls ``asyncio.run`` more than once) would otherwise hit asyncio's
    "bound to a different event loop" guard. Rebinding with fresh state is safe
    because a loop change implies nothing is currently in flight.
    """

    def __init__(self):
        self._cond = None
        self._loop = None

    def _condition(self) -> asyncio.Condition:
        loop = asyncio.get_running_loop()
        if self._cond is None or self._loop is not loop:
            self._cond = asyncio.Condition()
            self._loop = loop
            self._reset()
        return self._cond

    def _reset(self) -> None:
        pass


class _PoolLimiter(_LoopBound):
    """A semaphore whose ceiling can change at runtime.

    ``asyncio.Semaphore`` fixes its value at construction; this re-reads the
    limit on every acquire so an admin edit takes effect on the next request.
    """

    def __init__(self):
        super().__init__()
        self._active = 0

    def _reset(self) -> None:
        self._active = 0

    async def acquire(self, limit: int) -> None:
        cond = self._condition()
        async with cond:
            while self._active >= limit:
                await cond.wait()
            self._active += 1

    async def release(self) -> None:
        cond = self._condition()
        async with cond:
            if self._active > 0:
                self._active -= 1
            cond.notify_all()

    @property
    def active(self) -> int:
        return self._active


class _UserLimiter(_LoopBound):
    """Per-user in-flight counter shared across both pools."""

    def __init__(self):
        super().__init__()
        self._counts: Dict[str, int] = {}

    def _reset(self) -> None:
        self._counts = {}

    async def acquire(self, user_id: str, limit: int) -> None:
        cond = self._condition()
        async with cond:
            while self._counts.get(user_id, 0) >= limit:
                await cond.wait()
            self._counts[user_id] = self._counts.get(user_id, 0) + 1

    async def release(self, user_id: str) -> None:
        cond = self._condition()
        async with cond:
            remaining = self._counts.get(user_id, 0) - 1
            if remaining <= 0:
                # Drop the key entirely so the dict does not grow forever.
                self._counts.pop(user_id, None)
            else:
                self._counts[user_id] = remaining
            cond.notify_all()


_FREE_POOL = _PoolLimiter()
_PREMIUM_POOL = _PoolLimiter()
_USER_LIMITER = _UserLimiter()


async def acquire_slot(user_id: str, is_premium: bool) -> _PoolLimiter:
    """Reserve a slot for ``user_id``; returns the pool to release afterwards.

    The per-user cap is taken first (fairness), then a pool slot (capacity).
    If the pool wait is cancelled, the per-user slot is released immediately so
    a disconnected client never leaks a per-user count.
    """
    limits = _limits()
    pool = _PREMIUM_POOL if is_premium else _FREE_POOL
    await _USER_LIMITER.acquire(user_id, limits["per_user"])
    try:
        await pool.acquire(limits["premium" if is_premium else "free"])
    except BaseException:
        await _USER_LIMITER.release(user_id)
        raise
    return pool


async def release_slot(user_id: str, pool: _PoolLimiter) -> None:
    """Release a slot previously reserved by :func:`acquire_slot`."""
    await pool.release()
    await _USER_LIMITER.release(user_id)


def current_usage() -> Dict[str, int]:
    """Best-effort snapshot of live concurrency, for admin/observability."""
    limits = _limits()
    return {
        "free_active": _FREE_POOL.active,
        "free_limit": limits["free"],
        "premium_active": _PREMIUM_POOL.active,
        "premium_limit": limits["premium"],
        "per_user_limit": limits["per_user"],
    }
