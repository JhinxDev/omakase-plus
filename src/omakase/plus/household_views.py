"""Secret-free selected-library read models; GETs never initialize or copy history."""

from __future__ import annotations

from omakase.plus import households as core
from omakase.plus.household_media import binding, physical_facts, public_metadata, viewer_history


def detail_url(scope, item_id):
    return f"/plus/libraries/{scope.library_id}/items/{item_id}"


def collection(db, scope, *, domain=None, query="", offset=0, limit=60):
    core.revalidate_scope(db, scope)
    core._require(domain in {None, "anime", "movie", "tv"}, "Choose a media type.", 422)
    core._require(
        type(offset) is int
        and 0 <= offset <= 10000
        and type(limit) is int
        and 1 <= limit <= 100
        and isinstance(query, str)
        and len(query) <= 200,
        "Invalid library filter.",
        422,
    )
    where, params = "library_id=? AND state='active'", [scope.library_id]
    if domain:
        where += " AND domain=?"
        params.append(domain)
    if query:
        # instr is literal: title searches do not turn '%'/'_' into wildcards.
        where += " AND instr(lower(title),lower(?))>0"
        params.append(query)
    total = db.execute("SELECT COUNT(*) FROM library_items WHERE " + where, params).fetchone()[0]
    rows = db.execute(
        "SELECT id FROM library_items WHERE " + where + " ORDER BY title,id LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    return {
        "cards": [detail(db, scope, item_id=row["id"]) for row in rows],
        "total": total,
        "offset": offset,
        "has_more": total > offset + limit,
        "query": query,
        "domain": domain,
        "scope": scope,
        "library_name": library_name(db, scope),
    }


def library_name(db, scope):
    core.revalidate_scope(db, scope)
    return db.execute("SELECT name FROM libraries WHERE id=?", (scope.library_id,)).fetchone()[0]


def detail(db, scope, *, item_id):
    media = binding(db, scope, item_id=item_id)
    item = core.LibraryRepository(db, scope).item(item_id)
    metadata = public_metadata(db, media)
    history = viewer_history(db, media)
    facts = physical_facts(db, media)
    interest = db.execute(
        "SELECT keep_available,active_use FROM library_item_interest WHERE library_item_id=? AND user_id=?",
        (item_id, scope.viewer_id),
    ).fetchone()
    request = db.execute(
        "SELECT state,result_code,detail FROM library_request_events WHERE library_item_id=? AND actor_user_id=? ORDER BY created_at DESC,id DESC LIMIT 1",
        (item_id, scope.viewer_id),
    ).fetchone()
    return {
        "id": item_id,
        "domain": media.domain,
        "canonical_id": media.canonical_id,
        "season_number": media.season,
        "title": item["title"],
        "metadata": metadata,
        "history": history,
        "facts": facts,
        "detail_url": detail_url(scope, item_id),
        "artwork_url": detail_url(scope, item_id) + "/artwork",
        "request": dict(request) if request else None,
        "keep_available": bool(interest and interest["keep_available"]),
        "active_use": bool(interest and interest["active_use"]),
        "scope": scope,
    }


def anime_cards(db, scope, *, viewer_id, query="", lifecycle="active"):
    """Compatibility cards for root-owned Home/Collection macros with explicit URLs."""
    core._require(scope.viewer_id == viewer_id, "Library viewer mismatch.", 403)
    rows = []
    for offset in range(0, 10001, 100):
        page = collection(db, scope, domain="anime", query=query, limit=100, offset=offset)
        rows.extend(page["cards"])
        if not page["has_more"]:
            break
    else:
        raise core.HouseholdError("This library needs a paginated collection view.", 422)
    cards = []
    for item in rows:
        history = item["history"]
        status = history["status"] if history else "untracked"
        covered = item["facts"]["verified_episodes"]
        progress = history["progress_episodes"] if history else 0
        total = history["episode_total"] if history else None
        title_lifecycle = (
            "completed" if status == "completed" else "in_progress" if progress else "waiting"
        )
        if (lifecycle == "active" and title_lifecycle == "completed") or (
            lifecycle not in {"active", "all"} and title_lifecycle != lifecycle
        ):
            continue
        cards.append(
            {
                **item,
                "anilist_id": int(item["canonical_id"]),
                "format": "Anime",
                "active": True,
                "required": 0,
                "usable": len(covered),
                "usable_episodes": covered,
                "plex_visible": 0,
                "state": "planned",
                "status_label": item["facts"]["label"],
                "providers": [],
                "provider_kinds": [],
                "provider_summary": "Shared library",
                "asset_count": 0,
                "release_qualities": [],
                "fallback_episodes": [],
                "lifecycle": title_lifecycle,
                "lifecycle_label": "Completed"
                if status == "completed"
                else "Not watched"
                if history is None
                else "Watching"
                if progress
                else "My list",
                "progress_episodes": progress,
                "episode_total": total,
                "next_episode": progress + 1 if history and status != "completed" else None,
                "progress_percent": min(100, 100 * progress // total) if total else 0,
                "watch_status": status,
                "last_source_label": "Your tracking" if history else "Not tracked yet",
                "last_observed_at": None,
                "has_pending_confirmation": False,
                "new_episode_ready": False,
                "removal_status": "",
                "publication_pending": False,
                "quality_fallback": False,
                "release_episode_total": None,
                "display_episode_count": total,
                "can_download": False,
                "rating": history.get("rating") if history else None,
            }
        )
    return cards


def search_cards(db, scope, *, query, domain):
    """Local selected catalog only. This never queries a provider or imports history."""
    page = collection(db, scope, domain=domain, query=query, limit=8)
    result = []
    for card in page["cards"]:
        identity, kind = card["canonical_id"], card["domain"]
        history = card["history"]
        url = (
            f"/plus/catalog/anime/{identity}"
            if kind == "anime"
            else f"/plus/movies/{identity}"
            if kind == "movie"
            else f"/plus/tv/{identity}"
        )
        result.append(
            {
                "id": int(identity) if kind != "movie" else identity,
                "media_type": kind,
                "title": card["title"],
                "year": card["metadata"].get("year"),
                "format": "TV series" if kind == "tv" else "Anime" if kind == "anime" else "MOVIE",
                "poster_url": card["artwork_url"],
                "url": url,
                "watch_status": history["status"] if history else "unknown",
                "progress": history.get("progress_episodes") if history else None,
                "episode_total": history.get("episode_total") if history else None,
                "rating": history.get("rating") if history else None,
                "library_id": card["id"],
                "library_status": "Shared library",
                "description": card["metadata"].get("overview", ""),
                "genres": [],
            }
        )
    return result
