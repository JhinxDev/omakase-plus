"""Bounded, opt-in tracker sync for the existing runner or an explicit CLI unit."""

from __future__ import annotations

import math
import os
import secrets
import time
from collections import Counter

import httpx

from omakase.plus.mal import TrackerHTTPError
from omakase.plus.tracker_sync import BridgeError, configured_service


def worker_enabled():
    return (
        all(
            os.getenv(key, "").strip().casefold() == "true"
            for key in (
                "OMAKASE_PLUS_PRIVATE",
                "OMAKASE_TRACKER_BRIDGE_WORKER_ENABLED",
                "OMAKASE_TRACKER_BRIDGE_WRITERS_READY",
            )
        )
        and os.getenv("OMAKASE_PLUS_REVIEW_MODE", "").strip().casefold() != "true"
    )


def run_tracker_bridge(
    db,
    *,
    max_accounts=2,
    budget_seconds=60,
    now=time.time,
    monotonic=time.monotonic,
    service_factory=configured_service,
    http_factory=None,
):
    """Fair persisted rotation, bounded before dispatch; no providers when gated off.

    One invocation never starts more than max_accounts distinct users. Each unit
    has at most50 seconds across its preview/apply stages, bounded further by the
    remaining invocation budget. HTTP adapters propagate that absolute deadline.
    """
    if (
        type(max_accounts) is not int
        or not 1 <= max_accounts <= 10
        or type(budget_seconds) not in (int, float)
        or not math.isfinite(budget_seconds)
        or not 1 <= budget_seconds <= 120
    ):
        raise ValueError("Choose one to ten accounts and a one to120 second budget.")
    if not worker_enabled():
        return {"state": "disabled", "attempted": 0, "outcomes": {}}
    if db.in_transaction:
        raise BridgeError("clean_transaction_required")
    if not db.execute(
        "SELECT 1 FROM _migrations WHERE name='035-myanimelist-bridge.sql'"
    ).fetchone():
        return {"state": "unavailable", "attempted": 0, "outcomes": {}}
    if not db.execute(
        "SELECT 1 FROM tracker_bridge_preferences WHERE enabled=1 LIMIT 1"
    ).fetchone():
        return {"state": "idle", "attempted": 0, "outcomes": {}}
    stamp = int(now())
    token = secrets.token_hex(24)
    with db:
        acquired = db.execute(
            "UPDATE tracker_bridge_worker_state SET lease_token=?,lease_until=? WHERE id=1 AND (lease_token IS NULL OR lease_until<=?)",
            (token, stamp + math.ceil(budget_seconds) + 10, stamp),
        ).rowcount
    if acquired != 1:
        return {"state": "busy", "attempted": 0, "outcomes": {}}
    deadline = monotonic() + budget_seconds
    seen = set()
    outcomes = Counter()

    def scheduler_guard():
        if not worker_enabled():
            raise BridgeError("worker_disabled")
        if monotonic() >= deadline:
            raise BridgeError("worker_budget_exhausted")
        if not db.execute(
            "SELECT 1 FROM tracker_bridge_worker_state WHERE id=1 AND lease_token=? AND lease_until>?",
            (token, int(now())),
        ).fetchone():
            raise BridgeError("worker_lease_expired")

    try:
        while len(seen) < max_accounts and monotonic() < deadline:
            scheduler_guard()
            cursor = db.execute(
                "SELECT last_user_id FROM tracker_bridge_worker_state WHERE id=1"
            ).fetchone()[0]
            row = db.execute(
                "SELECT user_id FROM tracker_bridge_preferences WHERE enabled=1 AND user_id>? ORDER BY user_id LIMIT 1",
                (cursor,),
            ).fetchone()
            if row is None:
                row = db.execute(
                    "SELECT user_id FROM tracker_bridge_preferences WHERE enabled=1 ORDER BY user_id LIMIT 1"
                ).fetchone()
            if row is None or row[0] in seen:
                break
            user = row[0]
            seen.add(user)
            with db:
                changed = db.execute(
                    "UPDATE tracker_bridge_worker_state SET last_user_id=? WHERE id=1 AND lease_token=? AND lease_until>?",
                    (user, token, int(now())),
                ).rowcount
            if changed != 1:
                raise BridgeError("worker_lease_expired")

            def account_guard():
                scheduler_guard()
                row = db.execute(
                    "SELECT enabled FROM tracker_bridge_preferences WHERE user_id=?", (user,)
                ).fetchone()
                if row is None or not row[0]:
                    raise BridgeError("account_sync_paused")

            try:
                account_guard()
                factory = http_factory or (lambda: httpx.Client(trust_env=False))
                with factory() as http:
                    service = service_factory(db, http, now=now)
                    result = service.sync_tick(
                        user, guard=account_guard, deadline=min(deadline, monotonic() + 50)
                    )
                state = result.get("state", result.get("status", "checked"))
                outcomes[
                    state
                    if state in {"paused", "reading", "review_required", "checked"}
                    else "deferred"
                ] += 1
            except TrackerHTTPError as exc:
                # Service records owner-scoped RetryAfter/reconnect state. Never
                # retry a provider mutation in this loop or expose its payload.
                outcomes[
                    "deferred"
                    if exc.code in {"rate_limited", "job_budget_exhausted", "transport_unavailable"}
                    else "review_required"
                ] += 1
            except BridgeError:
                outcomes["deferred"] += 1
            except (ValueError, httpx.HTTPError):
                outcomes["failed"] += 1
        return {"state": "checked", "attempted": len(seen), "outcomes": dict(outcomes)}
    finally:
        with db:
            db.execute(
                "UPDATE tracker_bridge_worker_state SET lease_token=NULL,lease_until=0 WHERE id=1 AND lease_token=?",
                (token,),
            )


def sync_trackers_once(*, data_dir=None, max_accounts=2, budget_seconds=60):
    if not worker_enabled():
        return {"state": "disabled", "attempted": 0, "outcomes": {}}
    from omakase.plus.db import _connect

    db = _connect(data_dir)
    try:
        return run_tracker_bridge(db, max_accounts=max_accounts, budget_seconds=budget_seconds)
    finally:
        db.close()
