"""Owner-scoped resume positions for Real-Debrid Cloud files."""

from __future__ import annotations

import re
import time

_KEY = re.compile(
    r"(?:t-[a-zA-Z0-9]{3,64}(?:-[1-9][0-9]{0,9})?"
    r"|d-[a-zA-Z0-9]{3,64}"
    r"|named:(?:anime|anime-film|movie|tv):[A-Za-z0-9]{1,20}:[0-9]{1,4}:[0-9]{1,4})"
)
_TITLE_LIMIT = 300
_MAX_KEYS = 60


def valid_key(value):
    return isinstance(value, str) and len(value) <= 128 and _KEY.fullmatch(value) is not None


def clock(seconds):
    value = max(0, int(seconds))
    hours, remainder = divmod(value, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def get_position(db, user_id, file_key):
    if not valid_key(file_key):
        return None
    row = db.execute(
        "SELECT position_seconds,duration_seconds,updated_at FROM cloud_playback_resumes "
        "WHERE user_id=? AND file_key=?",
        (user_id, file_key),
    ).fetchone()
    if row is None:
        return None
    position, duration = float(row[0]), float(row[1])
    if position <= 5 or position >= duration - 30:
        return None
    return {"position_seconds": position, "duration_seconds": duration,
            "label": clock(position), "updated_at": row[2]}


def positions(db, user_id, file_keys):
    keys = [key for key in dict.fromkeys(file_keys) if valid_key(key)][:_MAX_KEYS]
    if not keys:
        return {}
    rows = db.execute(
        "SELECT file_key,position_seconds,duration_seconds FROM cloud_playback_resumes "
        f"WHERE user_id=? AND file_key IN ({','.join('?' for _ in keys)})",
        (user_id, *keys),
    ).fetchall()
    result = {}
    for file_key, position, duration in rows:
        position, duration = float(position), float(duration)
        if position <= 5 or position >= duration - 30:
            continue
        result[file_key] = {"position_seconds": position, "label": clock(position)}
    return result


def save_position(db, *, user_id, file_key, title, position, duration, now=None):
    if not valid_key(file_key):
        raise ValueError("Unknown cloud file")
    if type(user_id) is not int or user_id < 1:
        raise ValueError("Unknown viewer")
    try:
        duration = float(duration)
        position = float(position)
    except (TypeError, ValueError):
        raise ValueError("Invalid playback position") from None
    if not 1 <= duration <= 43200 or not 0 <= position <= duration:
        raise ValueError("Invalid playback position")
    title = title if isinstance(title, str) else ""
    title = "".join(char for char in title if char.isprintable()).strip()[:_TITLE_LIMIT]
    if position < 5 or duration - position < 30:
        position = 0
    now = float(time.time() if now is None else now)
    db.execute(
        """INSERT INTO cloud_playback_resumes
           (user_id,file_key,title,position_seconds,duration_seconds,updated_at)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(user_id,file_key) DO UPDATE SET
             title=excluded.title, position_seconds=excluded.position_seconds,
             duration_seconds=excluded.duration_seconds, updated_at=excluded.updated_at""",
        (user_id, file_key, title, position, duration, now),
    )
    db.commit()
    if position == 0:
        return {"position_seconds": 0.0, "cleared": True}
    return {"position_seconds": position, "label": clock(position)}
