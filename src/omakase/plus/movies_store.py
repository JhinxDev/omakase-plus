"""Parameterised movie-only persistence; never writes anime or watch history."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

_CATALOG_FIELDS = ("imdb_id", "tmdb_id", "title", "year", "poster_url", "backdrop_url", "overview")
_REQUEST_FIELDS = {"backend_id", "state", "code", "detail", "retry_after", "retry_at"}
_RECEIPT_FIELDS = (
    "fingerprint",
    "plex_key",
    "resolution",
    "selected_audio",
    "edition",
    "state",
    "code",
    "detail",
    "http_status",
    "bytes_read",
    "verified_at",
    "expires_at",
    "retry_after",
    "retry_at",
)


def require_user(conn: sqlite3.Connection, user_id: int) -> None:
    if type(user_id) is not int or user_id <= 0:
        raise ValueError("Movie user identity is invalid")
    if conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
        raise ValueError("Movie user does not exist")


def get_movie(conn: sqlite3.Connection, user_id: int, imdb_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM movie_catalog WHERE user_id = ? AND imdb_id = ?", (user_id, imdb_id)
    ).fetchone()
    return dict(row) if row is not None else None


def upsert_movie(
    conn: sqlite3.Connection,
    user_id: int,
    metadata: dict[str, Any],
    *,
    now: str,
    identity_verified: bool = True,
) -> dict[str, Any]:
    require_user(conn, user_id)
    imdb_id, tmdb_id = metadata.get("imdb_id"), metadata.get("tmdb_id")
    if not isinstance(imdb_id, str) or not re.fullmatch(r"tt[0-9]{7,10}", imdb_id):
        raise ValueError("Movie IMDb identity is invalid")
    if tmdb_id is not None and (type(tmdb_id) is not int or not 0 < tmdb_id <= 2_147_483_647):
        raise ValueError("Movie TMDB identity is invalid")
    existing = get_movie(conn, user_id, imdb_id)
    if existing and existing["tmdb_id"] and tmdb_id and existing["tmdb_id"] != tmdb_id:
        raise ValueError("Movie identity mapping conflicts with the saved movie")
    if tmdb_id is not None:
        conflict = conn.execute(
            "SELECT imdb_id FROM movie_catalog WHERE user_id = ? AND tmdb_id = ?",
            (user_id, tmdb_id),
        ).fetchone()
        if conflict is not None and conflict["imdb_id"] != imdb_id:
            raise ValueError("Movie identity mapping conflicts with the saved movie")
    if existing and existing["identity_verified"] and not identity_verified:
        return existing
    values = dict(metadata)
    if existing:
        for key in _CATALOG_FIELDS:
            if values.get(key) is None:
                values[key] = existing.get(key)
    try:
        conn.execute(
            "INSERT INTO movie_catalog "
            "(user_id, imdb_id, tmdb_id, title, year, poster_url, backdrop_url, overview, identity_verified, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, imdb_id) DO UPDATE SET "
            "tmdb_id=excluded.tmdb_id, title=excluded.title, year=excluded.year, "
            "poster_url=excluded.poster_url, backdrop_url=excluded.backdrop_url, "
            "overview=excluded.overview, identity_verified=excluded.identity_verified, updated_at=excluded.updated_at",
            (user_id, *(values.get(key) for key in _CATALOG_FIELDS), int(identity_verified), now),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        raise ValueError("Movie metadata could not be saved safely") from None
    if identity_verified:
        mark_metadata_check(conn, user_id, imdb_id, now=now)
        if metadata.get("genres") is not None:
            save_movie_genres(conn, user_id, imdb_id, metadata["genres"])
    result = get_movie(conn, user_id, imdb_id)
    assert result is not None
    return result


def list_movies(
    conn: sqlite3.Connection, user_id: int, *, requested_only: bool = False
) -> list[dict[str, Any]]:
    require_user(conn, user_id)
    query = "SELECT c.* FROM movie_catalog c WHERE c.user_id = ?"
    if requested_only:
        query += " AND EXISTS (SELECT 1 FROM movie_requests r WHERE r.user_id=c.user_id AND r.imdb_id=c.imdb_id)"
    query += " ORDER BY c.title COLLATE NOCASE, c.imdb_id LIMIT 100"
    return [dict(row) for row in conn.execute(query, (user_id,)).fetchall()]


def collection_movies(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    native_ids: set[str],
    requested_only: bool = False,
) -> list[dict[str, Any]]:
    """Return bounded personal/native membership, excluding mere catalog cache."""
    require_user(conn, user_id)
    if (
        not isinstance(native_ids, set)
        or len(native_ids) > 1100
        or any(
            not isinstance(identity, str) or not re.fullmatch(r"tt[0-9]{7,10}", identity)
            for identity in native_ids
        )
    ):
        raise ValueError("Movie native inventory identities are invalid")

    # SQL candidate limits and the final merge must agree for Unicode titles too.
    conn.create_function("omakase_movie_collection_casefold", 1, str.casefold, deterministic=True)
    prefix = "SELECT c.* FROM movie_catalog c WHERE c.user_id = ? AND "
    order = " ORDER BY omakase_movie_collection_casefold(c.title), c.imdb_id LIMIT 100"
    personal = (
        "EXISTS (SELECT 1 FROM movie_requests r WHERE r.user_id=c.user_id AND r.imdb_id=c.imdb_id)"
    )
    if not requested_only:
        personal += (
            " OR EXISTS (SELECT 1 FROM movie_history h "
            "WHERE h.user_id=c.user_id AND h.imdb_id=c.imdb_id "
            "AND (h.status_source != 'none' OR h.rating_source != 'none'))"
        )
    candidates = {
        row["imdb_id"]: dict(row)
        for row in conn.execute(prefix + "(" + personal + ")" + order, (user_id,))
    }
    ordered_native = sorted(native_ids) if not requested_only else []
    for start in range(0, len(ordered_native), 400):
        chunk = ordered_native[start : start + 400]
        placeholders = ",".join("?" for _ in chunk)
        for row in conn.execute(
            prefix + "c.imdb_id IN (" + placeholders + ")" + order, (user_id, *chunk)
        ):
            candidates[row["imdb_id"]] = dict(row)
    return sorted(candidates.values(), key=lambda row: (row["title"].casefold(), row["imdb_id"]))[
        :100
    ]


def get_request(conn: sqlite3.Connection, user_id: int, imdb_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM movie_requests WHERE user_id = ? AND imdb_id = ?", (user_id, imdb_id)
    ).fetchone()
    return dict(row) if row is not None else None


def ensure_request(
    conn: sqlite3.Connection,
    user_id: int,
    imdb_id: str,
    *,
    now: str,
    edition: str | None,
) -> dict[str, Any]:
    existing = get_request(conn, user_id, imdb_id)
    if existing and edition is not None and existing["requested_edition"] != edition:
        raise ValueError("Movie edition differs from the existing request")
    conn.execute(
        "INSERT OR IGNORE INTO movie_requests "
        "(user_id, imdb_id, requested_edition, detail, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, imdb_id, edition, "Requested; delivery has not been verified.", now, now),
    )
    conn.commit()
    result = get_request(conn, user_id, imdb_id)
    assert result is not None
    return result


def update_request(
    conn: sqlite3.Connection,
    user_id: int,
    imdb_id: str,
    *,
    now: str,
    investigation: bool = False,
    **values: Any,
) -> None:
    if not values or not values.keys() <= _REQUEST_FIELDS:
        raise ValueError("Movie request update is invalid")
    columns = sorted(values)
    assignment = ", ".join(f"{name} = ?" for name in columns)
    if investigation:
        assignment += ", investigations = investigations + 1"
    conn.execute(
        f"UPDATE movie_requests SET {assignment}, updated_at = ? WHERE user_id = ? AND imdb_id = ?",
        (*(values[name] for name in columns), now, user_id, imdb_id),
    )
    conn.commit()


def get_submission(
    conn: sqlite3.Connection, backend_scope: str, imdb_id: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM movie_submissions WHERE backend_scope = ? AND imdb_id = ?",
        (backend_scope, imdb_id),
    ).fetchone()
    return dict(row) if row is not None else None


def claim_submission(
    conn: sqlite3.Connection,
    backend_scope: str,
    imdb_id: str,
    tmdb_id: int,
    *,
    now: str,
    retry_rejected: bool = False,
) -> bool:
    existing = get_submission(conn, backend_scope, imdb_id)
    if existing is not None:
        if existing["tmdb_id"] != tmdb_id:
            raise ValueError("Movie identity mapping conflicts with the original submission")
        if retry_rejected and existing["code"] == "connection_rejected":
            cursor = conn.execute(
                "UPDATE movie_submissions SET attempted_at=?, backend_id=NULL, state='deferred', "
                "code='submission_pending', detail='An explicit authenticated retry is being checked.', "
                "retry_after=NULL, retry_at=NULL WHERE backend_scope=? AND imdb_id=? "
                "AND tmdb_id=? AND code='connection_rejected'",
                (now, backend_scope, imdb_id, tmdb_id),
            )
            conn.commit()
            return cursor.rowcount == 1
        return False
    cursor = conn.execute(
        "INSERT OR IGNORE INTO movie_submissions (backend_scope, imdb_id, tmdb_id, attempted_at) VALUES (?, ?, ?, ?)",
        (backend_scope, imdb_id, tmdb_id, now),
    )
    conn.commit()
    winner = get_submission(conn, backend_scope, imdb_id)
    if winner is not None and winner["tmdb_id"] != tmdb_id:
        raise ValueError("Movie identity mapping conflicts with the original submission")
    return cursor.rowcount == 1


def update_submission(
    conn: sqlite3.Connection, backend_scope: str, imdb_id: str, **values: Any
) -> None:
    if not values or not values.keys() <= _REQUEST_FIELDS:
        raise ValueError("Movie submission update is invalid")
    columns = sorted(values)
    assignment = ", ".join(f"{name} = ?" for name in columns)
    conn.execute(
        f"UPDATE movie_submissions SET {assignment} WHERE backend_scope = ? AND imdb_id = ?",
        (*(values[name] for name in columns), backend_scope, imdb_id),
    )
    conn.commit()


def get_verification(
    conn: sqlite3.Connection, user_id: int, imdb_id: str, fingerprint: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM movie_verifications WHERE user_id = ? AND imdb_id = ? AND fingerprint = ?",
        (user_id, imdb_id, fingerprint),
    ).fetchone()
    return dict(row) if row is not None else None


def save_verification(
    conn: sqlite3.Connection, user_id: int, imdb_id: str, receipt: dict[str, Any]
) -> None:
    fields = ", ".join(_RECEIPT_FIELDS)
    placeholders = ", ".join("?" for _ in _RECEIPT_FIELDS)
    updates = ", ".join(f"{name}=excluded.{name}" for name in _RECEIPT_FIELDS)
    conn.execute(
        f"INSERT INTO movie_verifications (user_id, imdb_id, {fields}) "
        f"VALUES (?, ?, {placeholders}) ON CONFLICT(user_id, imdb_id, fingerprint) DO UPDATE SET {updates}",
        (user_id, imdb_id, *(receipt.get(key) for key in _RECEIPT_FIELDS)),
    )
    conn.commit()


def invalidate_verification(conn: sqlite3.Connection, user_id: int, imdb_id: str) -> None:
    conn.execute(
        "DELETE FROM movie_verifications WHERE user_id = ? AND imdb_id = ? "
        "AND code NOT IN ('provider_restricted', 'rate_limited')",
        (user_id, imdb_id),
    )
    conn.commit()


def mark_metadata_check(
    conn: sqlite3.Connection,
    user_id: int,
    imdb_id: str,
    *,
    now: str,
    error_code: str | None = None,
    retry_after: str | None = None,
    retry_at: str | None = None,
) -> None:
    conn.execute(
        "UPDATE movie_catalog SET metadata_checked_at=?, metadata_error_code=?, "
        "metadata_retry_after=?, metadata_retry_at=? WHERE user_id=? AND imdb_id=?",
        (now, error_code, retry_after, retry_at, user_id, imdb_id),
    )
    conn.commit()


def list_movie_genres(conn: sqlite3.Connection, user_id: int, imdb_id: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT genre_id AS id, name FROM movie_genres WHERE user_id=? AND imdb_id=? ORDER BY genre_id",
            (user_id, imdb_id),
        ).fetchall()
    ]


def save_movie_genres(
    conn: sqlite3.Connection, user_id: int, imdb_id: str, genres: list[dict[str, Any]]
) -> None:
    if not isinstance(genres, list) or len(genres) > 50:
        raise ValueError("Movie genre metadata is invalid")
    seen = set()
    for genre in genres:
        if (
            not isinstance(genre, dict)
            or type(genre.get("id")) is not int
            or genre["id"] <= 0
            or genre["id"] in seen
            or not isinstance(genre.get("name"), str)
            or not 1 <= len(genre["name"]) <= 80
        ):
            raise ValueError("Movie genre metadata is invalid")
        seen.add(genre["id"])
    conn.execute("DELETE FROM movie_genres WHERE user_id=? AND imdb_id=?", (user_id, imdb_id))
    conn.executemany(
        "INSERT INTO movie_genres (user_id, imdb_id, genre_id, name) VALUES (?, ?, ?, ?)",
        [(user_id, imdb_id, genre["id"], genre["name"]) for genre in genres],
    )
    conn.commit()
