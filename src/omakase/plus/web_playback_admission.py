"""Immediate bounded admission for one private, single-worker player gateway."""

from __future__ import annotations

import time
from collections import Counter, deque


class Busy(ValueError):
    def __init__(self, message, *, retry_after=10):
        super().__init__(message)
        self.retry_after = retry_after


class Admission:
    """No waiting queue; count preparation before its first asynchronous await.

    Starts: four globally, one per viewer, six per viewer/minute.
    Reads: sixteen globally, eight per viewer and session, 180/minute.
    A seek overlaps old audio/video bodies, new init/segment pairs and ASS fonts;
    one session may use the viewer's existing allowance without increasing it.
    Rate records expire after a minute and are capped at 1024 viewer/kind pairs.
    Deployment uses one ASGI worker; the expensive copy lease is also durable.
    """

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.active = Counter()
        self.rates = {}

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
        elif kind in {"close", "close_replay"}:
            limits, rate_limit = (
                [((kind,), 16), ((kind, viewer), 2), ((kind, viewer, session), 1)],
                30,
            )
        elif kind == "read":
            limits, rate_limit = (
                [((kind,), 16), ((kind, viewer), 8), ((kind, viewer, session), 8)],
                180,
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

        return release
