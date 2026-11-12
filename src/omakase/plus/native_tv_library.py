"""Read-only personal and shared catalogs for the Apple TV app."""

from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from dataclasses import replace
from urllib.parse import urlsplit

from omakase.plus import connections, household_views, households
from omakase.plus.anime_synopsis import cached_synopsis
from omakase.plus.debrid_anime import DebridTitleSource, anime_episodes
from omakase.plus.debrid_catalog import catalog_title as debrid_catalog_title
from omakase.plus.debrid_library import account_token, playback_provider
from omakase.plus.history_artwork import (
    EpisodeArtworkUnavailable,
    anime_series_identity,
    tv_identity,
)
from omakase.plus.household_media import binding, matching_item, personal_legacy_visible
from omakase.plus.movies import MovieConnections, MovieError, MovieService
from omakase.plus.product_views import library_titles
from omakase.plus.tv_service import episode_runtime, plain
from omakase.plus.web_playback_catalog import catalog_target
from omakase.plus.web_playback_navigation import attach_navigation, exact_next_url
from omakase.plus.web_playback_source import PlaybackSourceError

PAGE_SIZE = 30


def _entry(key, title, *, note="", watch_path=None, **presentation):
    return {
        "id": str(key),
        "title": str(title)[:300],
        "note": str(note)[:300],
        "watch_path": watch_path,
        **presentation,
    }


def _tmdb_image(value):
    if isinstance(value, str) and re.fullmatch(
        r"https://image\.tmdb\.org/t/p/(?:w[0-9]{2,4}|original)/[A-Za-z0-9_-]{1,96}\.(?:jpg|png|webp)",
        value,
    ):
        return value
    return None


def _anime(db, scope, *, debrid_torrents=None):
    cards = library_titles(db, user_id=scope.viewer_id, lifecycle="all", library_scope=scope)
    mode = playback_provider(db, scope.viewer_id) if scope.kind == "personal" else "plex"
    if mode != "plex":
        has_debrid = bool(account_token(db, scope.viewer_id))
        for card in cards:
            if mode == "realdebrid":
                card.update(play_url=None, play_episode=None, episode_play_urls={})
            if card.get("state") == "archived":
                continue
            monitor_id = card["id"]
            episodes = (
                anime_episodes(db, scope, monitor_id, torrents=debrid_torrents)
                if has_debrid
                else set()
            )
            if episodes:
                paths = card.setdefault("episode_play_urls", {})
                for episode in episodes:
                    paths.setdefault(episode, f"/plus/watch/{monitor_id}/{episode}")
                preferred = 0 if 0 in episodes else card.get("next_episode") or 1
                if preferred in episodes and not card.get("play_url"):
                    card.update(
                        play_url=paths[preferred], play_episode=preferred or None, play_label="Play"
                    )
    return cards


def _anime_monitor_id(db, scope, title_id):
    if scope.kind == "personal":
        return str(title_id)
    media = binding(db, scope, item_id=title_id)
    return str(media.legacy_id) if media.domain == "anime" and media.legacy_id else None


def playback_monitor(db, scope, monitor_id):
    if scope.kind == "personal":
        return db.execute(
            "SELECT * FROM media_monitors WHERE user_id=? AND id=?", (scope.viewer_id, monitor_id)
        ).fetchone()
    return db.execute(
        """SELECT m.* FROM media_monitors m
           JOIN library_resources r ON r.owner_user_id=m.user_id AND r.domain='anime'
             AND r.legacy_id=CAST(m.id AS TEXT) AND r.canonical_id=CAST(m.anilist_id AS TEXT)
             AND r.season_number=COALESCE(m.season_number,0)
           JOIN library_items i ON i.resource_id=r.id AND i.domain=r.domain
             AND i.canonical_id=r.canonical_id AND i.season_number=r.season_number
           WHERE i.library_id=? AND i.state='active' AND m.user_id=? AND m.id=?""",
        (scope.library_id, scope.operational_owner_id, monitor_id),
    ).fetchone()


def _shared_cards(db, scope, kind, offset, query):
    page = household_views.collection(
        db, scope, domain=kind, query=query, offset=offset, limit=PAGE_SIZE
    )
    if kind == "anime":
        cards = [household_views.anime_card(card) for card in page["cards"]]
        attach_navigation(db, cards, user_id=scope.viewer_id, library_scope=scope)
        entries = _anime_entries(db, scope, cards)
        response = {
            "items": entries,
            "next_offset": offset + PAGE_SIZE if page["has_more"] else None,
        }
        return _feature(db, scope, response, entries, kind, offset, query)
    entries = []
    for card in page["cards"]:
        key = card["canonical_id"]
        metadata = card["metadata"]
        has_source = card["facts"]["label"] == "Previously indexed in Plex"
        entries.append(
            _entry(
                key,
                card["title"],
                note=metadata.get("year") or "",
                watch_path=f"/plus/watch/title/movie/{key}"
                if kind == "movie" and has_source
                else None,
                poster_url=_tmdb_image(metadata.get("poster_url")),
                backdrop_url=_tmdb_image(metadata.get("backdrop_url")),
                summary=(metadata.get("overview") or "")[:650],
                watching=(card["history"] or {}).get("status") == "watching",
            )
        )
    response = {"items": entries, "next_offset": offset + PAGE_SIZE if page["has_more"] else None}
    return _feature(db, scope, response, entries, kind, offset, query)


def _anime_entries(db, scope, cards):
    return [
        _entry(
            row["id"],
            row["title"],
            note=row["lifecycle_label"],
            artwork_id=_anime_monitor_id(db, scope, row["id"]),
            progress=row["progress_percent"] / 100,
            watching=row["lifecycle"] == "in_progress",
            featured_path=urlsplit(row["play_url"]).path if row.get("play_url") else None,
            featured_episode=row.get("play_episode"),
        )
        for row in cards
    ]


async def personal_plex_movies(db, scope, *, transport=None):
    """Read current Movies membership without writing catalog or watch history."""
    if scope.kind != "personal" or scope.viewer_id != scope.operational_owner_id:
        return ()
    owner = scope.viewer_id
    saved = connections.movie_connections(db, owner)
    if not (saved.plex_url and saved.plex_token and saved.plex_machine_id):
        return ()
    service = MovieService(
        db,
        MovieConnections(
            plex_url=saved.plex_url,
            plex_token=saved.plex_token,
            plex_machine_id=saved.plex_machine_id,
            plex_movies_section_id=saved.plex_movies_section_id,
        ),
        transport=transport,
    )
    try:
        async with asyncio.timeout(5):
            server = await service._xml("/identity")
            if server.get("machineIdentifier") != saved.plex_machine_id:
                return ()
            section, _ = await service._sections()
            videos = await service._videos(section, require_complete=True)
    except (MovieError, TimeoutError):
        return ()
    households.revalidate_scope(db, scope)
    current = connections.movie_connections(db, owner)
    if (
        current.plex_url,
        current.plex_token,
        current.plex_machine_id,
        current.plex_movies_section_id,
    ) != (
        saved.plex_url,
        saved.plex_token,
        saved.plex_machine_id,
        saved.plex_movies_section_id,
    ):
        return ()
    known = dict(
        db.execute(
            "SELECT imdb_id, tmdb_id FROM movie_catalog WHERE user_id=? AND identity_verified=1",
            (owner,),
        ).fetchall()
    )
    counts = Counter()
    exact = []
    for video in videos:
        imdbs = service._guids(video, "imdb")
        counts.update(imdbs)
        if len(imdbs) != 1:
            continue
        imdb_id = next(iter(imdbs))
        if (
            known.get(imdb_id)
            and service._guids(video, "tmdb") == {str(known[imdb_id])}
            and video.get("type") == "movie"
            and video.get("librarySectionID") == str(section)
            and video.get("librarySectionTitle") == "Movies"
        ):
            exact.append(imdb_id)
    return tuple(imdb_id for imdb_id in exact if counts[imdb_id] == 1)


def collection(
    db,
    scope,
    kind,
    offset,
    query="",
    *,
    plex_movies=(),
    debrid_movies=(),
    debrid_only=False,
    debrid_torrents=None,
):
    owner = scope.viewer_id
    if scope.kind == "household":
        return _shared_cards(db, scope, kind, offset, query)
    if kind == "anime":
        entries = _anime_entries(db, scope, _anime(db, scope, debrid_torrents=debrid_torrents))
    elif kind == "movie":
        rows = db.execute(
            """SELECT c.imdb_id,c.title,c.year,c.poster_url,c.backdrop_url,c.overview,
                      h.status AS watch_status,h.updated_at AS watch_updated_at,
                      CASE WHEN q.code='copy_removed' AND NOT EXISTS (
                        SELECT 1 FROM movie_verifications v
                        WHERE v.user_id=c.user_id AND v.imdb_id=c.imdb_id
                          AND v.state='ready' AND v.plex_key!=''
                          AND julianday(v.expires_at)>julianday('now')
                      ) THEN 1 ELSE 0 END AS copy_removed
               FROM movie_catalog c LEFT JOIN movie_history h USING(user_id,imdb_id)
               LEFT JOIN movie_requests q USING(user_id,imdb_id)
               WHERE c.user_id=? AND (
                 q.code<>'copy_removed'
                 OR (q.imdb_id IS NULL AND c.identity_verified=1
                     AND c.imdb_id IN (SELECT value FROM json_each(?)))
                 OR EXISTS (SELECT 1 FROM movie_history h
                     WHERE h.user_id=c.user_id AND h.imdb_id=c.imdb_id
                       AND (h.status_source<>'none' OR h.rating_source<>'none'))
                 OR EXISTS (SELECT 1 FROM library_items i
                     WHERE i.library_id=? AND i.domain='movie' AND i.canonical_id=c.imdb_id
                       AND i.state='active')
                 OR EXISTS (SELECT 1 FROM library_resources r JOIN movie_file_observations f
                       ON f.resource_id=r.id
                     WHERE r.owner_user_id=c.user_id AND r.domain='movie'
                       AND r.canonical_id=c.imdb_id))
               ORDER BY c.title COLLATE NOCASE,c.imdb_id""",
            (owner, json.dumps(sorted(set(plex_movies) | set(debrid_movies))), scope.library_id),
        )
        entries = [
            _entry(
                row["imdb_id"],
                row["title"],
                note=row["year"] or "",
                watch_path=f"/plus/watch/title/movie/{row['imdb_id']}"
                if not row["copy_removed"] and (not debrid_only or row["imdb_id"] in debrid_movies)
                else None,
                poster_url=_tmdb_image(row["poster_url"]),
                backdrop_url=_tmdb_image(row["backdrop_url"]),
                summary=(row["overview"] or "")[:650],
                watching=row["watch_status"] == "watching"
                and not row["copy_removed"]
                and (not debrid_only or row["imdb_id"] in debrid_movies),
            )
            for row in rows
            if personal_legacy_visible(db, scope, domain="movie", canonical_id=row["imdb_id"])
        ]
        activity = {str(row["imdb_id"]): row["watch_updated_at"] or "" for row in rows}
        for entry in entries:
            entry["_activity"] = activity.get(str(entry["id"]), "")
    else:
        rows = db.execute(
            """SELECT c.tmdb_id,c.metadata_json,h.status AS watch_status,
                      h.updated_at AS watch_updated_at
               FROM tv_catalog c LEFT JOIN tv_history h USING(user_id,tmdb_id)
               WHERE c.user_id=? AND json_extract(c.metadata_json,'$.in_collection')=1
               ORDER BY json_extract(c.metadata_json,'$.title') COLLATE NOCASE,c.tmdb_id""",
            (owner,),
        )
        entries = []
        for row in rows:
            if personal_legacy_visible(db, scope, domain="tv", canonical_id=str(row["tmdb_id"])):
                metadata = json.loads(row["metadata_json"])
                entry = _entry(
                    row["tmdb_id"],
                    metadata["title"],
                    note=metadata.get("year") or "",
                    poster_url=_tmdb_image(metadata.get("poster_url")),
                    backdrop_url=_tmdb_image(metadata.get("backdrop_url")),
                    summary=(metadata.get("overview") or "")[:650],
                    watching=row["watch_status"] == "watching",
                )
                entry["_activity"] = row["watch_updated_at"] or ""
                entries.append(entry)
    if query:
        entries = [entry for entry in entries if query.casefold() in entry["title"].casefold()]
    return _feature(db, scope, _page(entries, offset), entries, kind, offset, query)


def _feature(db, scope, response, entries, kind, offset, query):
    if not query and offset == 0:
        continue_watching = [row for row in entries if row.get("watching")]
        continue_watching.sort(key=lambda row: row.get("_activity") or "", reverse=True)
        continue_watching = continue_watching[:8]
        for row in entries:
            row.pop("_activity", None)
        response["continue_watching"] = continue_watching
        featured = (
            next(
                (
                    row
                    for row in continue_watching
                    if row.get("featured_path") or row.get("artwork_id")
                ),
                None,
            )
            or next(
                (
                    row
                    for row in entries
                    if kind == "movie" and row.get("watch_path") and row.get("backdrop_url")
                ),
                None,
            )
            or next(
                (row for row in entries if row.get("backdrop_url") or row.get("featured_path")),
                None,
            )
        )
        if featured and kind == "anime":
            anilist = anime_artwork_id(db, scope, featured.get("artwork_id") or "")
            if anilist:
                featured = {
                    **featured,
                    "summary": cached_synopsis(db, anilist)["description_preview"],
                }
        response["featured"] = featured
    else:
        for row in entries:
            row.pop("_activity", None)
    return response


def anime_artwork_id(db, scope, monitor_id):
    if not re.fullmatch(r"[1-9][0-9]{0,17}", monitor_id):
        return None
    if scope.kind == "household":
        row = playback_monitor(db, scope, monitor_id)
        return row["anilist_id"] if row else None
    row = db.execute(
        """SELECT m.anilist_id,COALESCE(m.season_number,0) AS season
           FROM media_monitors m WHERE m.user_id=? AND m.id=?
           AND NOT EXISTS (
             SELECT 1 FROM library_items i WHERE i.library_id=? AND i.domain='anime'
             AND i.canonical_id=CAST(m.anilist_id AS TEXT)
             AND i.season_number=COALESCE(m.season_number,0) AND i.state='removed'
           )""",
        (scope.viewer_id, monitor_id, scope.library_id),
    ).fetchone()
    if row and personal_legacy_visible(
        db, scope, domain="anime", canonical_id=str(row["anilist_id"]), season=row["season"]
    ):
        return row["anilist_id"]
    return None


def episodes(
    db,
    scope,
    kind,
    key,
    offset,
    *,
    debrid_episodes=(),
    debrid_only=False,
    debrid_torrents=None,
):
    pattern = (
        r"[0-9a-f]{32}" if kind == "anime" and scope.kind == "household" else r"[1-9][0-9]{0,17}"
    )
    if not re.fullmatch(pattern, key):
        raise ValueError("Invalid title")
    title_summary = ""
    if kind == "anime":
        if scope.kind == "household":
            item = household_views.detail(db, scope, item_id=key)
            if item["domain"] != "anime":
                raise ValueError("Invalid title")
            row = household_views.anime_card(item)
            attach_navigation(db, [row], user_id=scope.viewer_id, library_scope=scope)
        else:
            row = next(
                (
                    row
                    for row in _anime(db, scope, debrid_torrents=debrid_torrents)
                    if str(row["id"]) == key
                ),
                None,
            )
        if row is None:
            raise ValueError("Invalid title")
        monitor = _anime_monitor_id(db, scope, key)
        paths = row.get("episode_play_urls", {})
        if not paths and row.get("play_url") and urlsplit(row["play_url"]).path.endswith("/0"):
            paths = {0: row["play_url"]}
        entries = [
            _entry(
                episode,
                row["title"] if episode == 0 else f"Episode {episode}",
                note=f"S{row['season_number']} E{episode}" if episode else "Movie",
                watch_path=urlsplit(path).path,
                watched=bool(episode and episode <= row.get("progress_episodes", 0)),
                progress=(row.get("episode_resume") or {}).get("percent", 0) / 100
                if row.get("play_label") == "Resume" and row.get("play_episode") == episode
                else None,
                episode_artwork=f"episode-artwork/anime/{monitor}/{row['season_number']}/{episode}"
                if monitor and episode and row["season_number"]
                else None,
            )
            for episode, path in sorted(paths.items())
        ]
        if anilist := row.get("anilist_id"):
            title_summary = cached_synopsis(db, anilist)["description"]
    else:
        owner = _tv_catalog_owner(db, scope, key)
        if owner is None:
            return _page([], offset)
        rows = db.execute(
            """SELECT e.season_number,e.episode_number,e.metadata_json,e.plex_present,v.watched,v.watch_source
               FROM tv_episodes e JOIN tv_catalog c USING(user_id,tmdb_id)
               LEFT JOIN tv_episodes v ON v.user_id=? AND v.tmdb_id=e.tmdb_id
                   AND v.season_number=e.season_number AND v.episode_number=e.episode_number
               WHERE e.user_id=? AND e.tmdb_id=?
                   AND (? OR json_extract(c.metadata_json,'$.in_collection')=1)
               ORDER BY e.season_number,e.episode_number""",
            (scope.viewer_id, owner, key, scope.kind == "household"),
        )
        entries = []
        for row in rows:
            season, episode = row["season_number"], row["episode_number"]
            metadata = json.loads(row["metadata_json"])
            try:
                summary = plain(metadata.get("overview"))[:650]
            except ValueError:
                summary = ""
            entries.append(
                _entry(
                    f"{season}-{episode}",
                    metadata.get("title") or metadata.get("name") or f"Episode {episode}",
                    note=f"S{season} E{episode}",
                    watch_path=f"/plus/watch/title/tv/{key}/{season}/{episode}"
                    if (season, episode) in debrid_episodes
                    or (row["plex_present"] and not debrid_only)
                    else None,
                    watched=bool(row["watched"] and row["watch_source"] in {"manual", "plex"}),
                    summary=summary,
                    duration_seconds=episode_runtime(metadata.get("runtime_seconds")),
                    episode_artwork=f"episode-artwork/tv/{key}/{season}/{episode}",
                )
            )
    response = _page(entries, offset)
    if title_summary:
        response["title_summary"] = title_summary
    return response


def _tv_catalog_owner(db, scope, key):
    if scope.kind == "personal":
        if personal_legacy_visible(db, scope, domain="tv", canonical_id=key):
            return scope.viewer_id
    else:
        item = matching_item(db, scope, domain="tv", canonical_id=key)
        if item:
            media = binding(db, scope, item_id=item)
            return media.operational_owner_id if media.legacy_id else None
    raise ValueError("Invalid title")


def episode_image_identity(db, scope, kind, key, season, episode):
    if kind == "anime":
        if not 1 <= episode <= 5000 or not anime_artwork_id(db, scope, str(key)):
            raise EpisodeArtworkUnavailable()
        identity = anime_series_identity(db, scope.operational_owner_id, key, episode)
        if identity.season != season:
            raise EpisodeArtworkUnavailable()
        return replace(identity, user_id=scope.viewer_id)
    if not 1 <= key <= 2_147_483_647:
        raise EpisodeArtworkUnavailable()
    try:
        owner = _tv_catalog_owner(db, scope, str(key))
    except ValueError:
        raise EpisodeArtworkUnavailable() from None
    if owner is None or (
        scope.kind == "personal"
        and not db.execute(
            "SELECT 1 FROM tv_catalog WHERE user_id=? AND tmdb_id=? "
            "AND json_extract(metadata_json,'$.in_collection')=1",
            (scope.viewer_id, key),
        ).fetchone()
    ):
        raise EpisodeArtworkUnavailable()
    identity = tv_identity(db, owner, key, season, episode, detail=True)
    return replace(identity, user_id=scope.viewer_id)


def next_watch_path(db, source, *, anime_monitor_id):
    """Return the same-season successor only when its exact catalog evidence exists."""
    episode = source.episode_number + 1
    if anime_monitor_id is not None:
        if not 2 <= episode <= 5000:
            return None
        if isinstance(source, DebridTitleSource):
            return (
                f"/plus/watch/{anime_monitor_id}/{episode}"
                if episode in anime_episodes(db, source._scope, anime_monitor_id)
                else None
            )
        url = exact_next_url(db, source=source, monitor_id=anime_monitor_id)
        return urlsplit(url).path if url else None
    if source.domain != "tv" or not 2 <= episode <= 1000:
        return None
    scope = source._scope
    if isinstance(source, DebridTitleSource):
        return (
            f"/plus/watch/title/tv/{source.canonical_id}/{source.season_number}/{episode}"
            if debrid_catalog_title(
                db, scope, "tv", source.canonical_id, source.season_number, episode
            )
            else None
        )
    try:
        catalog_target(db, scope, "tv", source.canonical_id, source.season_number, episode)
    except PlaybackSourceError:
        return None
    return f"/plus/watch/title/tv/{source.canonical_id}/{source.season_number}/{episode}"


def _page(entries, offset):
    end = offset + PAGE_SIZE
    return {"items": entries[offset:end], "next_offset": end if end < len(entries) else None}
