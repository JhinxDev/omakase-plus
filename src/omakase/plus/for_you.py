"""Viewer-owned For You recommendations and explicit request intake."""

from __future__ import annotations

import asyncio
import re
from functools import lru_cache

import httpx
from starlette.concurrency import run_in_threadpool

from omakase.plus import discover_hub as hub
from omakase.plus import (
    households,
    movie_fallback,
    movies_store,
    request_candidates,
    tv_provider_requests,
)
from omakase.plus import request_status as status_model
from omakase.plus.catalog_search import AnimeCatalogSearch, CatalogSearchError, _plain
from omakase.plus.connections import movie_connections
from omakase.plus.debrid_anime import anime_episodes
from omakase.plus.debrid_catalog import movie_ids as debrid_movie_ids
from omakase.plus.debrid_library import (
    DebridLibrary,
    DebridLibraryError,
    account_token,
    catalog_library,
)
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
_LABELS = status_model.LABELS
_MOVIE_PROGRESS_LABELS = status_model.MOVIE_PROGRESS_LABELS
_CLOUD_LIBRARY = DebridLibrary()
_FALLBACK_AFTER_RIVEN = frozenset(
    {
        "backend_failed",
        "backend_item_missing",
        "backend_unavailable",
        "investigation_exhausted",
        "no_eligible_results",
        "search_complete",
        "transport_error",
        "transport_timeout",
    }
)
_POSTER = re.compile(
    r"https://(?:image\.tmdb\.org/t/p/(?:w[0-9]{2,4}|original)/[A-Za-z0-9_-]{1,96}"
    r"|s4\.anilist\.co/file/anilistcdn/media/anime/cover/(?:medium|large)/[A-Za-z0-9_-]{1,128})"
    r"\.(?:jpg|jpeg|png|webp)"
)


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


def _movie_status(
    db, user_id, title_id, library=None, movie_card=None, observations=(), *, include_removed=False
):
    """Project one movie request through the shared status model."""
    removed_filter = "" if include_removed else "AND r.code!='copy_removed'"
    row = db.execute(
        f"""SELECT c.title,c.year,c.poster_url,r.imdb_id,r.code,r.detail,r.created_at,r.updated_at,
                   r.backend_id IS NOT NULL AS provider_record_confirmed
            FROM movie_requests r JOIN movie_catalog c USING(user_id,imdb_id)
            WHERE r.user_id=? AND r.imdb_id=? {removed_filter}""",
        (user_id, title_id),
    ).fetchone()
    if row is None:
        return None
    card_matches = movie_card and movie_card.get("imdb_id") == title_id
    if card_matches and "cloud_match" in movie_card:
        library = ("", None) if movie_card["cloud_match"] else None
        ready = None
    else:
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
    milestones = list(observations or ())
    known = {entry[0] for entry in milestones if entry[0] != "candidate_rejected"}
    lifecycle = row["created_at"]
    for observed in status_model.observations(user_id, "movie", str(title_id), lifecycle=lifecycle):
        # Repeated verified candidate rejections are distinct events.
        if observed[0] == "candidate_rejected" or observed[0] not in known:
            milestones.append(observed)
            known.add(observed[0])
    model = status_model.build_movie(
        row,
        title=row["title"],
        library_at=library[1] if library else None,
        in_library=bool(library),
        ready_at=ready["verified_at"] if ready else None,
        observations_list=milestones,
        provider_record_confirmed=bool(row["provider_record_confirmed"]),
    )
    model["release_year"] = row["year"]
    if row["poster_url"]:
        model["poster_href"] = f"/plus/movies/{title_id}/artwork?variant=poster"
    return model


async def _movie_library(
    db, scope, *, candidates=None
) -> dict[str, tuple[str, str | None]] | set[str]:
    """Return Plex index evidence or the selected Real-Debrid inventory matches."""
    cloud_playback = playback_providers.select(db, scope.viewer_id).cloud_route
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
        return set() if cloud_playback else {}
    try:
        async with asyncio.timeout(5):
            if cloud_playback:
                return (await debrid_movie_ids(db, scope, catalog_library)).intersection(imdb_ids)
            service = MovieService(db, movie_connections(db, scope.viewer_id))
            return await service.indexed_requests(scope.viewer_id, imdb_ids)
    except (MovieError, DebridLibraryError, PlaybackSourceError, TimeoutError):
        return set() if cloud_playback else {}


def _anime_playable(db, scope, monitor, publication):
    selection = playback_providers.select(db, scope.viewer_id)
    first = 0 if monitor["format"] == "MOVIE" else 1
    if selection.cloud_route and account_token(db, scope.viewer_id):
        return first in anime_episodes(db, scope, monitor["id"])
    if selection.publication_route:
        return first in _candidates(db, scope, monitor, publication)
    return False


def _anime_model(db, scope, row, monitor, *, publication=None):
    if monitor is not None:
        try:
            _require_visible(
                db, scope, "anime", str(monitor["anilist_id"]), season=monitor["season_number"] or 0
            )
        except ForYouError:
            return None
    last_run = (
        db.execute(
            """SELECT outcome,accepted_count,covered_count FROM media_sync_runs
               WHERE monitor_id=? ORDER BY id DESC LIMIT 1""",
            (monitor["id"],),
        ).fetchone()
        if monitor is not None
        else None
    )
    model = status_model.build_anime(
        anilist_id=row["title_id"],
        title=row["title"],
        created_at=row["created_at"],
        monitor=monitor,
        last_run=last_run,
        playable=bool(monitor is not None and _anime_playable(db, scope, monitor, publication)),
        withdrawn=bool(monitor is not None and _monitor_withdrawn(db, monitor["id"])),
        provider_connected=bool(read_secret(db, scope.viewer_id, "realdebrid_api_key")),
    )
    if monitor is not None:
        model["poster_href"] = f"/plus/media/{monitor['anilist_id']}/poster"
    return model


def _tv_model(db, scope, row):
    progress = tv_provider_requests.read(db, scope.viewer_id, row["title_id"])
    if progress and progress["snapshot"]:
        service = TVService(db, movie_connections(db, scope.viewer_id))
        progress = tv_provider_requests.project_library(db, scope.viewer_id, service, progress)
    snapshot = db.execute(
        """SELECT complete,observed_at FROM tv_plex_snapshots
           WHERE user_id=? AND tmdb_id=? AND complete=1""",
        (scope.viewer_id, int(row["title_id"])),
    ).fetchone()
    library_at = snapshot["observed_at"] if snapshot is not None else None
    if library_at is None:
        episode = db.execute(
            """SELECT max(updated_at) FROM tv_episodes
               WHERE user_id=? AND tmdb_id=? AND plex_present=1""",
            (scope.viewer_id, int(row["title_id"])),
        ).fetchone()
        library_at = episode[0] if episode is not None else None
    model = status_model.build_tv(
        tmdb_id=row["title_id"],
        title=row["title"],
        created_at=row["created_at"],
        library_at=library_at,
        library_source="plex_snapshot" if snapshot is not None else "plex_episode",
        provider=progress,
        can_check=tv_provider_requests.enabled(),
    )
    model["poster_href"] = f"/plus/tv/{row['title_id']}/artwork?variant=poster"
    return model


def _request_status(
    db,
    scope,
    kind,
    title_id,
    *,
    publication=None,
    movie_library=None,
    movie_card=None,
    include_removed=False,
):
    if kind == "movie":
        if not personal_legacy_visible(db, scope, domain=kind, canonical_id=title_id):
            return None
        if isinstance(movie_library, set):
            movie_card = {"imdb_id": title_id, "cloud_match": title_id in movie_library}
        elif movie_library is None and movie_card is None:
            if playback_providers.select(db, scope.viewer_id).cloud_route:
                movie_card = {"imdb_id": title_id, "cloud_match": False}
        return _movie_status(
            db,
            scope.viewer_id,
            title_id,
            movie_library.get(title_id) if isinstance(movie_library, dict) else None,
            movie_card,
            include_removed=include_removed,
        )
    row = db.execute(
        """SELECT title_id,title,created_at FROM for_you_requests
           WHERE user_id=? AND kind=? AND title_id=?""",
        (scope.viewer_id, kind, title_id),
    ).fetchone()
    if row is None:
        return None
    if kind == "tv":
        return _tv_model(db, scope, row)
    monitor = db.execute(
        "SELECT * FROM media_monitors WHERE user_id=? AND anilist_id=?",
        (scope.viewer_id, title_id),
    ).fetchone()
    return _anime_model(db, scope, row, monitor, publication=publication)


def request_status(db, user_id, kind, title_id, *, movie_card=None):
    title_id = _identity(kind, title_id)
    return _request_status(db, _scope(db, user_id), kind, title_id, movie_card=movie_card)


def _active_request(status):
    return status["category"] != "history"


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


async def dashboard(db, user_id):
    """Read-only owner-scoped dashboard model for the web Requests screen."""
    scope = _scope(db, user_id)
    movie_ids = [
        row[0]
        for row in db.execute(
            """SELECT imdb_id FROM movie_requests WHERE user_id=?
               ORDER BY updated_at DESC,imdb_id LIMIT 30""",
            (scope.viewer_id,),
        )
    ]
    movie_library = await _movie_library(db, scope) if movie_ids else {}
    models = []
    for imdb_id in movie_ids:
        model = _request_status(
            db,
            scope,
            "movie",
            str(imdb_id),
            movie_library=movie_library,
            include_removed=True,
        )
        if model is not None:
            models.append(model)
    publication = _publication_reader(db)
    for kind in ("anime", "tv"):
        rows = db.execute(
            """SELECT title_id FROM for_you_requests WHERE user_id=? AND kind=?
               ORDER BY created_at DESC,title_id DESC LIMIT 60""",
            (scope.viewer_id, kind),
        )
        for row in rows:
            model = _request_status(db, scope, kind, str(row[0]), publication=publication)
            if model is not None:
                models.append(model)
    request_candidates.attach_search_summaries(db, user_id, models)
    movie_fallback.attach_summaries(db, user_id, models)
    return status_model.group(models)


async def request_detail(db, user_id, kind, title_id):
    """Read-only detail model for one owner-scoped request."""
    title_id = _identity(kind, title_id)
    scope = _scope(db, user_id)
    if kind == "movie":
        library = await _movie_library(db, scope, candidates=[title_id])
        model = _request_status(
            db, scope, "movie", title_id, movie_library=library, include_removed=True
        )
    else:
        model = _request_status(db, scope, kind, title_id, publication=_publication_reader(db))
    if model is None:
        raise ForYouError("Request not found.", 404)
    movie_fallback.attach_summaries(db, user_id, [model])
    return model


def _record_movie_observation(user_id, title_id, row, code, occurred_at):
    """Record one verified provider milestone for the request lifecycle."""
    if code not in status_model.OBSERVATION_CODES or occurred_at is None:
        return
    if row is None:
        return
    try:
        status_model.record_observation(
            user_id,
            "movie",
            str(title_id),
            lifecycle=row["created_at"],
            code=code,
            occurred_at=occurred_at,
        )
    except status_model.RequestStatusError:
        return


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
                hub.screen_shelves(db, user_id, kind=category), _movie_library(db, scope)
            )
        else:
            rows = await hub.screen_shelves(db, user_id, kind=category)
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
    request_candidates.attach_search_summaries(db, user_id, requests)
    movie_fallback.attach_summaries(db, user_id, requests)
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
        if inserted.rowcount and kind == "tv" and tv_provider_requests.enabled():
            service._save(user_id, metadata, commit=False)
        if inserted.rowcount and kind == "anime":
            try:
                _enroll_anime(db, user_id, target)
            except ValueError:
                raise ForYouError("AniList returned unsupported title metadata.", 422) from None


async def submit_request(db, user_id, kind, title_id):
    """Explicit intake. One open request per owner-scoped identity; a repeat returns it."""
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
        if kind == "tv" and tv_provider_requests.enabled():
            service = TVService(db, movie_connections(db, user_id))
            try:
                await tv_provider_requests.refresh(
                    db,
                    user_id,
                    int(title_id),
                    service,
                    submit=True,
                    cloud_library=_CLOUD_LIBRARY,
                )
            except MovieError:
                # The local request was committed, so review or send it from Requests.
                pass
    except households.HouseholdError as error:
        raise ForYouError(str(error), error.status) from None
    except (MovieError, TVError) as error:
        raise ForYouError(error.detail) from None
    return _request_status(db, scope, kind, title_id)


async def _fallback_movie_search(db, user_id, title_id, status, provider_code, *, cloud):
    if status is None:
        return
    if not status["resolved"] and provider_code in _FALLBACK_AFTER_RIVEN:
        on_matches = None
        if (
            cloud
            and provider_code in movie_fallback.TERMINAL_RIVEN_CODES
            and request_candidates.acquire_enabled()
            and await movie_fallback.advance(db, user_id, title_id) is None
        ):

            async def on_matches(eligible):
                await movie_fallback.advance(db, user_id, title_id, eligible)

        await request_candidates.refresh(
            db,
            user_id,
            "movie",
            title_id,
            request_candidates.JudgeRequest(title=status["title"], year=status.get("release_year")),
            on_matches=on_matches,
        )
    request_candidates.attach_search_summaries(db, user_id, [status])
    movie_fallback.attach_summaries(db, user_id, [status])


async def refresh_request(db, user_id, kind, title_id):
    """Check provider and library state, then record fallback search evidence when needed."""
    title_id = _identity(kind, title_id)
    scope = _scope(db, user_id)
    cloud = kind == "movie" and playback_providers.select(db, user_id).cloud_route
    library = await _movie_library(db, scope, candidates=[title_id]) if cloud else None
    status = _request_status(db, scope, kind, title_id, movie_library=library)
    if status is None:
        raise ForYouError("Request not found.", 404)
    if kind == "tv" and status["can_check"]:
        _require_visible(db, scope, kind, title_id)
        service = TVService(db, movie_connections(db, user_id))
        try:
            await tv_provider_requests.refresh(
                db, user_id, int(title_id), service, cloud_library=_CLOUD_LIBRARY
            )
            provider = tv_provider_requests.read(db, user_id, int(title_id))
            if provider:
                await tv_provider_requests.search_missing_episode(
                    db,
                    user_id,
                    int(title_id),
                    service._raw(user_id, int(title_id)),
                    provider["snapshot"],
                )
        except (MovieError, TVError) as error:
            raise ForYouError("Riven could not be checked. Try again shortly.", 503) from error
        return _request_status(db, scope, kind, title_id)
    if kind == "movie" and status["can_check"]:
        _require_visible(db, scope, kind, title_id)
        row = movies_store.get_request(db, scope.viewer_id, title_id)
        service = MovieService(db, movie_connections(db, user_id))
        try:
            card = await service.refresh(user_id, title_id)
            if card["code"] == "verifying" and not cloud:
                await service.verify(user_id, title_id)
        except MovieError as error:
            raise ForYouError(error.detail) from None
        check = card.get("request_check") or {}
        if check.get("performed") and check.get("attempted_at"):
            _record_movie_observation(
                user_id, title_id, row, card.get("code"), check.get("attempted_at")
            )
        status = _request_status(db, scope, kind, title_id, movie_library=library)
        await _fallback_movie_search(db, user_id, title_id, status, card.get("code"), cloud=cloud)
        return status
    return status


async def retry_request(db, user_id, kind, title_id):
    """Explicit retry that cannot create a second request for one identity.

    The request row is unique per owner-scoped identity and a retry only rechecks
    the existing request (``MovieService.refresh`` reads provider state and never
    submits). A repeated retry therefore returns the same existing request.
    """
    title_id = _identity(kind, title_id)
    scope = _scope(db, user_id)
    cloud = kind == "movie" and playback_providers.select(db, user_id).cloud_route
    library = await _movie_library(db, scope, candidates=[title_id]) if cloud else None
    status = _request_status(db, scope, kind, title_id, movie_library=library)
    if status is None:
        raise ForYouError("Request not found.", 404)
    if not status["can_retry"]:
        return status
    if kind == "tv":
        _require_visible(db, scope, kind, title_id)
        service = TVService(db, movie_connections(db, user_id))
        try:
            await tv_provider_requests.refresh(
                db, user_id, int(title_id), service, submit=True, cloud_library=_CLOUD_LIBRARY
            )
        except (MovieError, TVError):
            raise ForYouError("Riven could not be reached. Try again shortly.", 503) from None
        return _request_status(db, scope, kind, title_id)
    if kind != "movie":
        return status
    row = movies_store.get_request(db, scope.viewer_id, title_id)
    service = MovieService(db, movie_connections(db, user_id))
    try:
        card = await service.refresh(user_id, title_id)
        if card["code"] == "verifying" and not cloud:
            await service.verify(user_id, title_id)
    except MovieError:
        return _request_status(db, scope, kind, title_id, movie_library=library)
    check = card.get("request_check") or {}
    if check.get("performed") and check.get("attempted_at"):
        _record_movie_observation(
            user_id, title_id, row, card.get("code"), check.get("attempted_at")
        )
    status = _request_status(db, scope, kind, title_id, movie_library=library)
    await _fallback_movie_search(db, user_id, title_id, status, card.get("code"), cloud=cloud)
    return status
