"""Immediate bounded admission for one private, single-worker player gateway."""

from __future__ import annotations

import asyncio
import time
from collections import Counter, deque


class Busy(ValueError):
    def __init__(self, message, *, retry_after=10):
        super().__init__(message)
        self.retry_after = retry_after


# A seek asks for the target segment and its neighbours at once. Eight
# concurrent reads plus a one second wait turned that burst into 429s, which
# AVPlayer reports as -16845 and a failed item, so the read allowance is higher
# than the allowance for start and close requests. Tests read these names
# instead of hardcoding the numbers.
READ_LIMIT_TOTAL = 24
READ_LIMIT_VIEWER = 14
READ_LIMIT_SESSION = 14


class Admission:
    """Count preparation before its first asynchronous await.

    Starts: four globally, one per viewer, six per viewer/minute.
    Reads: twenty four globally, fourteen per viewer and session, 180/minute by default.
    A seek overlaps old audio/video bodies, new init/segment pairs and ASS fonts;
    one session may use the viewer's existing allowance without increasing it.
    Rate records expire after a minute and are capped at 1024 viewer/kind pairs.
    Deployment uses one ASGI worker; the expensive copy lease is also durable.
    """

    def __init__(self, clock=time.monotonic, *, read_rate_limit=180):
        self.clock = clock
        self.read_rate_limit = read_rate_limit
        self.active = Counter()
        self.rates = {}
        self._waiting_reads = asyncio.Semaphore(28)
        self._read_released: asyncio.Event | None = None

    async def acquire_buffered_read(self, viewer, session):
        """Let HLS seeks overlap retiring requests without increasing active reads."""
        try:
            return self.acquire("read", viewer, session)
        except Busy as error:
            if error.retry_after != 1 or self._waiting_reads.locked():
                raise

        try:
            # A seek requests several segments at once and each one takes
            # seconds to build, so wait rather than reject.
            async with self._waiting_reads, asyncio.timeout(6):
                while True:
                    try:
                        return self.acquire("read", viewer, session)
                    except Busy as error:
                        if error.retry_after != 1:
                            raise
                    if self._read_released is None:
                        self._read_released = asyncio.Event()
                    await self._read_released.wait()
        except TimeoutError:
            raise Busy("Player request limit reached. Try again shortly.", retry_after=1) from None

    def acquire(self, kind, viewer, session=None):
        now = self.clock()
        for key, values in list(self.rates.items()):
            while values and values[0] <= now - 60:
                values.popleft()
            if not values:
                del self.rates[key]
        rate_key = (kind, viewer)
        if kind == "start":
            limits, rate_limit = [((kind,), 4), ((kind, viewer), 1)], 6
        elif kind in {"close", "close_replay", "seek"}:
            limits, rate_limit = (
                [((kind,), 16), ((kind, viewer), 2), ((kind, viewer, session), 1)],
                30,
            )
        elif kind == "read":
            limits, rate_limit = (
                [
                    ((kind,), READ_LIMIT_TOTAL),
                    ((kind, viewer), READ_LIMIT_VIEWER),
                    ((kind, viewer, session), READ_LIMIT_SESSION),
                ],
                self.read_rate_limit,
            )
        else:
            raise ValueError("Unknown player admission kind")
        if len(self.rates.get(rate_key, ())) >= rate_limit or (
            rate_key not in self.rates and len(self.rates) >= 1024
        ):
            raise Busy("Player request limit reached. Try again shortly.")
        if any(self.active[key] >= limit for key, limit in limits):
            raise Busy(
                "Player request limit reached. Try again shortly.",
                retry_after=1 if kind == "read" else 10,
            )
        self.rates.setdefault(rate_key, deque()).append(now)
        for key, _ in limits:
            self.active[key] += 1
        released = False

        def release():
            nonlocal released
            if not released:
                released = True
                for key, _ in limits:
                    self.active[key] -= 1
                    if not self.active[key]:
                        del self.active[key]
                if kind == "read" and self._read_released is not None:
                    self._read_released.set()
                    self._read_released = None

        return release
