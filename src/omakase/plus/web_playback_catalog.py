"""Exact personal movie and TV sources for the shared playback gateway."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re

from omakase.plus import connections, households
from omakase.plus.household_media import personal_legacy_visible
from omakase.plus.language_source import digest
from omakase.plus.movies import MovieConnections, MovieService
from omakase.plus.playback_proof import _account, _get, _xml
from omakase.plus.web_playback_source import (
    PlaybackSourceError,
    _numeric,
    _physical_version,
    _require,
    _resolved_part,
)


def personal_scope(db, user_id):
    row = db.execute(
        "SELECT library_id FROM personal_libraries WHERE user_id=?", (user_id,)
    ).fetchone()
    _require(row is not None, "Your personal library is unavailable.")
    return households.resolve_scope(db, viewer_id=user_id, library_id=row[0])


def catalog_target(db, scope, domain, canonical_id, season=0, episode=0):
    households.revalidate_scope(db, scope)
    owner = scope.viewer_id
    _require(scope.kind == "personal" and scope.operational_owner_id == owner)
    _require(personal_legacy_visible(db, scope, domain=domain, canonical_id=canonical_id))
    if domain == "movie":
        row = db.execute(
            "SELECT imdb_id,tmdb_id,title,year,overview FROM movie_catalog "
            "WHERE user_id=? AND imdb_id=? AND identity_verified=1",
            (owner, canonical_id),
        ).fetchone()
        _require(row is not None, "This movie is unavailable in your collection.")
        return dict(row), None
    _require(domain == "tv")
    row = db.execute(
        "SELECT c.metadata_json,e.metadata_json AS episode_json,s.plex_key,s.source_scope "
        "FROM tv_catalog c JOIN tv_episodes e USING(user_id,tmdb_id) "
        "JOIN tv_plex_snapshots s USING(user_id,tmdb_id) "
        "WHERE c.user_id=? AND c.tmdb_id=? AND e.season_number=? AND e.episode_number=? "
        "AND e.plex_present=1 AND s.complete=1",
        (owner, canonical_id, season, episode),
    ).fetchone()
    _require(row is not None, "This episode is unavailable in your collection.")
    return json.loads(row["metadata_json"]), dict(row)


def _snapshot(db, scope, domain, canonical_id, season, episode):
    catalog, entry = catalog_target(db, scope, domain, canonical_id, season, episode)
    base, token = connections._values(db, scope.viewer_id, connections._spec("plex"))
    server = connections._read(db, scope.viewer_id, "plex_machine_id")
    _require(base and token and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", server or ""))
    if entry is not None:
        connector = hashlib.sha256((base + "\0" + token).encode()).hexdigest()
        _require(entry["source_scope"] == connector)
        plex_key = _numeric(entry["plex_key"])
    else:
        plex_key = ""
    catalog_identity = (catalog.get("imdb_id"), catalog.get("tmdb_id"), catalog.get("year"))
    return (base, digest(token), server, catalog_identity, plex_key), catalog, token


def revalidate_catalog_source(db, source):
    snapshot, _, _ = _snapshot(
        db,
        source._scope,
        source.domain,
        source.canonical_id,
        source.season_number,
        source.episode_number,
    )
    _require(snapshot == source._snapshot, "Playback access or title identity changed.")
    _account_is_personal(db, source.account_id, source.server_id, source.actor_user_id)
    return source


def _account_is_personal(db, account, server, user_id):
    _require(
        db.execute(
            "SELECT 1 FROM viewer_playback_bindings WHERE external_account_id=? "
            "AND server_id=? AND actor_user_id!=?",
            (account, server, user_id),
        ).fetchone()
        is None,
        "This Plex identity belongs to another viewer.",
    )


def _single_item(root, section, key, tag):
    rows = root.findall(tag)
    _require(len(rows) == 1 and root.get("librarySectionID") in {None, section})
    item = rows[0]
    _require(
        item.get("ratingKey") == key
        and item.get("librarySectionID", root.get("librarySectionID")) == section
    )
    return item


def _movie_identity(video, canonical_id, tmdb_id):
    _require(video.get("type") == "movie" and MovieService._guids(video, "imdb") == {canonical_id})
    tmdb_guids = MovieService._guids(video, "tmdb")
    _require(not tmdb_guids or (tmdb_id is not None and tmdb_guids == {str(tmdb_id)}))


def _show_identity(show, canonical_id, imdb_id):
    _require(show.get("type") == "show" and MovieService._guids(show, "tmdb") == {canonical_id})
    imdb_guids = MovieService._guids(show, "imdb")
    _require(not imdb_guids or imdb_guids == {imdb_id})


def _episode_identity(episode, show_key, season, number):
    return (
        episode.get("type") == "episode"
        and episode.get("grandparentRatingKey") == show_key
        and episode.get("parentIndex") == str(season)
        and episode.get("index") == str(number)
    )


async def _tv_episode(xml, section, show_key, season, number):
    matches, expected_total, seen = [], None, set()
    for offset in range(0, 1000, 40):
        root = await xml(
            f"/library/metadata/{show_key}/allLeaves?X-Plex-Container-Start={offset}"
            "&X-Plex-Container-Size=40"
        )
        rows = root.findall("Video")
        count = root.get("totalSize", "")
        _require(re.fullmatch(r"[0-9]{1,4}", count) and int(count) <= 1000)
        total = int(count)
        _require(
            (expected_total is None or total == expected_total)
            and root.get("offset", str(offset)) == str(offset)
            and len(rows) == min(40, max(0, total - offset))
            and root.get("librarySectionID") in {None, section}
        )
        expected_total = total
        for row in rows:
            key = _numeric(row.get("ratingKey", ""))
            _require(key not in seen and row.get("librarySectionID", section) == section)
            seen.add(key)
            if _episode_identity(row, show_key, season, number):
                matches.append(key)
        if offset + len(rows) >= total:
            break
    _require(len(matches) == 1, "One exact episode could not be matched.")
    video = _single_item(
        await xml(f"/library/metadata/{matches[0]}?includeMarkers=1"), section, matches[0], "Video"
    )
    _require(_episode_identity(video, show_key, season, number))
    return video


def _single_part(video):
    media = video.findall("Media")
    _require(len(media) == 1, "This title has more than one copy. Select a copy in Plex.")
    parts = media[0].findall("Part")
    _require(len(parts) == 1, "Multi-part files cannot open in this player.")
    size = parts[0].get("size", "")
    _require(re.fullmatch(r"[1-9][0-9]{0,12}", size) and 65536 <= int(size) <= 150 * 1024**3)
    return media[0], parts[0]


async def resolve_catalog_source(
    db,
    *,
    scope,
    domain,
    canonical_id,
    season,
    episode,
    media_version,
    transport=None,
):
    try:
        async with asyncio.timeout(18):
            snapshot, catalog, token = _snapshot(db, scope, domain, canonical_id, season, episode)
            base, _, server, catalog_identity, show_key = snapshot

            def current():
                _require(_snapshot(db, scope, domain, canonical_id, season, episode)[0] == snapshot)

            async def xml(path):
                result = _xml(await _get(base, path, token, transport=transport))
                current()
                return result

            account = await _account(token, transport)
            current()
            _account_is_personal(db, account, server, scope.viewer_id)
            _require((await xml("/identity")).get("machineIdentifier") == server)
            if domain == "movie":
                service = MovieService(
                    db,
                    MovieConnections(plex_url=base, plex_token=token, plex_machine_id=server),
                    transport=transport,
                )
                video, section_id = await service._plex_movie(catalog)
                current()
                section = str(section_id)
                _movie_identity(video, canonical_id, catalog["tmdb_id"])
                service._file(video, section_id, catalog, None)
            else:
                sections = [
                    row
                    for row in (await xml("/library/sections")).findall("Directory")
                    if row.get("type") == "show" and row.get("title") == "TV Shows"
                ]
                _require(len(sections) == 1)
                section = _numeric(sections[0].get("key", ""))
                show = _single_item(
                    await xml(f"/library/metadata/{show_key}?includeGuids=1"),
                    section,
                    show_key,
                    "Directory",
                )
                _show_identity(show, canonical_id, catalog.get("imdb_id"))
                video = await _tv_episode(xml, section, show_key, season, episode)

            media, part = _single_part(video)
            identity = digest([domain, canonical_id, catalog_identity, show_key, season, episode])
            source = _resolved_part(
                video,
                media,
                part,
                scope=scope,
                anilist_id=None,
                season_number=season,
                episode_number=episode,
                account=account,
                section=section,
                base=base,
                token=token,
                server=server,
                identity=identity,
                snapshot=snapshot,
                domain=domain,
                catalog_id=canonical_id,
                series_key=show_key,
            )
            _require(media_version in {None, source.media_version})
            return revalidate_catalog_source(db, source)
    except PlaybackSourceError:
        raise
    except (ValueError, KeyError, IndexError, TypeError, TimeoutError):
        raise PlaybackSourceError("The exact copy could not be opened from your library.") from None


async def revalidate_remote_catalog_source(db, source, transport=None):
    try:
        section = _numeric(source._section_id)
        key = _numeric(source.metadata_key)

        async def xml(path):
            revalidate_catalog_source(db, source)
            result = _xml(await _get(source.base_url, path, source.token, transport=transport))
            revalidate_catalog_source(db, source)
            return result

        _require((await xml("/identity")).get("machineIdentifier") == source.server_id)
        video = _single_item(
            await xml(f"/library/metadata/{key}?includeGuids=1"), section, key, "Video"
        )
        imdb_id, tmdb_id, _ = source._snapshot[3]
        if source.domain == "movie":
            _movie_identity(video, source.canonical_id, tmdb_id)
        else:
            show_key = _numeric(source._series_key)
            show = _single_item(
                await xml(f"/library/metadata/{show_key}?includeGuids=1"),
                section,
                show_key,
                "Directory",
            )
            _show_identity(show, source.canonical_id, imdb_id)
            _require(
                _episode_identity(video, show_key, source.season_number, source.episode_number)
            )

        media, part = _single_part(video)
        _require(
            part.get("key") == source.part_key
            and part.get("file") == source._file_path
            and _physical_version(
                source.source_identity, source.server_id, section, key, media, part
            )
            == source.media_version
        )
        return revalidate_catalog_source(db, source)
    except (ValueError, KeyError, IndexError, TypeError):
        raise PlaybackSourceError("Current playback access could not be confirmed.") from None
