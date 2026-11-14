"""Viewer-owned For You recommendations and explicit request intake."""

from __future__ import annotations

import asyncio
import re
from functools import lru_cache

import httpx
from starlette.concurrency import run_in_threadpool

from omakase.plus import discover_hub as hub
from omakase.plus import households, movies_store
from omakase.plus.connections import movie_connections
from omakase.plus.debrid_anime import anime_episodes
from omakase.plus.debrid_library import account_token
from omakase.plus.direct import resolve_direct_request
from omakase.plus.household_media import personal_legacy_visible
from omakase.plus.media_store import upsert_monitor
from omakase.plus.media_sync import _monitor_withdrawn
from omakase.plus.movies import MovieError, MovieService
from omakase.plus.providers import playback as playback_providers
from omakase.plus.secrets import read_secret
from omakase.plus.tv_service import TVError, TVService
from omakase.plus.web_playback_catalog import personal_scope
from omakase.plus.web_playback_navigation import _candidates
from omakase.plus.web_playback_source import PlaybackSourceError, qualified_publication

_CATEGORIES = {"anime": "anime", "tv": "tv", "movies": "movie"}
_LABELS = {
    "requested": "Requested",
    "preparing": "Preparing",
    "ready": "Ready to Play",
    "needs_attention": "Needs Attention",
}
_POSTER = re.compile(
    r"https://(?:image\.tmdb\.org/t/p/(?:w[0-9]{2,4}|original)/[A-Za-z0-9_-]{1,96}"
    r"|s4\.anilist\.co/file/anilistcdn/media/anime/cover/(?:medium|large)/[A-Za-z0-9_-]{1,128})"
    r"\.(?:jpg|jpeg|png|webp)"
)
_MOVIE_WAITING = {"submission_pending", "acknowledgement_unconfirmed", "unreleased"}
_MOVIE_PREPARING = {
    "searching",
    "search_complete",
    "acquiring",
    "verifying",
    "plex_not_found",
    "verification_expired",
    "file_changed",
}


class ForYouError(ValueError):
    def __init__(self, detail: str, status: int = 409):
        super().__init__(detail)
        self.detail = detail
        self.status = status


def _identity(kind, title_id):
    if type(title_id) is int:
        title_id = str(title_id)
    if isinstance(title_id, str):
        if kind == "movie" and re.fullmatch(r"tt[0-9]{7,10}", title_id):
            return title_id
        if (
            kind in {"anime", "tv"}
            and re.fullmatch(r"[1-9][0-9]{0,9}", title_id)
            and int(title_id) <= 2**31 - 1
        ):
            return title_id
    raise ForYouError("Choose a valid catalog title.", 422)


def _require_visible(db, scope, kind, title_id, *, season=0):
    removed = db.execute(
        """SELECT 1 FROM library_items WHERE library_id=? AND domain=?
           AND canonical_id=? AND season_number=? AND state='removed'""",
        (scope.library_id, kind, title_id, season),
    ).fetchone()
    if removed or not personal_legacy_visible(
        db, scope, domain=kind, canonical_id=title_id, season=season
    ):
        raise ForYouError("This title is unavailable in your personal library.")


def _scope(db, user_id):
    try:
        return personal_scope(db, user_id)
    except (households.HouseholdError, PlaybackSourceError):
        raise ForYouError("Your personal library is unavailable.", 404) from None


def _status(kind, title_id, title, state, detail, href):
    return {
        "kind": kind,
        "title_id": title_id,
        "title": title,
        "state": state,
        "label": _LABELS[state],
        "detail": detail,
        "href": href,
        "can_check": kind == "movie" and state != "ready",
    }


def _movie_status(db, user_id, title_id):
    row = db.execute(
        """SELECT c.title,r.code,r.detail FROM movie_requests r
           JOIN movie_catalog c USING(user_id,imdb_id)
           WHERE r.user_id=? AND r.imdb_id=?""",
        (user_id, title_id),
    ).fetchone()
    if row is None:
        return None
    code, detail = row["code"], row["detail"]
    ready = (
        code != "copy_removed"
        and db.execute(
            """SELECT 1 FROM movie_verifications
           WHERE user_id=? AND imdb_id=? AND proof_schema=1 AND state='ready' AND plex_key!=''
             AND julianday(expires_at)>julianday('now') LIMIT 1""",
            (user_id, title_id),
        ).fetchone()
    )
    if ready:
        state, detail = "ready", "The exact movie copy passed its latest Plex playback check."
    elif code in _MOVIE_PREPARING:
        state = "preparing"
    elif code == "requested" or code in _MOVIE_WAITING:
        state = "requested"
    else:
        state = "needs_attention"
    result = _status("movie", title_id, row["title"], state, detail, f"/plus/movies/{title_id}")
    if code == "copy_removed":
        result["can_check"] = False
    return result


def _anime_status(db, scope, title_id, monitor, *, publication=None):
    if _monitor_withdrawn(db, monitor["id"]):
        return "needs_attention", "Acquisition stopped for this title."
    selection = playback_providers.select(db, scope.viewer_id)
    first = 0 if monitor["format"] == "MOVIE" else 1
    if selection.cloud_route and account_token(db, scope.viewer_id):
        if first in anime_episodes(db, scope, monitor["id"]):
            return "ready", "The first playable file has a verified Real-Debrid match."
    elif selection.publication_route and first in _candidates(db, scope, monitor, publication):
        return "ready", "The first playable file is in your verified Plex publication."
    if not monitor["active"]:
        return "needs_attention", "No currently playable opening was verified."
    if not read_secret(db, scope.viewer_id, "realdebrid_api_key"):
        return "needs_attention", "Connect Real-Debrid in Settings to prepare this request."
    if not monitor["last_checked_at"]:
        return "requested", "Queued for the next scheduled anime media check."
    last = db.execute(
        """SELECT outcome,accepted_count,covered_count FROM media_sync_runs
           WHERE monitor_id=? ORDER BY id DESC LIMIT 1""",
        (monitor["id"],),
    ).fetchone()
    if monitor["last_result"] == "SYNC_FAILED":
        return "needs_attention", "The last anime media check failed."
    if last and (last["accepted_count"] or last["covered_count"] or last["outcome"] == "running"):
        return (
            "preparing",
            "The anime pipeline found matching media. Playback is still being checked.",
        )
    if last and last["outcome"] == "degraded":
        return (
            "needs_attention",
            "The most recent anime media check found no confirmed playable opening.",
        )
    return "requested", "Waiting for a playable release or the next media check."


def _request_status(db, scope, kind, title_id, *, publication=None):
    if kind == "movie":
        if not personal_legacy_visible(db, scope, domain=kind, canonical_id=title_id):
            return None
        return _movie_status(db, scope.viewer_id, title_id)
    row = db.execute(
        "SELECT title FROM for_you_requests WHERE user_id=? AND kind=? AND title_id=?",
        (scope.viewer_id, kind, title_id),
    ).fetchone()
    if row is None:
        return None
    if kind == "tv":
        return _status(
            kind,
            title_id,
            row["title"],
            "requested",
            "Waiting for owner review. TV acquisition requires a manual step.",
            f"/plus/tv/{title_id}",
        )
    monitor = db.execute(
        "SELECT * FROM media_monitors WHERE user_id=? AND anilist_id=?",
        (scope.viewer_id, title_id),
    ).fetchone()
    if monitor is None:
        return _status(
            kind,
            title_id,
            row["title"],
            "needs_attention",
            "The anime monitor is unavailable.",
            f"/plus/catalog/anime/{title_id}",
        )
    try:
        _require_visible(db, scope, kind, title_id, season=monitor["season_number"] or 0)
    except ForYouError:
        return None
    state, detail = _anime_status(db, scope, title_id, monitor, publication=publication)
    return _status(kind, title_id, row["title"], state, detail, f"/plus/library/{monitor['id']}")


def request_status(db, user_id, kind, title_id):
    title_id = _identity(kind, title_id)
    return _request_status(db, _scope(db, user_id), kind, title_id)


def _requests(db, scope, kind, *, publication=None):
    if kind == "movie":
        rows = db.execute(
            """SELECT imdb_id FROM movie_requests WHERE user_id=?
               ORDER BY updated_at DESC,imdb_id LIMIT 30""",
            (scope.viewer_id,),
        )
    else:
        rows = db.execute(
            """SELECT title_id FROM for_you_requests WHERE user_id=? AND kind=?
               ORDER BY created_at DESC,title_id DESC LIMIT 30""",
            (scope.viewer_id, kind),
        )
    return [
        status
        for row in rows
        if (status := _request_status(db, scope, kind, str(row[0]), publication=publication))
    ]


async def recommendations(db, user_id, category):
    if category != "screen" and category not in _CATEGORIES:
        raise ForYouError("Choose a For You category.", 422)
    scope = _scope(db, user_id)
    history_unavailable = False
    if category == "anime":
        catalog, watched = await asyncio.gather(
            hub.anime_catalog(), hub.tracker_watched_ids(db, user_id)
        )
        history_unavailable = watched is None
        rows = (
            []
            if history_unavailable
            else hub.anime_shelves(db, user_id, catalog, hub.viewer_monitors(db, user_id), watched)
        )
    else:
        rows = await hub.screen_shelves(db, user_id)
        if category != "screen":
            rows = [row for row in rows if row["key"] == category]

    @lru_cache(maxsize=1)
    def publication():
        try:
            return qualified_publication(db)
        except (ValueError, KeyError, TypeError, RecursionError):
            return None

    kinds = ("movie", "tv") if category == "screen" else (_CATEGORIES[category],)
    requests = [
        status for kind in kinds for status in _requests(db, scope, kind, publication=publication)
    ]
    known = {(status["kind"], status["title_id"]): status for status in requests}
    shelves = []
    for row in rows:
        cards = []
        kind = _CATEGORIES.get(row["key"], "anime")
        for source in row["cards"]:
            if "id" not in source:
                cards.append(source)
                continue
            title_id = str(source["id"])
            status = known.get((kind, title_id)) or _request_status(
                db, scope, kind, title_id, publication=publication
            )
            poster = source.get("poster_url")
            cards.append(
                {
                    **source,
                    "id": title_id,
                    "kind": kind,
                    "poster_url": poster
                    if isinstance(poster, str) and _POSTER.fullmatch(poster)
                    else None,
                    "summary": (source.get("summary") or "")[:650],
                    "request": status,
                    "can_request": not status
                    and not source.get("owned")
                    and not source.get("available"),
                }
            )
        shelves.append({**row, "cards": cards})
    return {
        "category": category,
        "rows": shelves,
        "requests": requests,
        "history_unavailable": history_unavailable,
    }


async def _submit_movie(db, scope, title_id):
    _require_visible(db, scope, "movie", title_id)
    movie = movies_store.get_movie(db, scope.viewer_id, title_id)
    if movie is None or not movie["identity_verified"]:
        raise ForYouError("Open this title in Movies for You to verify its identity.", 404)
    service = MovieService(db, movie_connections(db, scope.viewer_id))
    await service.request(
        scope.viewer_id,
        imdb_id=title_id,
        tmdb_id=movie["tmdb_id"],
        _authorize_submission=lambda: _require_visible(db, scope, "movie", title_id),
    )


async def _resolve_anime(title_id):
    try:
        async with asyncio.timeout(8):
            target = await run_in_threadpool(resolve_direct_request, title_id, season="")
    except (ValueError, KeyError, TypeError, httpx.HTTPError, TimeoutError):
        raise ForYouError("AniList could not verify this title. Try again shortly.", 503) from None
    if str(target.anilist_id) != title_id or not target.title.strip():
        raise ForYouError("AniList returned a different title identity.")
    return target


def _enroll_anime(db, user_id, target):
    planning = db.execute(
        "SELECT id FROM anilist_plannings WHERE user_id=? AND anilist_id=? ORDER BY id LIMIT 1",
        (user_id, target.anilist_id),
    ).fetchone()
    planning_id = (
        planning["id"]
        if planning
        else db.execute(
            "INSERT INTO anilist_plannings (user_id,anilist_id,title,status) VALUES(?,?,?,'PLANNING')",
            (user_id, target.anilist_id, target.title),
        ).lastrowid
    )
    upsert_monitor(
        db,
        user_id=user_id,
        anilist_id=target.anilist_id,
        title=target.title,
        search_titles=target.search_titles,
        genres=target.genres,
        anilist_planning_id=planning_id,
        format=target.format,
        release_status=target.status,
        season_number=target.season_number,
        expected_episodes=target.episodes,
        aired_episodes=target.aired_episodes,
        next_airing_at=target.next_airing_at,
        active=True,
    )


async def _submit_local(db, scope, kind, title_id):
    user_id = scope.viewer_id
    if kind == "anime":
        target = await _resolve_anime(title_id)
        title, season = target.title, target.season_number or 0
    else:
        service = TVService(db, movie_connections(db, user_id))
        metadata = await service._metadata_tv(int(title_id))
        title, season = metadata["title"], 0
    with households._transaction(db):
        _require_visible(db, scope, kind, title_id, season=season)
        if kind == "anime":
            monitor = db.execute(
                "SELECT id FROM media_monitors WHERE user_id=? AND anilist_id=?",
                (user_id, title_id),
            ).fetchone()
            if monitor and _monitor_withdrawn(db, monitor["id"]):
                raise ForYouError("This anime was previously withdrawn from acquisition.")
        inserted = db.execute(
            """INSERT INTO for_you_requests (user_id,kind,title_id,title) VALUES (?,?,?,?)
               ON CONFLICT(user_id,kind,title_id) DO NOTHING""",
            (user_id, kind, title_id, title.strip()[:300]),
        )
        if inserted.rowcount and kind == "anime":
            try:
                _enroll_anime(db, user_id, target)
            except ValueError:
                raise ForYouError("AniList returned unsupported title metadata.", 422) from None


async def submit_request(db, user_id, kind, title_id):
    title_id = _identity(kind, title_id)
    scope = _scope(db, user_id)
    previous = _request_status(db, scope, kind, title_id)
    if previous:
        return previous
    try:
        if kind == "movie":
            await _submit_movie(db, scope, title_id)
        else:
            await _submit_local(db, scope, kind, title_id)
    except households.HouseholdError as error:
        raise ForYouError(str(error), error.status) from None
    except (MovieError, TVError) as error:
        raise ForYouError(error.detail) from None
    return _request_status(db, scope, kind, title_id)


async def refresh_request(db, user_id, kind, title_id):
    title_id = _identity(kind, title_id)
    scope = _scope(db, user_id)
    status = _request_status(db, scope, kind, title_id)
    if status is None:
        raise ForYouError("Request not found.", 404)
    if kind == "movie" and status["can_check"]:
        _require_visible(db, scope, kind, title_id)
        service = MovieService(db, movie_connections(db, user_id))
        try:
            card = await service.refresh(user_id, title_id)
            if card["code"] == "verifying":
                await service.verify(user_id, title_id)
        except MovieError as error:
            raise ForYouError(error.detail) from None
        return _request_status(db, scope, kind, title_id)
    return status
