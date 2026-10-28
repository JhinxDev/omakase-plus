"""Signed Home Assistant observations for Apple TV Crunchyroll playback."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
import unicodedata
from binascii import Error as BinasciiError
from dataclasses import dataclass
from datetime import datetime, timezone

from cryptography.exceptions import InvalidTag

from omakase.plus.secrets import decrypt, encrypt
from omakase.plus.watch.models import WatchEvent

MAX_APPLE_TV_EVENT_BYTES = 16 * 1024
SIGNATURE_MAX_AGE_SECONDS = 300
_UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_SIGNATURE_RE = re.compile(r"sha256=([0-9a-f]{64})")


class AppleTVWatchError(ValueError):
    """An Apple TV integration request failed validation."""


@dataclass(frozen=True)
class IntegrationSecret:
    key_id: str
    secret: str


@dataclass(frozen=True)
class VerifiedIntegration:
    id: int
    user_id: int
    key_id: str
    signed_at: int


@dataclass(frozen=True)
class AppleTVObservation:
    event_id: str
    observed_at: str
    device_id: str
    app_name: str
    series_title: str
    season: int | None
    episode: int | None
    position_seconds: float
    duration_seconds: float

    @property
    def playback_fraction(self) -> float:
        return min(1.0, self.position_seconds / self.duration_seconds)


@dataclass(frozen=True)
class AppleTVClassification:
    monitor_id: int | None
    event: WatchEvent | None
    disposition: str
    reason: str


def _positive_id(value: int, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AppleTVWatchError(f"{name} must be a positive integer")
    return value


def create_or_rotate_integration(db: sqlite3.Connection, *, user_id: int) -> IntegrationSecret:
    """Create fresh HMAC material and reveal the secret exactly once."""
    _positive_id(user_id, name="user_id")
    if db.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
        raise AppleTVWatchError("user does not exist")
    key_id = f"atv_{secrets.token_urlsafe(18)}"
    secret = secrets.token_urlsafe(32)
    encrypted_secret = encrypt(secret)
    db.execute(
        """INSERT INTO watch_integrations
           (user_id, source, key_id, encrypted_secret, enabled)
           VALUES (?, 'apple_tv', ?, ?, 1)
           ON CONFLICT(user_id, source) DO UPDATE SET
               key_id = excluded.key_id,
               encrypted_secret = excluded.encrypted_secret,
               enabled = 1,
               last_seen_at = NULL,
               updated_at = datetime('now')""",
        (user_id, key_id, encrypted_secret),
    )
    db.commit()
    return IntegrationSecret(key_id=key_id, secret=secret)


def disable_integration(db: sqlite3.Connection, *, user_id: int) -> bool:
    """Disable the owner's Apple TV bridge without revealing or deleting its secret."""
    _positive_id(user_id, name="user_id")
    cursor = db.execute(
        """UPDATE watch_integrations SET enabled = 0, updated_at = datetime('now')
           WHERE user_id = ? AND source = 'apple_tv'""",
        (user_id,),
    )
    db.commit()
    return cursor.rowcount == 1


def integration_status(db: sqlite3.Connection, *, user_id: int) -> dict[str, object] | None:
    """Return public status fields only."""
    row = db.execute(
        """SELECT key_id, enabled, last_seen_at, created_at, updated_at
           FROM watch_integrations WHERE user_id = ? AND source = 'apple_tv'""",
        (user_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def verify_signed_request(
    db: sqlite3.Connection,
    *,
    raw_body: bytes,
    key_id: str,
    timestamp_header: str,
    signature_header: str,
    now_epoch: int | None = None,
) -> VerifiedIntegration:
    """Authenticate exact raw bytes and return the integration owner."""
    if len(raw_body) > MAX_APPLE_TV_EVENT_BYTES:
        raise AppleTVWatchError("request body is too large")
    if not isinstance(key_id, str) or not 16 <= len(key_id) <= 128:
        raise AppleTVWatchError("invalid integration signature")
    try:
        signed_at = int(timestamp_header)
    except (TypeError, ValueError) as exc:
        raise AppleTVWatchError("invalid integration signature") from exc
    current = int(time.time()) if now_epoch is None else now_epoch
    if abs(current - signed_at) > SIGNATURE_MAX_AGE_SECONDS:
        raise AppleTVWatchError("invalid integration signature")
    signature_match = _SIGNATURE_RE.fullmatch(signature_header or "")
    if signature_match is None:
        raise AppleTVWatchError("invalid integration signature")
    row = db.execute(
        """SELECT id, user_id, key_id, encrypted_secret FROM watch_integrations
           WHERE key_id = ? AND source = 'apple_tv' AND enabled = 1""",
        (key_id,),
    ).fetchone()
    if row is None:
        raise AppleTVWatchError("invalid integration signature")
    try:
        shared_secret = decrypt(row["encrypted_secret"])
    except (BinasciiError, InvalidTag, TypeError, ValueError):
        raise AppleTVWatchError("invalid integration signature") from None
    signed_payload = f"{signed_at}.".encode() + raw_body
    expected = hmac.new(shared_secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    if not secrets.compare_digest(expected, signature_match.group(1)):
        raise AppleTVWatchError("invalid integration signature")
    seen_at = datetime.fromtimestamp(current, timezone.utc).strftime(_UTC_FORMAT)
    db.execute(
        "UPDATE watch_integrations SET last_seen_at = ?, updated_at = ? WHERE id = ?",
        (seen_at, seen_at, int(row["id"])),
    )
    db.commit()
    return VerifiedIntegration(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        key_id=row["key_id"],
        signed_at=signed_at,
    )


def _bounded_text(payload: dict[str, object], name: str, *, maximum: int) -> str:
    value = payload.get(name)
    if not isinstance(value, str):
        raise AppleTVWatchError(f"{name} must be text")
    normalized = " ".join(value.split()).strip()
    if not 1 <= len(normalized) <= maximum:
        raise AppleTVWatchError(f"{name} is invalid")
    return normalized


def _optional_positive_integer(payload: dict[str, object], name: str) -> int | None:
    value = payload.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AppleTVWatchError(f"{name} must be a positive integer")
    return value


def parse_observation(raw_body: bytes, *, reference_epoch: int | None = None) -> AppleTVObservation:
    """Parse a bounded JSON observation without accepting coercions."""
    try:
        payload = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AppleTVWatchError("request body must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise AppleTVWatchError("request body must be a JSON object")
    event_id = _bounded_text(payload, "event_id", maximum=200)
    observed_at = _bounded_text(payload, "observed_at", maximum=40)
    try:
        observed_datetime = datetime.strptime(observed_at, _UTC_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise AppleTVWatchError("observed_at must be canonical UTC") from exc
    if reference_epoch is not None and abs(observed_datetime.timestamp() - reference_epoch) > 300:
        raise AppleTVWatchError("observed_at must be fresh")
    device_id = _bounded_text(payload, "device_id", maximum=120)
    app_name = _bounded_text(payload, "app_name", maximum=120)
    series_title = _bounded_text(payload, "series_title", maximum=300)
    season = _optional_positive_integer(payload, "season")
    episode = _optional_positive_integer(payload, "episode")
    position = payload.get("position_seconds")
    duration = payload.get("duration_seconds")
    if isinstance(position, bool) or not isinstance(position, (int, float)) or position < 0:
        raise AppleTVWatchError("position_seconds must be non-negative")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration <= 0:
        raise AppleTVWatchError("duration_seconds must be positive")
    if float(position) > float(duration) * 1.2:
        raise AppleTVWatchError("position_seconds is inconsistent with duration_seconds")
    return AppleTVObservation(
        event_id=event_id,
        observed_at=observed_at,
        device_id=device_id,
        app_name=app_name,
        series_title=series_title,
        season=season,
        episode=episode,
        position_seconds=float(position),
        duration_seconds=float(duration),
    )


def _identity(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.findall(r"[\w]+", normalized, flags=re.UNICODE))


def _monitor_titles(row: sqlite3.Row) -> set[str]:
    titles = {_identity(str(row["title"]))}
    try:
        aliases = json.loads(row["search_titles_json"] or "[]")
    except (TypeError, json.JSONDecodeError):
        aliases = []
    if isinstance(aliases, list):
        titles.update(_identity(item) for item in aliases if isinstance(item, str))
    return {title for title in titles if title}


def classify_observation(
    db: sqlite3.Connection,
    *,
    user_id: int,
    observation: AppleTVObservation,
) -> AppleTVClassification:
    """Resolve exact owner media or return a fail-closed classification."""
    if "crunchyroll" not in _identity(observation.app_name):
        return AppleTVClassification(None, None, "ignored", "non_crunchyroll_app")
    title_identity = _identity(observation.series_title)
    candidates = []
    for row in db.execute(
        """SELECT id, title, search_titles_json, season_number
           FROM media_monitors WHERE user_id = ?""",
        (user_id,),
    ).fetchall():
        if title_identity not in _monitor_titles(row):
            continue
        monitor_season = int(row["season_number"] or 0)
        if observation.season is not None and monitor_season not in {0, observation.season}:
            continue
        candidates.append(row)
    if not candidates:
        return AppleTVClassification(None, None, "ignored", "title_not_matched")
    if len(candidates) != 1:
        return AppleTVClassification(None, None, "ignored", "title_match_ambiguous")
    monitor = candidates[0]
    monitor_season = int(monitor["season_number"] or 0)
    exact_identity = (
        observation.season is not None
        and observation.episode is not None
        and monitor_season in {0, observation.season}
    )
    event = WatchEvent.progress(
        source="apple_tv",
        episode=observation.episode,
        event_key=observation.event_id,
        playback_fraction=observation.playback_fraction,
        confidence="exact" if exact_identity else "inferred",
    )
    return AppleTVClassification(
        monitor_id=int(monitor["id"]),
        event=event,
        disposition="accepted" if exact_identity else "confirmation_required",
        reason="exact_episode_identity" if exact_identity else "inferred_episode_identity",
    )
