"""Persist public AniList synopsis text separately from private viewing evidence."""

from __future__ import annotations

import asyncio
import time

from omakase.plus.movies_store import require_user


def cached_synopsis(db, anilist_id: int) -> dict:
    row = db.execute(
        "SELECT description FROM anime_synopses WHERE anilist_id=?", (anilist_id,)
    ).fetchone()
    text = row["description"] if row else ""
    first, _, remaining = text.partition("\n")
    if len(first) > 420:
        split = first.rfind(" ", 0, 421)
        if split < 1:
            split = 420
        preview, rest = first[:split], first[split:].lstrip()
        remaining = "\n".join(part for part in (rest, remaining) if part)
    else:
        preview = first
    return {
        "description": text,
        "description_preview": preview,
        "description_more": remaining,
        "description_source": "AniList" if text else "",
    }


def store_synopsis(db, anilist_id: int, description: str) -> None:
    """Save normalized public catalog text, including an honest absent result."""
    # Sanitize here as well so every writer shares the same trust boundary.
    from omakase.plus.catalog_search import _plain

    description = "\n".join(line for line in _plain(description).splitlines() if line.strip())
    db.execute(
        """INSERT INTO anime_synopses(anilist_id,description,retry_at) VALUES (?,?,?)
           ON CONFLICT(anilist_id) DO UPDATE SET
           description=excluded.description,retry_at=excluded.retry_at""",
        (anilist_id, description, time.time() + (30 * 86400 if description else 86400)),
    )
    db.commit()


async def ensure_anime_synopsis(db, *, user_id: int, anilist_id: int, transport=None) -> dict:
    """Fill a cold cache with one public-only request, bounded to three seconds.

    Call only after the route has verified title ownership. A persisted retry
    timestamp throttles missing/error results across requests and app restarts.
    Existing catalog searches also populate this cache, avoiding duplicate reads.
    """
    from omakase.plus.catalog_search import _FIELDS, AnimeCatalogSearch, CatalogSearchError, _id

    require_user(db, user_id)
    anilist_id = _id(anilist_id)
    row = db.execute(
        "SELECT retry_at FROM anime_synopses WHERE anilist_id=?", (anilist_id,)
    ).fetchone()
    if row and row["retry_at"] > time.time():
        return cached_synopsis(db, anilist_id)
    # Claim the attempt before awaiting. Concurrent requests reuse stale text.
    db.execute(
        """INSERT INTO anime_synopses(anilist_id,description,retry_at) VALUES (?,'',?)
           ON CONFLICT(anilist_id) DO UPDATE SET retry_at=excluded.retry_at""",
        (anilist_id, time.time() + 3600),
    )
    db.commit()
    catalog = AnimeCatalogSearch(db, user_id, transport=transport)
    try:
        data = await asyncio.wait_for(
            catalog._request(
                "query($id:Int!){Media(id:$id,type:ANIME){" + _FIELDS + "}}",
                {"id": anilist_id},
                None,
            ),
            timeout=3,
        )
        metadata = catalog._metadata(data.get("Media"), anilist_id)
        store_synopsis(db, anilist_id, metadata["description"])
    except (CatalogSearchError, asyncio.TimeoutError):
        pass
    return cached_synopsis(db, anilist_id)
