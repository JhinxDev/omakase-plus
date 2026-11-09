"""Private For You shelves from bounded catalog reads and local viewing signals."""

from __future__ import annotations

import asyncio
import json
import time
from collections import Counter, OrderedDict
from itertools import islice

import httpx

from omakase.plus.anilist import API_URL, USER_AGENT
from omakase.plus.connections import movie_connections
from omakase.plus.movie_discovery import MovieDiscoveryService
from omakase.plus.movies import MovieError, MovieService, _art
from omakase.plus.product_views import plex_visible_counts

_QUERY = """
query {
  trending: Page(perPage: 42) {
    media(type: ANIME, isAdult: false, sort: TRENDING_DESC) { ...card }
  }
  airing: Page(perPage: 30) {
    media(type: ANIME, isAdult: false, status: RELEASING, sort: POPULARITY_DESC) { ...card }
  }
  films: Page(perPage: 30) {
    media(type: ANIME, isAdult: false, format: MOVIE, status: FINISHED, sort: POPULARITY_DESC) { ...card }
  }
}
fragment card on Media {
  id title { english romaji } format episodes status genres averageScore
}
"""
_CARD_LIMIT = 12
_catalog: tuple[dict, ...] = ()
_catalog_due = 0.0
_catalog_task: asyncio.Task | None = None
_screen_rows = OrderedDict()
_screen_tasks: dict[int, asyncio.Task] = {}


def _json_list(value):
    try:
        result = json.loads(value)
    except (ValueError, TypeError):
        return []
    return result if isinstance(result, list) else []


def _shelf(key, title, cards, note=""):
    return {"key": key, "title": title, "cards": list(islice(cards, _CARD_LIMIT)), "note": note}


def _anime_card(
    media_id, title, *, monitor=None, episodes=None, format="TV", status="", score=None
):
    return {
        "id": media_id,
        "kind": "anime",
        "title": title,
        "href": f"/plus/library/{monitor}" if monitor else f"/plus/catalog/anime/{media_id}",
        "poster": f"/plus/media/{media_id}/poster",
        "format": {"MOVIE": "Anime film", "TV": "Anime series", "TV_SHORT": "Short series"}.get(
            format, "Anime"
        ),
        "episodes": episodes,
        "airing": status == "RELEASING",
        "score": score,
        "badge": "",
    }


def viewer_monitors(db, user_id):
    return db.execute(
        """SELECT m.id, m.anilist_id, m.title, m.format, m.expected_episodes,
                  m.release_status, m.genres_json, m.season_number, w.status,
                  COALESCE(w.progress_episodes,0) AS progress,
                  (SELECT score_ten FROM watch_events e
                   WHERE e.user_id=m.user_id AND e.watch_entry_id=w.id
                     AND e.disposition='accepted' AND e.score_ten IS NOT NULL
                   ORDER BY e.id DESC LIMIT 1) AS rating,
                  (SELECT max(last_played_at) FROM web_playback_watch p
                   WHERE p.user_id=m.user_id AND p.anilist_id=m.anilist_id
                     AND p.season_number=m.season_number AND p.completed_at IS NULL
                     AND p.covered_seconds>0
                     AND p.library_id=(SELECT library_id FROM personal_libraries WHERE user_id=m.user_id)
                  ) AS partial_at,
                  COALESCE(w.last_observed_at,'') AS observed
           FROM media_monitors m LEFT JOIN watch_entries w
             ON w.monitor_id=m.id AND w.user_id=m.user_id
           WHERE m.user_id=? AND NOT EXISTS (
             SELECT 1 FROM library_items i JOIN personal_libraries p ON p.library_id=i.library_id
             WHERE p.user_id=m.user_id AND i.domain='anime' AND i.state='removed'
               AND i.canonical_id=CAST(m.anilist_id AS TEXT)
               AND i.season_number=COALESCE(m.season_number,0))
           ORDER BY m.id DESC LIMIT 300""",
        (user_id,),
    ).fetchall()


def _from_monitor(row):
    card = _anime_card(
        row["anilist_id"],
        row["title"],
        monitor=row["id"],
        episodes=row["expected_episodes"],
        format=row["format"],
        status=row["release_status"],
    )
    card["badge"] = f"{row['progress']} watched" if row["progress"] else "In your library"
    return card


def continue_shelf(monitors):
    current = sorted(
        (
            row
            for row in monitors
            if row["status"] != "completed" and (row["status"] == "watching" or row["partial_at"])
        ),
        key=lambda row: max(row["observed"], row["partial_at"] or ""),
        reverse=True,
    )
    return _shelf("continue", "Continue Watching", map(_from_monitor, current))


def anime_shelves(db, user_id, catalog, monitors):
    owned = {row["anilist_id"]: row for row in monitors}
    saved = {
        row[0]
        for row in db.execute(
            "SELECT anilist_id FROM anilist_plannings WHERE user_id=? AND status='PLANNING'",
            (user_id,),
        )
    }
    excluded = {
        row[0]
        for row in db.execute(
            "SELECT media_id FROM recommendation_feedback WHERE user_id=? AND source='anilist' "
            "AND feedback_type IN ('not_for_me','already_watched','wrong_sequel')",
            (user_id,),
        )
    } | {row["anilist_id"] for row in monitors if row["status"] == "completed"}
    genres = Counter()
    anchors = []
    by_id = {item["id"]: item for item in catalog}
    for row in monitors:
        known = [value for value in _json_list(row["genres_json"]) if isinstance(value, str)]
        rating = row["rating"]
        weight = rating - 5 if rating else 2 if row["status"] == "completed" else 1
        for genre in known:
            genres[genre] += weight
        if known and (rating and rating >= 8 or rating is None and row["status"] == "completed"):
            anchors.append((rating or 6, row["title"], set(known)))
    for media_id in saved - owned.keys():
        genres.update(by_id.get(media_id, {}).get("genres", []))

    pool = [card for card in catalog if card["id"] not in excluded]
    ranked = sorted(
        pool,
        key=lambda card: (
            sum(genres[g] for g in card["genres"]) / max(1, len(card["genres"])),
            card["score"] or 0,
        ),
        reverse=True,
    )
    visible = plex_visible_counts(db) if owned.keys() & by_id.keys() else {}

    def project(item):
        card = dict(item)
        row = owned.get(card["id"])
        if row:
            card["href"] = f"/plus/library/{row['id']}"
            card["badge"] = (
                "On Plex"
                if visible.get((card["id"], row["season_number"]), (0, 0))[1] > 0
                else "In your library"
            )
            card["owned"] = True
        elif card["id"] in saved:
            card["badge"] = "Saved"
            card["saved"] = True
        return card

    fresh = [card for card in ranked if card["id"] not in owned]
    rows = [
        _shelf(
            "top",
            "Top Picks for You",
            map(project, fresh or ranked),
            "Matched to your viewing and ratings."
            if any(genres.values())
            else "Popular picks to get you started.",
        )
    ]
    for index, (_, title, liked) in enumerate(sorted(anchors, reverse=True)[:2]):
        matched = [item for item in fresh if liked.intersection(item["genres"])]
        if matched:
            rows.append(
                _shelf(f"liked-{index}", f"Because You Liked {title}", map(project, matched))
            )
    for index, (genre, weight) in enumerate(genres.most_common(2)):
        if weight > 0:
            matches = [item for item in ranked if genre in item["genres"]]
            if matches:
                rows.append(
                    _shelf(
                        f"genre-{index}",
                        genre,
                        map(project, matches),
                        "One of your favorite genres",
                    )
                )
    rows.extend(
        (
            _shelf(
                "airing", "Currently Airing", (project(item) for item in ranked if item["airing"])
            ),
            _shelf(
                "films",
                "Anime Movies for You",
                (project(item) for item in ranked if item["format"] == "Anime film"),
            ),
            _shelf(
                "new",
                "New for You",
                islice(
                    (project(item) for item in fresh if item["id"] not in saved), _CARD_LIMIT, None
                ),
            ),
        )
    )
    return [row for row in rows if row["cards"]]


async def _refresh_catalog():
    global _catalog, _catalog_due
    _catalog_due = time.monotonic() + 60
    try:
        async with asyncio.timeout(6), httpx.AsyncClient(timeout=5) as client:
            async with client.stream(
                "POST", API_URL, headers={"User-Agent": USER_AGENT}, json={"query": _QUERY}
            ) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 512 * 1024:
                        return
        pages = json.loads(body).get("data") or {}
        cards = {}
        for name in ("trending", "airing", "films"):
            for item in (pages.get(name) or {}).get("media", [])[:42]:
                media_id = item.get("id")
                title = (item.get("title") or {}).get("english") or (item.get("title") or {}).get(
                    "romaji"
                )
                if (
                    type(media_id) is not int
                    or not 0 < media_id <= 2**31 - 1
                    or not isinstance(title, str)
                    or not title
                ):
                    continue
                card = _anime_card(
                    media_id,
                    title[:240],
                    episodes=item.get("episodes"),
                    format=item.get("format"),
                    status=item.get("status"),
                    score=item.get("averageScore"),
                )
                card["genres"] = tuple(g for g in item.get("genres", []) if isinstance(g, str))[:12]
                cards[media_id] = card
        if cards:
            _catalog = tuple(cards.values())
            _catalog_due = time.monotonic() + 1800
    except (ValueError, TimeoutError, httpx.HTTPError, TypeError, AttributeError):
        return


async def anime_catalog():
    global _catalog_task
    if time.monotonic() >= _catalog_due and (_catalog_task is None or _catalog_task.done()):
        _catalog_task = asyncio.create_task(_refresh_catalog())
    if not _catalog and _catalog_task is not None:
        await asyncio.shield(_catalog_task)
    return _catalog


def _screen_signature(db, user_id):
    return tuple(
        tuple(row)
        for query in (
            "SELECT 'movie',tmdb_id,status,rating FROM movie_history WHERE user_id=? ORDER BY tmdb_id",
            "SELECT 'tv',tmdb_id,status,rating FROM tv_history WHERE user_id=? ORDER BY tmdb_id",
        )
        for row in db.execute(query, (user_id,))
    )


async def _screen_shelves(db, user_id, signature):
    svc = MovieService(db, movie_connections(db, user_id))

    async def movies():
        try:
            cards = await MovieDiscoveryService(svc).discover(user_id, limit=_CARD_LIMIT)
        except MovieError:
            return []
        return [
            {
                "title": card["title"],
                "href": f"/plus/movies/{card['imdb_id']}",
                "poster": f"/plus/movies/{card['imdb_id']}/artwork"
                if card.get("poster_url")
                else None,
                "format": "Movie",
                "badge": "In your library" if card.get("state") == "ready" else "",
            }
            for card in cards
            if card.get("imdb_id")
        ]

    async def television():
        try:
            data = await svc._json("seerr", "/api/v1/discover/tv", params={"page": "1"})
        except MovieError:
            return []
        watched = {
            media_id
            for kind, media_id, status, _ in signature
            if kind == "tv" and status in {"completed", "dropped"}
        }
        watching = {
            mid for kind, mid, status, _ in signature if kind == "tv" and status == "watching"
        }
        present = {
            row[0]
            for row in db.execute(
                "SELECT DISTINCT tmdb_id FROM tv_episodes WHERE user_id=? AND plex_present=1",
                (user_id,),
            )
        }
        favorites = Counter()
        for row in db.execute(
            "SELECT c.metadata_json, h.rating FROM tv_history h JOIN tv_catalog c USING(user_id,tmdb_id) "
            "WHERE h.user_id=? AND (h.rating IS NOT NULL OR h.status='completed')",
            (user_id,),
        ):
            for genre in json.loads(row["metadata_json"]).get("genres", []):
                if isinstance(genre, dict) and type(genre.get("id")) is int:
                    favorites[genre["id"]] += (row["rating"] or 7) - 5
        cards = []
        results = data.get("results")
        if not isinstance(results, list):
            return []
        for item in results[:30]:
            if not isinstance(item, dict):
                continue
            media_id = item.get("id")
            title = item.get("name")
            if (
                type(media_id) is not int
                or not 0 < media_id <= 2**31 - 1
                or not isinstance(title, str)
                or media_id in watched
                or item.get("adult")
            ):
                continue
            try:
                artwork = _art(item.get("posterPath"), "w342")
            except MovieError:
                artwork = None
            score = sum(
                favorites[genre] for genre in (item.get("genreIds") or ()) if type(genre) is int
            )
            cards.append(
                {
                    "id": media_id,
                    "title": title[:240],
                    "href": f"/plus/tv/{media_id}",
                    "poster": f"/plus/discover/tv-art/{media_id}" if artwork else None,
                    "artwork": artwork,
                    "format": "TV show",
                    "badge": "On Plex"
                    if media_id in present
                    else "Watching"
                    if media_id in watching
                    else "",
                    "affinity": score,
                }
            )
        return sorted(cards, key=lambda card: card["affinity"], reverse=True)[:_CARD_LIMIT]

    movies_row, tv_row = await asyncio.gather(movies(), television())
    return [
        row
        for row in (
            _shelf("movies", "Movies for You", movies_row),
            _shelf("tv", "TV Shows for You", tv_row),
        )
        if row["cards"]
    ]


async def screen_shelves(db, user_id):
    signature = _screen_signature(db, user_id)
    cached = _screen_rows.get(user_id)
    now = time.monotonic()
    if cached and cached[0] > now and cached[1] == signature:
        return cached[2]
    if user_id in _screen_tasks:
        return await asyncio.shield(_screen_tasks[user_id])
    if len(_screen_tasks) >= 4:
        return cached[2] if cached else []
    # The route owns its DB connection until this task completes.
    task = asyncio.create_task(_screen_shelves(db, user_id, signature))
    _screen_tasks[user_id] = task
    try:
        rows = await task
    finally:
        _screen_tasks.pop(user_id, None)
    _screen_rows[user_id] = (now + (600 if rows else 60), signature, rows)
    _screen_rows.move_to_end(user_id)
    if len(_screen_rows) > 64:
        _screen_rows.popitem(last=False)
    return rows


def television_artwork(user_id, media_id):
    cached = _screen_rows.get(user_id)
    if not cached:
        return None
    return next(
        (
            card["artwork"]
            for row in cached[2]
            if row["key"] == "tv"
            for card in row["cards"]
            if card["id"] == media_id
        ),
        None,
    )
