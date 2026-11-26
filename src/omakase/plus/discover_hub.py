"""Private For You shelves from bounded catalog reads and local viewing signals."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import Counter, OrderedDict
from itertools import islice

import httpx

from omakase.plus import for_you_layout, movies_store
from omakase.plus.anilist import API_URL, USER_AGENT
from omakase.plus.connections import movie_connections
from omakase.plus.http_pool import client_scope
from omakase.plus.movie_discovery import MovieDiscoveryService
from omakase.plus.movies import MovieError, MovieService, _art, _iso
from omakase.plus.product_views import plex_visible_counts
from omakase.plus.secrets import read_secret
from omakase.plus.trailers import extract_anilist_trailer

_QUERY = """
query {
  trending: Page(perPage: 42) {
    media(type: ANIME, isAdult: false, sort: TRENDING_DESC) { ...card }
  }
  finishedPopular: Page(perPage: 50) {
    media(type: ANIME, isAdult: false, status: FINISHED,
          format_in: [TV, TV_SHORT, ONA, OVA], sort: POPULARITY_DESC) { ...card }
  }
  finishedRated: Page(perPage: 50) {
    media(type: ANIME, isAdult: false, status: FINISHED,
          format_in: [TV, TV_SHORT, ONA, OVA], sort: SCORE_DESC) { ...card }
  }
  hiddenGems: Page(perPage: 40) {
    media(type: ANIME, isAdult: false, status: FINISHED,
          format_in: [TV, TV_SHORT, ONA, OVA], averageScore_greater: 74,
          popularity_lesser: 120000, sort: SCORE_DESC) { ...card }
  }
  airing: Page(perPage: 30) {
    media(type: ANIME, isAdult: false, status: RELEASING, sort: POPULARITY_DESC) { ...card }
  }
  films: Page(perPage: 30) {
    media(type: ANIME, isAdult: false, format: MOVIE, status: FINISHED, sort: POPULARITY_DESC) { ...card }
  }
}
fragment card on Media {
  id
  title { english romaji }
  format
  episodes
  status
  seasonYear
  startDate { year }
  popularity
  genres
  averageScore
  studios(isMain: true) { nodes { name } }
  tags { name rank isMediaSpoiler }
  trailer { id site }
  coverImage { large }
}
"""
_SEED_RECS_QUERY = """
query ($id: Int) {
  Media(id: $id, type: ANIME) {
    id
    title { english romaji }
    recommendations(perPage: 18, sort: RATING_DESC) {
      nodes {
        rating
        mediaRecommendation {
          id
          isAdult
          title { english romaji }
          format
          episodes
          status
          seasonYear
          startDate { year }
          popularity
          genres
          averageScore
          studios(isMain: true) { nodes { name } }
          tags { name rank isMediaSpoiler }
          trailer { id site }
          coverImage { large }
        }
      }
    }
  }
}
"""
_CARD_LIMIT = 15
_catalog: tuple[dict, ...] = ()
_catalog_due = 0.0
_catalog_task: asyncio.Task | None = None
_seed_recs: OrderedDict[int, tuple[float, list[dict]]] = OrderedDict()
_screen_rows = OrderedDict()
_screen_tasks: dict[tuple[int, str], asyncio.Task] = {}
_history = OrderedDict()
_history_tasks: dict[tuple[int, str], asyncio.Task] = {}
_HISTORY_QUERY = """
query ($name: String, $id: Int, $chunk: Int) {
  MediaListCollection(userName: $name, userId: $id, type: ANIME, chunk: $chunk, perChunk: 500) {
    hasNextChunk
    lists {
      entries {
        mediaId
        status
        progress
        score(format: POINT_10_DECIMAL)
        media { title { english romaji } }
      }
    }
  }
}
"""


def _json_list(value):
    try:
        result = json.loads(value)
    except (ValueError, TypeError):
        return []
    return result if isinstance(result, list) else []


def _shelf(key, title, cards, note="", *, limit=_CARD_LIMIT, kind=None):
    shelf = {"key": key, "title": title, "cards": list(islice(cards, limit)), "note": note}
    if kind is not None:
        shelf["kind"] = kind
    return shelf


def _anime_card(
    media_id,
    title,
    *,
    monitor=None,
    episodes=None,
    format="TV",
    status="",
    score=None,
    year=None,
    trailer=None,
    studios=(),
    tags=(),
    popularity=None,
    score_label="AniList",
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
        "score_label": score_label,
        "year": year,
        "trailer": trailer,
        "studios": tuple(studios),
        "tags": tuple(tags),
        "popularity": popularity,
        "badge": "",
    }


def _parse_anilist_card(item: dict) -> dict | None:
    if not isinstance(item, dict) or item.get("isAdult"):
        return None
    media_id = item.get("id")
    title = (item.get("title") or {}).get("english") or (item.get("title") or {}).get("romaji")
    if (
        type(media_id) is not int
        or not 0 < media_id <= 2**31 - 1
        or not isinstance(title, str)
        or not title
    ):
        return None
    start = item.get("startDate") if isinstance(item.get("startDate"), dict) else {}
    year = item.get("seasonYear") or start.get("year")
    if type(year) is not int or not 1870 <= year <= 2200:
        year = None
    studio_nodes = (item.get("studios") or {}).get("nodes") or []
    studios = tuple(
        node["name"][:120]
        for node in studio_nodes
        if isinstance(node, dict) and isinstance(node.get("name"), str) and node["name"]
    )[:4]
    raw_tags = item.get("tags") or []
    tags = tuple(
        tag["name"][:80]
        for tag in raw_tags
        if isinstance(tag, dict)
        and not tag.get("isMediaSpoiler")
        and isinstance(tag.get("name"), str)
        and tag["name"]
        and (type(tag.get("rank")) is not int or tag["rank"] >= 55)
    )[:10]
    card = _anime_card(
        media_id,
        title[:240],
        episodes=item.get("episodes"),
        format=item.get("format"),
        status=item.get("status"),
        score=item.get("averageScore"),
        year=year,
        trailer=extract_anilist_trailer(item.get("trailer")),
        studios=studios,
        tags=tags,
        popularity=item.get("popularity") if type(item.get("popularity")) is int else None,
    )
    card["poster_url"] = (item.get("coverImage") or {}).get("large")
    card["genres"] = tuple(g for g in item.get("genres", []) if isinstance(g, str))[:12]
    return card


def invalidate_tracker_cache(user_id: int) -> None:
    """Clear cached AniList history/seeds for ``user_id`` after list updates."""
    for key in [entry_key for entry_key in _history if entry_key[0] == user_id]:
        _history.pop(key, None)


def invalidate_screen_cache(user_id: int, kind: str | None = None) -> None:
    """Clear cached Movie/TV For You shelves for ``user_id`` after tracking updates."""
    kinds = (kind,) if kind else ("movies", "tv")
    for category in kinds:
        _screen_rows.pop((user_id, category), None)


def cached_anime_trailer(anilist_id: int) -> dict[str, str] | None:
    """Return any in-memory cached trailer for ``anilist_id`` without extra I/O."""
    for card in _catalog:
        if card.get("id") == anilist_id and card.get("trailer"):
            return card["trailer"]
    for _, recs in _seed_recs.values():
        for card in recs:
            if card.get("id") == anilist_id and card.get("trailer"):
                return card["trailer"]
    return None


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


def _feedback_exclusions(db, user_id: int, source: str) -> tuple[set[int], set[str]]:
    rows = db.execute(
        """SELECT media_id, title FROM recommendation_feedback
           WHERE user_id=? AND source=? AND feedback_type IN ('not_for_me','already_watched','wrong_sequel')""",
        (user_id, source),
    ).fetchall()
    ids = {int(row["media_id"]) for row in rows if row["media_id"] is not None}
    titles = {str(row["title"] or "").strip().casefold() for row in rows if row["title"]}
    return ids, titles


def anime_shelves(
    db,
    user_id,
    catalog,
    monitors,
    tracker_watched=(),
    *,
    seed=None,
    seed_cards=(),
    ai_picks=(),
):
    owned = {row["anilist_id"]: row for row in monitors}
    saved = {
        row[0]
        for row in db.execute(
            "SELECT anilist_id FROM anilist_plannings WHERE user_id=? AND status='PLANNING'",
            (user_id,),
        )
    }
    excluded, excluded_titles = _feedback_exclusions(db, user_id, "anilist")
    excluded.update(tracker_watched)
    excluded.update(
        row[0]
        for row in db.execute(
            """SELECT anilist_id FROM watch_entries
                 WHERE user_id=? AND (status IN ('watching','completed') OR progress_episodes>0)
               UNION SELECT anilist_id FROM web_playback_watch
                 WHERE user_id=? AND covered_seconds>0
               UNION SELECT anilist_id FROM anilist_plannings
                 WHERE user_id=? AND status IN ('COMPLETED','CURRENT','REPEATING','DROPPED','PAUSED')""",
            (user_id, user_id, user_id),
        )
    )
    excluded.update(
        row["anilist_id"] for row in monitors if row["status"] in {"watching", "completed"}
    )

    genres = Counter()
    studios = Counter()
    tags = Counter()
    by_id = {item["id"]: item for item in catalog}
    for extra in seed_cards:
        by_id.setdefault(extra["id"], extra)
    for row in monitors:
        known = [value for value in _json_list(row["genres_json"]) if isinstance(value, str)]
        rating = row["rating"]
        weight = rating - 5 if rating else 2 if row["status"] == "completed" else 1
        for genre in known:
            genres[genre] += weight
        matched = by_id.get(row["anilist_id"])
        if matched:
            for studio in matched.get("studios", ()):
                studios[studio] += weight
            for tag in matched.get("tags", ()):
                tags[tag] += weight * 0.5
    for media_id in saved - owned.keys():
        cached = by_id.get(media_id, {})
        genres.update(cached.get("genres", ()))
        studios.update(cached.get("studios", ()))

    seed_boost = {
        item["id"]: float(item.get("_rec_rating") or 10)
        for item in seed_cards
        if isinstance(item, dict) and item.get("id")
    }

    def affinity(card: dict) -> float:
        card_genres = card.get("genres") or ()
        genre_score = (
            sum(genres[g] for g in card_genres) / max(1, len(card_genres)) if card_genres else 0.0
        )
        studio_score = sum(studios[s] for s in (card.get("studios") or ())) * 0.65
        tag_score = sum(tags[t] for t in (card.get("tags") or ())) * 0.35
        collab = min(4.0, seed_boost.get(card["id"], 0.0) / 10.0)
        quality = (card.get("score") or 0) / 25.0
        return genre_score + studio_score + tag_score + collab + quality

    def eligible(card: dict) -> bool:
        title_key = str(card.get("title") or "").strip().casefold()
        return card["id"] not in excluded and title_key not in excluded_titles

    pool = [card for card in by_id.values() if eligible(card)]
    ranked = sorted(
        pool,
        key=lambda card: (card["id"] in owned, -affinity(card), -(card.get("score") or 0)),
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

    if not ranked:
        return []

    top_picks = ranked[:30]
    for index, key_fn in (
        (4, lambda card: card.get("airing")),
        (7, lambda card: card.get("format") == "Anime film"),
    ):
        if any(key_fn(card) for card in top_picks[:index]):
            continue
        alternate = next((card for card in ranked if key_fn(card)), None)
        if alternate is not None:
            top_picks = [card for card in top_picks if card["id"] != alternate["id"]]
            top_picks.insert(index, alternate)

    has_signal = bool(any(genres.values()) or studios or tags or seed_boost)
    shelves = [
        _shelf(
            "top",
            "Top Picks for You",
            [project(card) for card in top_picks[:30]],
            None if has_signal else "Popular picks to get you started.",
            limit=30,
            kind="anime",
        )
    ]

    top_head_ids = {card["id"] for card in top_picks[:12]}
    seed_title = str((seed or {}).get("title") or "").strip()
    because = [project(card) for card in seed_cards if eligible(card)]
    if len(because) >= 3:
        shelf_title = f"Because You Liked {seed_title}" if seed_title else "Because You Liked"
        shelves.append(_shelf("because_you_liked", shelf_title, because, limit=15, kind="anime"))

    gems = [
        project(card)
        for card in ranked
        if not card.get("airing")
        and card.get("format") != "Anime film"
        and (card.get("score") or 0) >= 74
        and (card.get("popularity") is None or card["popularity"] <= 140000)
        and (len(ranked) <= 20 or card["id"] not in top_head_ids)
    ]
    if len(gems) >= 3:
        shelves.append(
            _shelf(
                "hidden_gems",
                "Hidden Gems",
                gems,
                "High-scoring finished series beyond the usual mainstream list.",
                limit=15,
                kind="anime",
            )
        )

    airing = [project(card) for card in ranked if card.get("airing")]
    if len(airing) >= 2:
        shelves.append(_shelf("airing", "Airing This Season", airing, limit=15, kind="anime"))

    films = [project(card) for card in ranked if card.get("format") == "Anime film"]
    if len(films) >= 2:
        shelves.append(_shelf("films", "Anime Films", films, limit=15, kind="anime"))

    ai_cards = []
    seen_ai: set[int] = set()
    for pick in ai_picks:
        media_id = pick.get("anilist_id") or pick.get("media_id")
        if type(media_id) is not int or media_id in seen_ai or media_id in excluded:
            continue
        title = str(pick.get("title") or "").strip()
        if not title or title.casefold() in excluded_titles:
            continue
        seen_ai.add(media_id)
        cached = by_id.get(media_id)
        card = (
            dict(cached)
            if cached
            else _anime_card(
                media_id,
                title,
                episodes=pick.get("episodes"),
                format=pick.get("format") or "TV",
                status=pick.get("airing_status") or "",
                score=round(pick["predicted_score"] * 10) if pick.get("predicted_score") else None,
                score_label="AI match" if pick.get("predicted_score") else "AniList",
            )
        )
        projected = project(card)
        if pick.get("predicted_score"):
            projected["badge"] = projected.get("badge") or f"{pick['predicted_score']}/10 AI fit"
        ai_cards.append(projected)
    if ai_cards:
        shelves.append(
            _shelf(
                "ai_picks",
                "AI Tasting Menu",
                ai_cards,
                "From your latest AI recommendations.",
                limit=15,
                kind="anime",
            )
        )

    layout = for_you_layout.load_layout(db, user_id, "anime")["rows"]
    return for_you_layout.order_shelves("anime", layout, shelves)


async def _anilist_query(client, query, variables=None, token=None):
    headers = {"User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with client.stream(
        "POST", API_URL, headers=headers, json={"query": query, "variables": variables or {}}
    ) as response:
        response.raise_for_status()
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > 512 * 1024:
                raise ValueError("AniList response too large")
    payload = json.loads(body)
    if payload.get("errors") or not isinstance(payload.get("data"), dict):
        raise ValueError("AniList returned an incomplete response")
    return payload["data"]


async def _read_watch_history_and_seeds(token, username):
    async with (
        asyncio.timeout(7),
        client_scope(key="discover-hub", timeout=5, client_cls=httpx.AsyncClient) as client,
    ):
        variables = {"name": username}
        if token:
            identity = await _anilist_query(client, "query { Viewer { id } }", token=token)
            viewer = identity["Viewer"]["id"]
            if type(viewer) is not int or viewer <= 0:
                raise ValueError("AniList viewer unavailable")
            variables = {"id": viewer}
        watched = set()
        seeds: list[tuple[float, int, str]] = []
        for chunk in range(1, 11):
            data = await _anilist_query(
                client, _HISTORY_QUERY, {**variables, "chunk": chunk}, token=token
            )
            collection = data["MediaListCollection"]
            for group in collection["lists"]:
                for entry in group["entries"]:
                    media_id = entry.get("mediaId")
                    if type(media_id) is not int or media_id <= 0:
                        continue
                    status = entry.get("status")
                    progress = entry.get("progress")
                    if status in {"CURRENT", "COMPLETED", "REPEATING", "DROPPED", "PAUSED"} or (
                        type(progress) is int and progress > 0
                    ):
                        watched.add(media_id)
                    score = entry.get("score")
                    if (
                        status == "COMPLETED"
                        and type(score) in {int, float}
                        and float(score) >= 7.5
                    ):
                        media_title = (entry.get("media") or {}).get("title") or {}
                        label = media_title.get("english") or media_title.get("romaji") or ""
                        seeds.append((float(score), media_id, str(label)))
            if collection["hasNextChunk"] is False:
                seeds.sort(key=lambda row: (row[0], row[1]), reverse=True)
                return frozenset(watched), tuple(
                    {"id": mid, "title": label, "score": score}
                    for score, mid, label in seeds[:5]
                    if label
                )
        raise ValueError("AniList history is incomplete")


async def _read_watch_history(token, username):
    watched, _ = await _read_watch_history_and_seeds(token, username)
    return watched


async def _refresh_watch_history(key, token, username):
    try:
        result = await _read_watch_history_and_seeds(token, username)
        lifetime = 120
    except (ValueError, KeyError, TimeoutError, httpx.HTTPError, TypeError, AttributeError):
        result = None
        lifetime = 20
    _history[key] = (time.monotonic() + lifetime, result)
    _history.move_to_end(key)
    if len(_history) > 64:
        _history.popitem(last=False)
    return result


async def _tracker_state(db, user_id):
    token = read_secret(db, user_id, "anilist_oauth_token")
    username = None if token else read_secret(db, user_id, "anilist_username")
    if not token and not username:
        return frozenset(), ()
    binding = hashlib.sha256(
        (("oauth:" + token) if token else ("name:" + username)).encode()
    ).hexdigest()
    key = (user_id, binding)
    cached = _history.get(key)
    if cached and cached[0] > time.monotonic():
        value = cached[1]
        return (None, ()) if value is None else value
    task = _history_tasks.get(key)
    if task is None:
        if len(_history_tasks) >= 4:
            return None, ()
        task = asyncio.create_task(_refresh_watch_history(key, token, username))
        _history_tasks[key] = task
        task.add_done_callback(lambda _: _history_tasks.pop(key, None))
    value = await asyncio.shield(task)
    return (None, ()) if value is None else value


async def tracker_watched_ids(db, user_id):
    watched, _ = await _tracker_state(db, user_id)
    return watched


async def tracker_top_seed(db, user_id):
    """Return the viewer's top-rated AniList anime or local rated monitor for collaborative picks."""
    _, seeds = await _tracker_state(db, user_id)
    if seeds:
        return seeds[0]
    row = db.execute(
        """SELECT m.anilist_id, m.title, e.score_ten
           FROM watch_events e
           JOIN watch_entries w ON w.id = e.watch_entry_id
           JOIN media_monitors m ON m.id = w.monitor_id
           WHERE e.user_id=? AND e.disposition='accepted' AND e.score_ten >= 7.5
           ORDER BY e.score_ten DESC, e.id DESC LIMIT 1""",
        (user_id,),
    ).fetchone()
    if row is not None:
        return {"id": int(row["anilist_id"]), "title": str(row["title"]), "score": row["score_ten"]}
    return None


async def anime_seed_recommendations(seed_id: int) -> list[dict]:
    """Fetch bounded AniList collaborative recommendations for ``seed_id``."""
    now = time.monotonic()
    cached = _seed_recs.get(seed_id)
    if cached and cached[0] > now:
        return cached[1]
    cards: list[dict] = []
    try:
        async with (
            asyncio.timeout(5),
            client_scope(key="discover-hub", timeout=4, client_cls=httpx.AsyncClient) as client,
        ):
            data = await _anilist_query(client, _SEED_RECS_QUERY, {"id": seed_id})
        nodes = ((data.get("Media") or {}).get("recommendations") or {}).get("nodes") or []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            card = _parse_anilist_card(node.get("mediaRecommendation") or {})
            if card is None:
                continue
            rec_rating = node.get("rating")
            if type(rec_rating) is int and rec_rating > 0:
                card["_rec_rating"] = rec_rating
            cards.append(card)
    except (ValueError, TimeoutError, httpx.HTTPError, TypeError, AttributeError):
        cards = []
    _seed_recs[seed_id] = (now + (1800 if cards else 60), cards)
    _seed_recs.move_to_end(seed_id)
    if len(_seed_recs) > 64:
        _seed_recs.popitem(last=False)
    return cards


async def _refresh_catalog():
    global _catalog, _catalog_due
    _catalog_due = time.monotonic() + 60
    try:
        async with (
            asyncio.timeout(6),
            client_scope(key="discover-hub", timeout=5, client_cls=httpx.AsyncClient) as client,
        ):
            pages = await _anilist_query(client, _QUERY)
        cards = {}
        for name in (
            "trending",
            "finishedPopular",
            "finishedRated",
            "hiddenGems",
            "airing",
            "films",
        ):
            for item in (pages.get(name) or {}).get("media", [])[:50]:
                card = _parse_anilist_card(item)
                if card is not None:
                    cards[card["id"]] = card
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
    queries = (
        "SELECT 'movie',tmdb_id,status,rating FROM movie_history WHERE user_id=? ORDER BY tmdb_id",
        "SELECT 'tv',tmdb_id,status,rating FROM tv_history WHERE user_id=? ORDER BY tmdb_id",
        "SELECT 'fb',source,COALESCE(media_id,0),feedback_type FROM recommendation_feedback WHERE user_id=? AND source IN ('movie','tv') ORDER BY id DESC LIMIT 100",
        "SELECT 'layout',kind,revision FROM viewer_discover_layout WHERE user_id=? ORDER BY kind",
    )
    return tuple(tuple(row) for query in queries for row in db.execute(query, (user_id,)))


def _project_movie_card(card: dict) -> dict:
    vote_avg = card.get("vote_average") or card.get("voteAverage")
    score = round(float(vote_avg) * 10) if type(vote_avg) in {int, float} and vote_avg > 0 else None
    return {
        "id": card["imdb_id"],
        "tmdb_id": card.get("tmdb_id"),
        "kind": "movie",
        "title": card["title"],
        "href": f"/plus/movies/{card['imdb_id']}",
        "poster": f"/plus/movies/{card['imdb_id']}/artwork" if card.get("poster_url") else None,
        "poster_url": card.get("poster_url"),
        "summary": card.get("overview"),
        "format": "Movie",
        "year": card.get("year"),
        "score": score,
        "score_label": "TMDB",
        "trailer": card.get("trailer"),
        "badge": "In your library" if card.get("state") == "ready" else "",
        "available": card.get("state") == "ready",
    }


def _seed_movie(db, user_id: int):
    return db.execute(
        """SELECT h.tmdb_id, h.rating, c.title
           FROM movie_history h
           JOIN movie_catalog c ON c.user_id=h.user_id AND c.imdb_id=h.imdb_id
           WHERE h.user_id=? AND h.rating >= 7
           ORDER BY h.rating DESC, h.updated_at DESC LIMIT 1""",
        (user_id,),
    ).fetchone()


def _seed_tv(db, user_id: int):
    return db.execute(
        """SELECT h.tmdb_id, h.rating,
                  COALESCE(
                      json_extract(c.metadata_json, '$.title'),
                      json_extract(c.metadata_json, '$.name'),
                      ''
                  ) AS title
           FROM tv_history h
           JOIN tv_catalog c ON c.user_id=h.user_id AND c.tmdb_id=h.tmdb_id
           WHERE h.user_id=? AND h.rating >= 7
           ORDER BY h.rating DESC, h.updated_at DESC LIMIT 1""",
        (user_id,),
    ).fetchone()


async def _screen_shelves(db, user_id, signature, kind):
    svc = MovieService(db, movie_connections(db, user_id))

    async def movies():
        excluded_ids, excluded_titles = _feedback_exclusions(db, user_id, "movie")
        watched_movie_ids = {
            mid
            for k, mid, status, _ in signature
            if k == "movie" and status in {"completed", "dropped", "watching"}
        }
        excluded_ids |= watched_movie_ids
        discovery = MovieDiscoveryService(svc)

        def allow_movie(card: dict) -> bool:
            if not card.get("imdb_id"):
                return False
            tmdb_id = card.get("tmdb_id")
            title_key = str(card.get("title") or "").strip().casefold()
            return tmdb_id not in excluded_ids and title_key not in excluded_titles

        async def load_sort(sort: str) -> list[dict]:
            try:
                rows = await discovery.discover(user_id, sort=sort, limit=_CARD_LIMIT)
            except MovieError:
                return []
            return [_project_movie_card(row) for row in rows if allow_movie(row)]

        async def load_because() -> tuple[str, list[dict]]:
            seed = _seed_movie(db, user_id)
            if seed is None:
                return "", []
            try:
                data = await svc._json(
                    "seerr",
                    f"/api/v1/movie/{int(seed['tmdb_id'])}/recommendations",
                    params={"page": "1"},
                )
            except MovieError:
                return "", []
            results = data.get("results")
            if not isinstance(results, list):
                return "", []
            candidates = []
            for row in results[:12]:
                if not isinstance(row, dict) or row.get("adult"):
                    continue
                tmdb_id = row.get("id")
                title = row.get("title")
                if type(tmdb_id) is not int or tmdb_id <= 0 or tmdb_id in excluded_ids:
                    continue
                if isinstance(title, str) and title.strip().casefold() in excluded_titles:
                    continue
                candidates.append((tmdb_id, row))
            semaphore = asyncio.Semaphore(4)

            async def resolve_candidate(tmdb_id: int, row: dict) -> dict | None:
                async with semaphore:
                    try:
                        meta, _ = await discovery._cache(
                            "detail",
                            tmdb_id,
                            lambda: svc._metadata(tmdb_id),
                        )
                    except MovieError:
                        return None
                if meta.get("imdb_id"):
                    try:
                        movies_store.upsert_movie(
                            db,
                            user_id,
                            meta,
                            now=_iso(svc._clock()),
                        )
                    except ValueError:
                        pass
                card = svc._card(user_id, meta)
                card["vote_average"] = row.get("voteAverage")
                card["trailer"] = meta.get("trailer")
                return _project_movie_card(card)

            resolved = await asyncio.gather(
                *(resolve_candidate(tmdb_id, row) for tmdb_id, row in candidates)
            )
            picked = [card for card in resolved if card is not None and card.get("id")]
            return str(seed["title"]), picked

        (
            top_cards,
            (seed_title, because_cards),
            acclaimed_cards,
            newest_cards,
        ) = await asyncio.gather(
            load_sort("personalized"),
            load_because(),
            load_sort("top_rated"),
            load_sort("newest"),
        )
        shelves: list[dict] = []
        if top_cards:
            shelves.append(_shelf("top", "Top Picks for You", top_cards, kind="movie"))
        if len(because_cards) >= 3:
            shelves.append(
                _shelf(
                    "because_you_liked",
                    f"Because You Liked {seed_title}",
                    because_cards,
                    kind="movie",
                )
            )
        if acclaimed_cards:
            shelves.append(
                _shelf("hidden_gems", "Top Rated & Acclaimed", acclaimed_cards, kind="movie")
            )
        if newest_cards:
            shelves.append(_shelf("new_releases", "New Releases", newest_cards, kind="movie"))
        layout = for_you_layout.load_layout(db, user_id, "movies")["rows"]
        return for_you_layout.order_shelves("movies", layout, shelves)

    async def television():
        excluded_ids, excluded_titles = _feedback_exclusions(db, user_id, "tv")
        watched = {
            media_id
            for kind_name, media_id, status, _ in signature
            if kind_name == "tv" and status in {"completed", "dropped", "watching"}
        }
        excluded_ids |= watched
        watching = {
            mid
            for kind_name, mid, status, _ in signature
            if kind_name == "tv" and status == "watching"
        }
        present = {
            row[0]
            for row in db.execute(
                "SELECT DISTINCT tmdb_id FROM tv_episodes WHERE user_id=? AND plex_present=1",
                (user_id,),
            )
        }
        cached_trailers: dict[int, dict[str, str]] = {}
        for row in db.execute(
            "SELECT tmdb_id, metadata_json FROM tv_catalog WHERE user_id=?",
            (user_id,),
        ):
            try:
                meta = json.loads(row["metadata_json"])
            except (ValueError, TypeError):
                continue
            if isinstance(meta, dict) and isinstance(meta.get("trailer"), dict):
                cached_trailers[int(row["tmdb_id"])] = meta["trailer"]

        favorites = Counter()
        for row in db.execute(
            "SELECT c.metadata_json, h.rating FROM tv_history h JOIN tv_catalog c USING(user_id,tmdb_id) "
            "WHERE h.user_id=? AND (h.rating IS NOT NULL OR h.status='completed')",
            (user_id,),
        ):
            for genre in json.loads(row["metadata_json"]).get("genres", []):
                if isinstance(genre, dict) and type(genre.get("id")) is int:
                    favorites[genre["id"]] += (row["rating"] or 7) - 5

        def project_tv_list(results: list, *, rank_by_taste: bool) -> list[dict]:
            cards = []
            for item in results[:30]:
                if not isinstance(item, dict):
                    continue
                media_id = item.get("id")
                title = item.get("name")
                if (
                    type(media_id) is not int
                    or not 0 < media_id <= 2**31 - 1
                    or not isinstance(title, str)
                    or media_id in excluded_ids
                    or title.strip().casefold() in excluded_titles
                    or item.get("adult")
                ):
                    continue
                try:
                    artwork = _art(item.get("posterPath"), "w342")
                except MovieError:
                    artwork = None
                first_air = item.get("firstAirDate")
                year = (
                    int(first_air[:4])
                    if isinstance(first_air, str)
                    and len(first_air) >= 4
                    and first_air[:4].isdigit()
                    else None
                )
                vote_avg = item.get("voteAverage")
                score = (
                    round(float(vote_avg) * 10)
                    if type(vote_avg) in {int, float} and vote_avg > 0
                    else None
                )
                affinity = sum(
                    favorites[genre] for genre in (item.get("genreIds") or ()) if type(genre) is int
                )
                cards.append(
                    {
                        "id": media_id,
                        "tmdb_id": media_id,
                        "title": title[:240],
                        "href": f"/plus/tv/{media_id}",
                        "poster": f"/plus/discover/tv-art/{media_id}" if artwork else None,
                        "artwork": artwork,
                        "poster_url": artwork,
                        "kind": "tv",
                        "summary": item.get("overview")
                        if isinstance(item.get("overview"), str)
                        else "",
                        "year": year,
                        "score": score,
                        "score_label": "TMDB",
                        "trailer": cached_trailers.get(media_id),
                        "available": media_id in present,
                        "format": "TV show",
                        "badge": "On Plex"
                        if media_id in present
                        else "Watching"
                        if media_id in watching
                        else "",
                        "affinity": affinity,
                    }
                )
            if rank_by_taste:
                cards.sort(key=lambda card: (card["affinity"], card["score"] or 0), reverse=True)
            return cards[:_CARD_LIMIT]

        async def fetch_discover(params: dict[str, str], *, rank_by_taste: bool) -> list[dict]:
            try:
                data = await svc._json("seerr", "/api/v1/discover/tv", params=params)
            except MovieError:
                return []
            results = data.get("results")
            return (
                project_tv_list(results, rank_by_taste=rank_by_taste)
                if isinstance(results, list)
                else []
            )

        async def fetch_because() -> tuple[str, list[dict]]:
            seed = _seed_tv(db, user_id)
            if seed is None or not str(seed["title"] or "").strip():
                return "", []
            try:
                data = await svc._json(
                    "seerr",
                    f"/api/v1/tv/{int(seed['tmdb_id'])}/recommendations",
                    params={"page": "1"},
                )
            except MovieError:
                return "", []
            results = data.get("results")
            if not isinstance(results, list):
                return "", []
            return str(seed["title"]), project_tv_list(results, rank_by_taste=False)

        trending_raw, acclaimed_cards, (seed_title, because_cards) = await asyncio.gather(
            fetch_discover({"page": "1"}, rank_by_taste=False),
            fetch_discover({"page": "1", "sortBy": "vote_average.desc"}, rank_by_taste=True),
            fetch_because(),
        )
        top_cards = sorted(
            trending_raw, key=lambda card: (card["affinity"], card["score"] or 0), reverse=True
        )
        shelves: list[dict] = []
        if top_cards:
            shelves.append(_shelf("top", "Top Picks for You", top_cards, kind="tv"))
        if len(because_cards) >= 3:
            shelves.append(
                _shelf(
                    "because_you_liked",
                    f"Because You Liked {seed_title}",
                    because_cards,
                    kind="tv",
                )
            )
        if acclaimed_cards:
            shelves.append(_shelf("acclaimed", "Critically Acclaimed", acclaimed_cards, kind="tv"))
        if trending_raw:
            shelves.append(_shelf("trending", "Trending & On Air", trending_raw, kind="tv"))
        layout = for_you_layout.load_layout(db, user_id, "tv")["rows"]
        return for_you_layout.order_shelves("tv", layout, shelves)

    if kind == "movies":
        return await movies()
    return await television()


async def screen_shelves(db, user_id, *, kind="screen"):
    signature = _screen_signature(db, user_id)
    if kind == "screen":
        movies, tv = await asyncio.gather(
            _cached_screen_shelf(db, user_id, signature, "movies"),
            _cached_screen_shelf(db, user_id, signature, "tv"),
        )
        return movies + tv
    return await _cached_screen_shelf(db, user_id, signature, kind)


async def _cached_screen_shelf(db, user_id, signature, kind):
    key = (user_id, kind)
    cached = _screen_rows.get(key)
    now = time.monotonic()
    if cached and cached[0] > now and cached[1] == signature:
        return cached[2]
    if key in _screen_tasks:
        return await asyncio.shield(_screen_tasks[key])
    if len(_screen_tasks) >= 4:
        return cached[2] if cached else []
    # The route owns its DB connection until this task completes.
    task = asyncio.create_task(_screen_shelves(db, user_id, signature, kind))
    _screen_tasks[key] = task
    try:
        rows = await task
    finally:
        _screen_tasks.pop(key, None)
    _screen_rows[key] = (now + (600 if rows else 60), signature, rows)
    _screen_rows.move_to_end(key)
    if len(_screen_rows) > 64:
        _screen_rows.popitem(last=False)
    return rows


def television_artwork(user_id, media_id):
    cached = _screen_rows.get((user_id, "tv"))
    if not cached:
        return None
    return next(
        (
            card["artwork"]
            for row in cached[2]
            for card in row["cards"]
            if card.get("id") == media_id and card.get("artwork")
        ),
        None,
    )
