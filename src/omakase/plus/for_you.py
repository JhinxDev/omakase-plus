"""Viewer-owned For You recommendations and explicit request intake."""

from __future__ import annotations

import asyncio
import re
from functools import lru_cache

import httpx
from starlette.concurrency import run_in_threadpool

from omakase.plus import discover_hub as hub
from omakase.plus import households, movies_store
from omakase.plus.catalog_search import AnimeCatalogSearch, CatalogSearchError, _plain
from omakase.plus.connections import movie_connections
from omakase.plus.debrid_anime import anime_episodes
from omakase.plus.debrid_library import account_token
from omakase.plus.direct import resolve_direct_request
from omakase.plus.household_media import personal_legacy_visible
from omakase.plus.media_store import upsert_monitor
from omakase.plus.media_sync import _monitor_withdrawn
from omakase.plus.movies import MovieError, MovieService
from omakase.plus.providers import playback as playback_providers
from omakase.plus.search_routes import _input, _SearchLimit, shared_catalog_search
from omakase.plus.secrets import read_secret
from omakase.plus.tv_service import TVError, TVService
from omakase.plus.web_playback_catalog import personal_scope
from omakase.plus.web_playback_navigation import _candidates
from omakase.plus.web_playback_source import PlaybackSourceError, qualified_publication

_CATEGORIES = {"anime": "anime", "tv": "tv", "movies": "movie"}
_SEARCH_HEADINGS = {"anime": "Anime", "tv": "TV Shows", "movies": "Movies"}
_LABELS = {
    "requested": "Requested",
    "preparing": "Preparing",
    "ready": "Ready to Play",
    "in_library": "In your library",
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
    def __init__(self, detail: str, status: int = 409, *, retry_after: int | None = None):
        super().__init__(detail)
        self.detail = detail
        self.status = status
        self.retry_after = retry_after


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


def _step(state, occurred_at, *, label=None):
    return {"key": state, "label": label or _LABELS[state], "occurred_at": occurred_at}


def _status(kind, title_id, title, state, detail, href, *, timeline):
    return {
        "kind": kind,
        "title_id": title_id,
        "title": title,
        "state": state,
        "label": _LABELS[state],
        "detail": detail,
        "href": href,
        "can_check": kind == "movie" and state not in {"ready", "in_library"},
        "timeline": timeline,
    }


_MOVIE_PROGRESS_LABELS = {
    "requested": "Riven confirmed the request",
    "searching": "Riven started looking for a copy",
    "search_complete": "Riven finished searching",
    "acquiring": "Riven reported a transfer",
    "verifying": "Checking library availability",
}


def _movie_status(db, user_id, title_id, library=None, movie_card=None, observations=()):
    row = db.execute(
        """SELECT c.title,r.code,r.created_at,r.updated_at FROM movie_requests r
           JOIN movie_catalog c USING(user_id,imdb_id)
           WHERE r.user_id=? AND r.imdb_id=? AND r.code!='copy_removed'""",
        (user_id, title_id),
    ).fetchone()
    if row is None:
        return None
    code = row["code"]
    card_matches = movie_card and movie_card.get("imdb_id") == title_id
    if card_matches and movie_card.get("plex_present"):
        library = ("", movie_card.get("plex_indexed_at"))
    if card_matches and movie_card.get("state") == "ready" and movie_card.get("verified_at"):
        ready = {"verified_at": movie_card["verified_at"]}
    else:
        fingerprint = library[0] if library else None
        ready = db.execute(
            """SELECT verified_at FROM movie_verifications
               WHERE user_id=? AND imdb_id=? AND proof_schema=1 AND state='ready' AND plex_key!=''
                 AND (? IS NULL OR fingerprint=?) AND julianday(expires_at)>julianday('now')
               ORDER BY verified_at DESC LIMIT 1""",
            (user_id, title_id, fingerprint, fingerprint),
        ).fetchone()
    timeline = [_step("requested", row["created_at"], label="Request saved")]
    if code == "acknowledgement_unconfirmed":
        timeline.append(
            _step("received", row["updated_at"], label="Riven acknowledged the request")
        )
    phases = {}
    if code in _MOVIE_PROGRESS_LABELS and code != "requested":
        phases[code] = row["updated_at"]
    for observed_code, observed_at in observations:
        if observed_code in _MOVIE_PROGRESS_LABELS:
            phases.setdefault(observed_code, observed_at)
            if observed_code != "requested" and (
                code in _MOVIE_PREPARING or code in _MOVIE_WAITING or code == "requested"
            ):
                code = observed_code
    for phase, at in sorted(phases.items(), key=lambda entry: entry[1]):
        if phase == "requested":
            if timeline[-1]["key"] != "received":
                timeline.append(_step("received", at, label=_MOVIE_PROGRESS_LABELS[phase]))
        else:
            timeline.append(_step(phase, at, label=_MOVIE_PROGRESS_LABELS[phase]))
    if ready:
        state, detail = "ready", "The matching copy passed its playback check."
        timeline.append(_step(state, ready["verified_at"]))
    elif library:
        state, detail = "in_library", "A matching copy is in your Movies library."
        timeline.append(_step(state, library[1]))
    elif code in _MOVIE_PREPARING:
        state, detail = "preparing", "Waiting for a matching copy to appear in your library."
    elif code == "unreleased":
        state, detail = "requested", "Waiting for the movie to be released."
    elif code == "acknowledgement_unconfirmed":
        state, detail = "requested", "Riven responded. Waiting for a matching copy."
    elif code == "requested" or code in _MOVIE_WAITING:
        state, detail = "requested", "Request saved. Waiting for a matching copy."
    else:
        state = "needs_attention"
        if code in {"no_eligible_results", "backend_failed", "investigation_exhausted"}:
            detail = "A matching copy could not be confirmed. Review the request."
        else:
            detail = "The latest status could not be confirmed. Check the request for an update."
        timeline.append(_step(state, row["updated_at"]))
    return _status(
        "movie",
        title_id,
        row["title"],
        state,
        detail,
        f"/plus/movies/{title_id}",
        timeline=timeline,
    )


async def _movie_library(db, scope, *, candidates=None):
    if candidates is None:
        imdb_ids = [
            row[0]
            for row in db.execute(
                """SELECT imdb_id FROM movie_requests WHERE user_id=? AND code!='copy_removed'
                   ORDER BY updated_at DESC,imdb_id LIMIT 30""",
                (scope.viewer_id,),
            )
        ]
    else:
        imdb_ids = [
            imdb_id
            for imdb_id in candidates
            if (request := movies_store.get_request(db, scope.viewer_id, imdb_id))
            and request["code"] != "copy_removed"
        ]
    if not imdb_ids:
        return {}
    try:
        async with asyncio.timeout(5):
            service = MovieService(db, movie_connections(db, scope.viewer_id))
            return await service.indexed_requests(scope.viewer_id, imdb_ids)
    except (MovieError, TimeoutError):
        return {}


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


def _request_status(
    db, scope, kind, title_id, *, publication=None, movie_library=None, movie_card=None
):
    if kind == "movie":
        if not personal_legacy_visible(db, scope, domain=kind, canonical_id=title_id):
            return None
        return _movie_status(
            db,
            scope.viewer_id,
            title_id,
            (movie_library or {}).get(title_id),
            movie_card,
        )
    row = db.execute(
        "SELECT title,created_at FROM for_you_requests WHERE user_id=? AND kind=? AND title_id=?",
        (scope.viewer_id, kind, title_id),
    ).fetchone()
    if row is None:
        return None
    timeline = [_step("requested", row["created_at"])]
    if kind == "tv":
        return _status(
            kind,
            title_id,
            row["title"],
            "requested",
            "Waiting for owner review. TV acquisition requires a manual step.",
            f"/plus/tv/{title_id}",
            timeline=timeline,
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
            timeline=timeline + [_step("needs_attention", None)],
        )
    try:
        _require_visible(db, scope, kind, title_id, season=monitor["season_number"] or 0)
    except ForYouError:
        return None
    state, detail = _anime_status(db, scope, title_id, monitor, publication=publication)
    if state != "requested":
        timeline.append(_step(state, monitor["last_checked_at"] if state != "ready" else None))
    return _status(
        kind,
        title_id,
        row["title"],
        state,
        detail,
        f"/plus/library/{monitor['id']}",
        timeline=timeline,
    )


def request_status(db, user_id, kind, title_id, *, movie_card=None):
    title_id = _identity(kind, title_id)
    return _request_status(db, _scope(db, user_id), kind, title_id, movie_card=movie_card)


def _active_request(status):
    return status["kind"] != "movie" or status["state"] not in {"ready", "in_library"}


def _requests(db, scope, kind, *, publication=None, movie_library=None):
    if kind == "movie":
        rows = db.execute(
            """SELECT imdb_id FROM movie_requests WHERE user_id=? AND code!='copy_removed'
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
        if (
            status := _request_status(
                db, scope, kind, str(row[0]), publication=publication, movie_library=movie_library
            )
        )
    ]


def _publication_reader(db):
    @lru_cache(maxsize=1)
    def publication():
        try:
            return qualified_publication(db)
        except (ValueError, KeyError, TypeError, RecursionError):
            return None

    return publication


def _project_shelves(db, scope, rows, *, publication, requests=(), movie_library=None):
    known = {(status["kind"], status["title_id"]): status for status in requests}
    shelves = []
    for row in rows:
        cards = []
        kind = _CATEGORIES.get(row["key"], "anime")
        for source in row["cards"]:
            if "id" not in source:
                cards.append(source)
                continue
            try:
                title_id = _identity(kind, source["id"])
            except ForYouError:
                continue
            status = known.get((kind, title_id)) or _request_status(
                db, scope, kind, title_id, publication=publication, movie_library=movie_library
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
                    and not source.get("available")
                    and source.get("requestable", True),
                }
            )
        shelves.append({**row, "cards": cards})
    return shelves


async def recommendations(db, user_id, category):
    if category != "screen" and category not in _CATEGORIES:
        raise ForYouError("Choose a For You category.", 422)
    scope = _scope(db, user_id)
    history_unavailable = False
    movie_library = None
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
        if category in {"movies", "screen"}:
            rows, movie_library = await asyncio.gather(
                hub.screen_shelves(db, user_id), _movie_library(db, scope)
            )
        else:
            rows = await hub.screen_shelves(db, user_id)
        if category != "screen":
            rows = [row for row in rows if row["key"] == category]

    publication = _publication_reader(db)

    kinds = ("movie", "tv") if category == "screen" else (_CATEGORIES[category],)
    requests = [
        status
        for kind in kinds
        for status in _requests(
            db, scope, kind, publication=publication, movie_library=movie_library
        )
    ]
    return {
        "category": category,
        "rows": _project_shelves(
            db, scope, rows, publication=publication, requests=requests, movie_library=movie_library
        ),
        "requests": [status for status in requests if _active_request(status)],
        "history_unavailable": history_unavailable,
    }


async def _search_cards(db, scope, query, category):
    user_id = scope.viewer_id
    if category == "anime":
        records = await AnimeCatalogSearch(db, user_id).search(query)
        return [
            {
                "id": record["id"],
                "title": record["title"],
                "poster_url": record.get("catalog_poster_url"),
                "summary": record.get("description"),
                "owned": bool(record.get("library_id")),
            }
            for record in records[:8]
        ]

    connections = movie_connections(db, user_id)
    if category == "movies":
        records = await MovieService(db, connections).search(user_id, query)
        return [
            {
                "id": record["imdb_id"],
                "title": record["title"],
                "poster_url": record.get("poster_url"),
                "summary": _plain(record.get("overview")),
            }
            for record in records[:8]
        ]

    service = TVService(db, connections)
    records = await service.search(user_id, query)
    return [
        {
            "id": record["tmdb_id"],
            "title": record["title"],
            "poster_url": (service._raw(user_id, record["tmdb_id"]) or {}).get("poster_url"),
            "summary": record.get("overview"),
            "available": bool(record.get("in_plex_episodes")),
        }
        for record in records[:8]
    ]


async def search(db, user_id, query, category="all"):
    """Search the same personal catalog as the web with one shared search budget."""
    try:
        query = _input(query, category)
    except CatalogSearchError as error:
        raise ForYouError(error.detail, error.status_code) from None
    scope = _scope(db, user_id)
    if len(query) < 2:
        return {
            "category": category,
            "rows": [],
            "requests": [],
            "history_unavailable": False,
            "warnings": [],
        }

    async def cards_for(kind):
        return await _search_cards(db, scope, query, kind)

    try:
        categories, outcomes = await shared_catalog_search.run(user_id, category, cards_for)
    except _SearchLimit as error:
        raise ForYouError(error.detail, error.status_code, retry_after=error.retry_after) from None

    rows = []
    warnings = []
    for key, (records, warning) in zip(categories, outcomes, strict=True):
        if warning:
            warnings.append(warning)
        kind = _CATEGORIES[key]
        cards = {}
        for record in records:
            try:
                title_id = _identity(kind, record["id"])
            except ForYouError:
                continue
            if kind != "anime":
                try:
                    _require_visible(db, scope, kind, title_id)
                except ForYouError:
                    continue
            cards.setdefault(title_id, record)
        if cards:
            ranked = sorted(
                cards.values(), key=lambda card: card["title"].casefold() != query.casefold()
            )
            rows.append(hub._shelf(key, _SEARCH_HEADINGS[key], ranked))
    movie_candidates = {
        str(card["id"]) for row in rows if row["key"] == "movies" for card in row["cards"]
    }
    movie_library = (
        await _movie_library(db, scope, candidates=movie_candidates) if movie_candidates else {}
    )
    rows = _project_shelves(
        db, scope, rows, publication=_publication_reader(db), movie_library=movie_library
    )
    return {
        "category": category,
        "rows": rows,
        "requests": [
            card["request"]
            for row in rows
            for card in row["cards"]
            if card["request"] and _active_request(card["request"])
        ],
        "history_unavailable": False,
        "warnings": warnings,
    }


async def _submit_movie(db, scope, title_id):
    _require_visible(db, scope, "movie", title_id)
    movie = movies_store.get_movie(db, scope.viewer_id, title_id)
    if movie is None or not movie["identity_verified"]:
        raise ForYouError(
            "Find this movie in Search or Movies for You to verify its identity.", 404
        )
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
