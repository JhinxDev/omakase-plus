"""Authentication utilities for Omakase Plus.

Uses Argon2 for password hashing and cryptographically-random session
tokens stored in SQLite.
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher

_hasher = PasswordHasher()


# ── Password helpers ────────────────────────────────────────


def hash_password(plain: str) -> str:
    """Return an Argon2id hash of *plain*."""
    return _hasher.hash(plain)


def verify_password(plain: str, hash_value: str) -> bool:
    """Return True if *plain* matches *hash_value*.

    Returns False on any Argon2 exception (wrong format, verification
    failure, etc.) instead of propagating the error.
    """
    try:
        return _hasher.verify(hash_value, plain)
    except Exception:
        return False


# ── Session helpers ─────────────────────────────────────────


def session_max_age() -> int:
    """Configured idle lifetime, bounded to 7–365 days (default 90)."""
    try:
        days = int(os.getenv("OMAKASE_SESSION_DAYS", "90"))
    except ValueError:
        days = 90
    return (days if 7 <= days <= 365 else 90) * 86400


def create_session(db, user_id: int) -> str:
    """Insert a persistent session for *user_id*.

    Returns the raw session id (64 hex chars) that should be stored
    in the ``omakase_session`` cookie.
    """
    session_id = secrets.token_hex(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=session_max_age())).isoformat()
    db.execute(
        "INSERT INTO sessions (id, user_id, expires_at) VALUES (?, ?, ?)",
        (session_id, user_id, expires_at),
    )
    db.commit()
    return session_id


def validate_session(db, session_id: str) -> int | None:
    """Look up *session_id* and return the associated ``user_id``.

    Expired sessions are silently deleted during lookup and ``None``
    is returned.
    """
    return authenticate_session(db, session_id, renew=False)[0]


def authenticate_session(db, session_id: str, *, renew: bool = True) -> tuple[int | None, bool]:
    """Authenticate and occasionally extend idle expiry using compare-and-set.

    The boolean asks the HTTP layer to renew its persistent cookie. Missing,
    expired and revoked sessions can never be recreated by this operation.
    """
    row = db.execute(
        "SELECT user_id, expires_at FROM sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        return None, False
    now = datetime.now(timezone.utc)
    try:
        expires = datetime.fromisoformat(row["expires_at"])
        invalid = expires.tzinfo is None or expires <= now
    except (TypeError, ValueError):
        invalid = True
    if invalid:
        db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        db.commit()
        return None, False
    renewed = False
    lifetime = session_max_age()
    if renew and expires - now < timedelta(seconds=lifetime / 3):
        cursor = db.execute(
            "UPDATE sessions SET expires_at=? WHERE id=? AND expires_at=?",
            ((now + timedelta(seconds=lifetime)).isoformat(), session_id, row["expires_at"]),
        )
        db.commit()
        renewed = cursor.rowcount == 1
        if (
            not renewed
            and db.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone() is None
        ):
            return None, False
    return row["user_id"], renewed


def delete_session(db, session_id: str) -> None:
    """Remove *session_id* from the database."""
    db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    db.commit()
