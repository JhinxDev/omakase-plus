"""Short-lived, owner-scoped OAuth handshake persistence.

OAuth authorization callbacks may arrive after an application restart or on a
different worker. Persisting the PKCE verifier avoids process-local state while
the hashed ``state`` value provides login-CSRF protection.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time

OAUTH_FLOW_TTL_SECONDS = 10 * 60


def _state_hash(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def create_oauth_flow(
    db: sqlite3.Connection,
    *,
    user_id: int,
    provider: str,
    code_verifier: str,
    now: int | None = None,
) -> str:
    """Persist one expiring handshake and return its unguessable state token."""
    current_time = int(time.time()) if now is None else now
    state = secrets.token_urlsafe(32)
    with db:
        db.execute("DELETE FROM oauth_flow_states WHERE expires_at < ?", (current_time,))
        db.execute(
            "DELETE FROM oauth_flow_states WHERE user_id = ? AND provider = ?",
            (user_id, provider),
        )
        db.execute(
            """
            INSERT INTO oauth_flow_states
                (user_id, provider, state_hash, code_verifier, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                user_id,
                provider,
                _state_hash(state),
                code_verifier,
                current_time + OAUTH_FLOW_TTL_SECONDS,
            ),
        )
    return state


def consume_oauth_flow(
    db: sqlite3.Connection,
    *,
    user_id: int,
    provider: str,
    state: str,
    now: int | None = None,
) -> str | None:
    """Consume a matching live handshake once and return its PKCE verifier."""
    if not state:
        return None
    current_time = int(time.time()) if now is None else now
    hashed_state = _state_hash(state)
    with db:
        row = db.execute(
            """
            SELECT id, code_verifier, expires_at
            FROM oauth_flow_states
            WHERE user_id = ? AND provider = ? AND state_hash = ?
            """,
            (user_id, provider, hashed_state),
        ).fetchone()
        if row is None:
            return None
        db.execute("DELETE FROM oauth_flow_states WHERE id = ?", (row["id"],))
    if int(row["expires_at"]) < current_time:
        return None
    return str(row["code_verifier"])
