"""Bounded personal browser resume, independent of watch progress.

Only trusted integration constructs targets and snapshots. No locator or duration
from browser events is accepted. Public results deliberately omit bindings and
source snapshots. Caller-owned transactions are never committed here.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import secrets
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass

_CLOCK_ID = secrets.token_hex(16)
_SHA = re.compile(r"[0-9a-f]{64}\Z")


class PlaybackStoreError(ValueError):
    """Safe public validation failure."""

    def __init__(self, message, *, code="event_invalid"):
        super().__init__(message)
        self.code = code


def _number(value, low, high, name):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not low <= value <= high
    ):
        raise PlaybackStoreError(f"Invalid {name}")
    return float(value)


def _positive(value, name):
    if type(value) is not int or not 1 <= value <= 2**63 - 1:
        raise PlaybackStoreError(f"Invalid {name}")


def _library(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise PlaybackStoreError("Invalid library")


@dataclass(frozen=True)
class ResolvedTarget:
    user_id: int
    library_id: str
    canonical_id: str
    season_number: int
    episode_number: int
    media_version: str
    duration_seconds: float
    domain: str = "anime"

    def __post_init__(self):
        for name in ("user_id", "season_number", "episode_number"):
            _positive(getattr(self, name), name)
        if (
            self.domain != "anime"
            or not isinstance(self.canonical_id, str)
            or not re.fullmatch(r"[1-9][0-9]{0,18}", self.canonical_id)
        ):
            raise PlaybackStoreError("Invalid exact anime target")
        if not isinstance(self.media_version, str) or not _SHA.fullmatch(self.media_version):
            raise PlaybackStoreError("Invalid media version")
        _library(self.library_id)
        _number(self.duration_seconds, 1, 43200, "duration")


def _key(target):
    if not isinstance(target, ResolvedTarget):
        raise PlaybackStoreError("Resolved exact target required")
    values = asdict(target)
    values.pop("duration_seconds")
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def _now(now):
    return _number(time.time() if now is None else now, 0, 1e12, "server time")


def _binding(binding):
    if not isinstance(binding, str) or not _SHA.fullmatch(binding):
        raise PlaybackStoreError("Invalid session binding")


@contextmanager
def _atomic(db):
    name = "playback_" + secrets.token_hex(8)
    db.execute(f"SAVEPOINT {name}")
    try:
        yield
    except BaseException:
        db.execute(f"ROLLBACK TO {name}")
        db.execute(f"RELEASE {name}")
        raise
    else:
        db.execute(f"RELEASE {name}")


def _row(db, sql, args=()):
    cursor = db.execute(sql, args)
    row = cursor.fetchone()
    return dict(zip((item[0] for item in cursor.description), row)) if row else None


def _resume(row):
    intervals = json.loads(row["intervals_json"])
    return {
        key: row[key]
        for key in (
            "user_id",
            "library_id",
            "domain",
            "canonical_id",
            "season_number",
            "episode_number",
            "media_version",
            "duration_seconds",
            "position_seconds",
            "revision",
            "audio_id",
            "text_id",
            "updated_at",
        )
    } | {"covered_seconds": sum(end - start for start, end in intervals)}


def get_resume(db, *, target):
    row = _row(db, "SELECT * FROM web_playback_resumes WHERE target_key=?", (_key(target),))
    return _resume(row) if row else None


def _session(db, session_id, user_id, binding_digest, now, expected_snapshot=None):
    _positive(user_id, "viewer")
    _binding(binding_digest)
    if not isinstance(session_id, str) or len(session_id) > 128:
        raise PlaybackStoreError("Playback session unavailable", code="session_unavailable")
    row = _row(
        db,
        "SELECT s.*, r.revision AS current_revision FROM web_playback_sessions s JOIN web_playback_resumes r USING(target_key) WHERE session_id=? AND s.user_id=?",
        (session_id, user_id),
    )
    if not row or row["closed"]:
        raise PlaybackStoreError("Playback session unavailable", code="session_unavailable")
    if not hmac.compare_digest(row["binding_digest"], binding_digest):
        raise PlaybackStoreError("Playback session unavailable", code="session_auth_mismatch")
    if row["revision"] != row["current_revision"]:
        raise PlaybackStoreError("Playback session unavailable", code="session_revision_mismatch")
    if now < row["last_seen"] or now >= row["expires_at"] or now - row["last_seen"] >= 1200:
        raise PlaybackStoreError("Playback session unavailable", code="session_lease_invalid")
    if expected_snapshot is not None and not hmac.compare_digest(
        row["snapshot_json"], _snapshot_json(expected_snapshot)
    ):
        raise PlaybackStoreError(
            "Playback authorization snapshot changed", code="session_snapshot_mismatch"
        )
    return row


def _public_session(db, row):
    return _get_resume_by_key(db, row["target_key"]) | {
        "session_id": row["session_id"],
        "expires_at": row["expires_at"],
    }


def _get_resume_by_key(db, key):
    return _resume(_row(db, "SELECT * FROM web_playback_resumes WHERE target_key=?", (key,)))


def _snapshot_json(snapshot):
    if not isinstance(snapshot, dict) or not all(
        isinstance(snapshot.get(k), dict) for k in ("membership", "policy", "source")
    ):
        raise PlaybackStoreError("Resolved authorization snapshot required")
    try:
        snapshot_json = json.dumps(snapshot, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        raise PlaybackStoreError("Invalid authorization snapshot") from None
    if len(snapshot_json.encode()) > 16384:
        raise PlaybackStoreError("Authorization snapshot too large")
    return snapshot_json


def watch_barrier(db, *, user_id, anilist_id, season_number):
    return db.execute(
        "SELECT COALESCE(MAX(e.id),0) FROM watch_events e JOIN watch_entries w "
        "ON w.id=e.watch_entry_id WHERE e.user_id=? AND w.anilist_id=? "
        "AND w.season_number=? AND e.disposition='accepted' "
        "AND e.kind IN ('correction','reopened')",
        (user_id, int(anilist_id), season_number),
    ).fetchone()[0]


def create_session(db, *, target, binding_digest, snapshot, now=None):
    key, now = _key(target), _now(now)
    _binding(binding_digest)
    snapshot_json = _snapshot_json(snapshot)
    source_snapshot = json.loads(snapshot_json)["source"]
    publication_identity = source_snapshot.get("identity")
    if (
        not isinstance(publication_identity, str)
        or not _SHA.fullmatch(publication_identity)
        or source_snapshot.get("media_version") != target.media_version
    ):
        publication_identity = None
    with _atomic(db):
        cleanup_sessions(db, now=now)
        db.execute(
            "INSERT OR IGNORE INTO web_playback_resumes(target_key,user_id,library_id,domain,canonical_id,season_number,episode_number,media_version,duration_seconds,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                key,
                target.user_id,
                target.library_id,
                target.domain,
                target.canonical_id,
                target.season_number,
                target.episode_number,
                target.media_version,
                target.duration_seconds,
                now,
            ),
        )
        stored = _row(db, "SELECT * FROM web_playback_resumes WHERE target_key=?", (key,))
        if stored["duration_seconds"] != target.duration_seconds:
            raise PlaybackStoreError("Exact media duration changed")
        db.execute(
            "UPDATE web_playback_sessions SET closed=1,closed_at=? WHERE target_key=? AND closed=0",
            (now, key),
        )
        db.execute(
            "UPDATE web_playback_resumes SET revision=revision+1,publication_identity=? WHERE target_key=?",
            (publication_identity, key),
        )
        revision = stored["revision"] + 1

        valid = db.execute(
            "SELECT session_id FROM web_playback_sessions WHERE user_id=? AND closed=0 AND expires_at>? AND last_seen>? ORDER BY created_at,session_id",
            (target.user_id, now, now - 1200),
        ).fetchall()
        for row in valid[:-1]:
            db.execute(
                "UPDATE web_playback_sessions SET closed=1,closed_at=? WHERE session_id=?",
                (now, row[0]),
            )
        sid = secrets.token_urlsafe(32)
        db.execute(
            "INSERT INTO web_playback_sessions(session_id,target_key,user_id,binding_digest,snapshot_json,revision,created_at,expires_at,last_seen) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                sid,
                key,
                target.user_id,
                binding_digest,
                snapshot_json,
                revision,
                now,
                now + 14400,
                now,
            ),
        )
        barrier = watch_barrier(
            db,
            user_id=target.user_id,
            anilist_id=target.canonical_id,
            season_number=target.season_number,
        )
        db.execute(
            "UPDATE web_playback_sessions SET watch_barrier=? WHERE session_id=?", (barrier, sid)
        )
        db.execute(
            "INSERT OR IGNORE INTO web_playback_cycle_coverage(target_key,barrier) VALUES(?,?)",
            (key, barrier),
        )
        return get_session(
            db, session_id=sid, user_id=target.user_id, binding_digest=binding_digest, now=now
        )


def get_session(
    db, *, session_id, user_id, binding_digest, expected_snapshot: dict | None = None, now=None
):
    return _public_session(
        db, _session(db, session_id, user_id, binding_digest, _now(now), expected_snapshot)
    )


def close_session(
    db, *, session_id, user_id, binding_digest, expected_snapshot: dict | None = None, now=None
):
    now = _now(now)
    with _atomic(db):
        _session(db, session_id, user_id, binding_digest, now, expected_snapshot)
        db.execute(
            "UPDATE web_playback_sessions SET closed=1,closed_at=? WHERE session_id=?",
            (now, session_id),
        )


def _union(intervals, added):
    merged = []
    for start, end in sorted(intervals + [added]):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])
    # Retain the largest observed intervals. Dropping evidence is conservative;
    # merging across an unobserved gap would fabricate coverage.
    if len(merged) > 512:
        merged = sorted(
            sorted(merged, key=lambda interval: (-(interval[1] - interval[0]), interval[0]))[:512]
        )
    return merged


def event_fingerprint(position, playing, event, rate, audio_id, text_id):
    """Stable normalized event identity, also used for a closed terminal retry."""
    return hashlib.sha256(
        json.dumps([float(position), playing, event, float(rate), audio_id, text_id]).encode()
    ).hexdigest()


def record_event(
    db,
    *,
    session_id,
    user_id,
    binding_digest,
    seq,
    position_seconds,
    playing,
    event="progress",
    playback_rate=1,
    audio_id=None,
    text_id=None,
    expected_snapshot: dict | None = None,
    now=None,
):
    injected_clock = now is not None
    now = _now(now)
    _positive(seq, "sequence")
    if type(playing) is not bool or event not in {"progress", "play", "pause", "seek", "ended"}:
        raise PlaybackStoreError("Invalid playback event")
    rate = _number(playback_rate, 0.25, 2, "playback rate")
    for track in (audio_id, text_id):
        if track is not None and (
            not isinstance(track, str) or len(track) > 128 or any(ord(c) < 32 for c in track)
        ):
            raise PlaybackStoreError("Invalid track identifier")
    with _atomic(db):
        s = _session(db, session_id, user_id, binding_digest, now, expected_snapshot)
        r = _row(db, "SELECT * FROM web_playback_resumes WHERE target_key=?", (s["target_key"],))
        pos = _number(position_seconds, 0, r["duration_seconds"], "position")
        fingerprint = event_fingerprint(pos, playing, event, rate, audio_id, text_id)
        previous = _row(
            db, "SELECT * FROM web_playback_events WHERE session_id=? AND seq=?", (session_id, seq)
        )
        if previous:
            if previous["fingerprint"] != fingerprint:
                raise PlaybackStoreError("Conflicting playback replay")
            return json.loads(previous["result_json"])
        if seq <= s["last_seq"]:
            raise PlaybackStoreError("Stale playback sequence")
        if s["last_seq"] and now - s["last_seen"] < 0.25:
            raise PlaybackStoreError("Playback event rate exceeded", code="event_rate_limited")
        intervals = json.loads(r["intervals_json"])
        credited = None
        mono = time.monotonic()
        if s["anchor_time"] is not None:
            elapsed = now - s["anchor_time"]
            # Wall time is also checked; process changes reset the monotonic anchor.
            if not injected_clock and s["clock_id"] == _CLOCK_ID:
                elapsed = min(elapsed, max(0, mono - s["anchor_monotonic"]))
            if not injected_clock and s["clock_id"] != _CLOCK_ID:
                elapsed = 0
            delta = pos - s["anchor_position"]
            if (
                s["anchor_playing"]
                and (playing or event in {"pause", "ended"})
                and event in {"progress", "play", "pause", "ended"}
                and 0 < elapsed <= 60
                and 0 < delta <= elapsed * s["anchor_rate"] + 0.5
            ):
                intervals = _union(intervals, [s["anchor_position"], pos])
                credited = [s["anchor_position"], pos]
        db.execute(
            "UPDATE web_playback_resumes SET position_seconds=?,intervals_json=?,audio_id=COALESCE(?,audio_id),text_id=COALESCE(?,text_id),updated_at=? WHERE target_key=? AND revision=?",
            (pos, json.dumps(intervals), audio_id, text_id, now, s["target_key"], s["revision"]),
        )
        anchor_playing = playing and event not in {"seek", "pause", "ended"}
        db.execute(
            "UPDATE web_playback_sessions SET last_seq=?,last_seen=?,anchor_position=?,anchor_time=?,anchor_monotonic=?,clock_id=?,anchor_playing=?,anchor_rate=? WHERE session_id=?",
            (seq, now, pos, now, mono, _CLOCK_ID, int(anchor_playing), rate, session_id),
        )
        result = _get_resume_by_key(db, s["target_key"])
        cycle = db.execute(
            "SELECT intervals_json FROM web_playback_cycle_coverage WHERE target_key=? AND barrier=?",
            (s["target_key"], s["watch_barrier"]),
        ).fetchone()
        cycle_intervals = json.loads(cycle[0]) if cycle else []
        if credited:
            cycle_intervals = _union(cycle_intervals, credited)
            db.execute(
                "INSERT INTO web_playback_cycle_coverage(target_key,barrier,intervals_json) VALUES(?,?,?) "
                "ON CONFLICT(target_key,barrier) DO UPDATE SET intervals_json=excluded.intervals_json",
                (s["target_key"], s["watch_barrier"], json.dumps(cycle_intervals)),
            )
        result.update(
            accepted_at=now,
            credited_seconds=0 if credited is None else credited[1] - credited[0],
            watch_barrier=s["watch_barrier"],
            watch_covered_seconds=sum(b - a for a, b in cycle_intervals),
        )
        db.execute(
            "INSERT INTO web_playback_events VALUES(?,?,?,?)",
            (session_id, seq, fingerprint, json.dumps(result)),
        )
        # A finite receipt window preserves recent retries while old sequence
        # numbers remain rejected by last_seq after their receipt is retired.
        db.execute(
            "DELETE FROM web_playback_events WHERE session_id=? AND seq < "
            "(SELECT seq FROM web_playback_events WHERE session_id=? ORDER BY seq DESC LIMIT 1 OFFSET 255)",
            (session_id, session_id),
        )
        return result


def active_media_holds(db, *, library_id=None, media_version=None, now=None):
    now = _now(now)
    clauses = ["s.closed=0", "s.expires_at>?", "s.last_seen>?", "s.revision=r.revision"]
    args = [now, now - 1200]
    if library_id is not None:
        _library(library_id)
        clauses.append("r.library_id=?")
        args.append(library_id)
    if media_version is not None:
        if not isinstance(media_version, str) or not _SHA.fullmatch(media_version):
            raise PlaybackStoreError("Invalid media version")
        clauses.append("r.media_version=?")
        args.append(media_version)
    cursor = db.execute(
        "SELECT r.user_id,r.library_id,r.domain,r.canonical_id,r.season_number,r.episode_number,r.media_version,s.expires_at FROM web_playback_sessions s JOIN web_playback_resumes r USING(target_key) WHERE "
        + " AND ".join(clauses),
        args,
    )
    return [dict(zip((column[0] for column in cursor.description), row)) for row in cursor]


def cleanup_sessions(db, *, now=None) -> int:
    """Remove at most 200 sessions expired/closed for seven days and their events.

    Resume positions, interval evidence, and unrelated tables remain untouched.
    Uses a nested savepoint when the integration already owns a transaction.
    """
    cutoff = _now(now) - 7 * 86400
    with _atomic(db):
        expired = db.execute(
            "SELECT session_id FROM web_playback_sessions WHERE "
            "(closed=1 AND closed_at<=?) OR expires_at<=? OR last_seen<=? "
            "ORDER BY last_seen,session_id LIMIT 200",
            (cutoff, cutoff, cutoff - 1200),
        ).fetchall()
        for row in expired:
            # Explicit bounded dependent delete also works with foreign_keys=OFF.
            db.execute("DELETE FROM web_playback_events WHERE session_id=?", (row[0],))
            db.execute("DELETE FROM web_playback_sessions WHERE session_id=?", (row[0],))
        return len(expired)
