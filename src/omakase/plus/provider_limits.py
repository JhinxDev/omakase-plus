"""Secret-free, owner-scoped provider restrictions; never shorten Retry-After."""

from __future__ import annotations

import math
import sqlite3
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime


class ProviderRateLimit(Exception):
    http_status = 429

    def __init__(self, retry_after_seconds: int | None = 900):
        self.retry_after_seconds = retry_after_seconds
        self.manual_review_required = retry_after_seconds is None
        self.retained_asset_ids: tuple[int, ...] = ()
        self.detail = (
            "Provider pause requires operator review before another request."
            if self.manual_review_required
            else "Provider requested a pause before another request."
        )
        super().__init__(self.detail)


def retry_delay(value: str | None, *, now: datetime | None = None) -> int | None:
    """HTTP delay-seconds/date; enormous valid values hold rather than retry early."""
    now = now or datetime.now(UTC)
    value = (value or "").strip()
    if value and value.isascii() and value.isdecimal():
        if len(value) > 12:
            return None
        delay = max(1, int(value))
    else:
        try:
            deadline = parsedate_to_datetime(value)
            if deadline.tzinfo is None:
                return 900
            delay = max(1, math.ceil((deadline.astimezone(UTC) - now).total_seconds()))
        except (ValueError, TypeError, OverflowError):
            return 900
    try:
        now + timedelta(seconds=delay)
    except (OverflowError, ValueError):
        return None
    return delay


def _stamp(now: datetime) -> str:
    return now.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def cooldown_hooks(
    db: sqlite3.Connection, owner_user_id: int, *, provider="realdebrid", clock=None
):
    """Guard every request and persist only sanitized deadlines, not credentials.

    Callers invoke these at the existing committed remote-operation boundary.
    Restrictions survive credential rotation and apply to all actors of an owner.
    """
    clock = clock or (lambda: datetime.now(UTC))

    def guard():
        row = db.execute(
            "SELECT not_before,manual_review FROM provider_cooldowns WHERE owner_user_id=? AND provider_kind=?",
            (owner_user_id, provider),
        ).fetchone()
        if row is None:
            return
        if row[1]:
            raise ProviderRateLimit(None)
        now = clock()
        deadline = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
        if deadline > now:
            raise ProviderRateLimit(max(1, math.ceil((deadline - now).total_seconds())))

    def observe(error: ProviderRateLimit):
        now = clock()
        deadline = (
            None
            if error.manual_review_required
            else _stamp(now + timedelta(seconds=error.retry_after_seconds))
        )
        db.execute(
            """INSERT INTO provider_cooldowns(owner_user_id,provider_kind,not_before,manual_review,updated_at)
            VALUES(?,?,?,?,?) ON CONFLICT(owner_user_id,provider_kind) DO UPDATE SET
            manual_review=MAX(provider_cooldowns.manual_review,excluded.manual_review),
            not_before=CASE WHEN provider_cooldowns.not_before IS NULL THEN excluded.not_before
              WHEN excluded.not_before IS NULL THEN provider_cooldowns.not_before
              ELSE MAX(provider_cooldowns.not_before,excluded.not_before) END,
            updated_at=excluded.updated_at""",
            (owner_user_id, provider, deadline, int(error.manual_review_required), _stamp(now)),
        )
        db.commit()

    return {"request_guard": guard, "rate_limit_observer": observe}
