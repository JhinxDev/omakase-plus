"""Owner-scoped identity checks shared by movie cleanup preview and execution."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

import httpx

from omakase.plus.household_media import managed_removal_guard
from omakase.plus.movies import _imdb
from omakase.plus.movies_store import get_movie
from omakase.plus.provider_limits import ProviderRateLimit
from omakase.plus.realdebrid import RealDebridClient
from omakase.plus.secrets import read_secret


class MovieCleanupUnavailable(ValueError):
    """The connected services did not establish a removable copy."""


def movie_cleanup_enabled() -> bool:
    return os.getenv("OMAKASE_PLUS_MOVIE_CLEANUP_ENABLED", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _matches(value: object, pattern: str) -> bool:
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def _basename(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 300
        and value not in {".", ".."}
        and not any(char in "/\\" or ord(char) < 32 for char in value)
    )


@dataclass(frozen=True)
class MovieCleanupProof:
    riven_id: str
    torrent_id: str
    torrent_hash: str
    file_name: str
    publication: tuple[str, str]
    plex_key: str
    plex_section: int
    file_size: int


def _owner_movie(service, user_id, imdb_id):
    service._guard(user_id)
    movie = get_movie(service.db, user_id, imdb_id)
    if movie is None or not movie["identity_verified"] or not movie["tmdb_id"]:
        raise MovieCleanupUnavailable("movie_identity_unverified")
    guard = managed_removal_guard(
        service.db, operational_owner_id=user_id, domain="movie", canonical_id=imdb_id
    )
    if guard["blocked"] or guard["has_shared_references"]:
        raise MovieCleanupUnavailable("movie_in_shared_library")
    return movie


async def _riven_binding(service, movie):
    binding = await service._json(
        "riven", "/api/v1/items/movie-removal-binding/" + movie["imdb_id"]
    )
    publication = binding.get("publication")
    if (
        binding.get("schema") != "riven-movie-removal-binding-v1"
        or binding.get("imdb_id") != movie["imdb_id"]
        or type(binding.get("tmdb_id")) is not int
        or binding["tmdb_id"] != movie["tmdb_id"]
        or not _matches(binding.get("riven_id"), r"[A-Za-z0-9_-]{1,128}")
        or not _matches(binding.get("torrent_id"), r"[A-Za-z0-9]{3,64}")
        or not _matches(binding.get("torrent_hash"), r"[0-9a-f]{40}")
        or not _basename(binding.get("file_name"))
        or not isinstance(publication, list)
        or len(publication) != 2
        or not all(_basename(part) for part in publication)
        or not publication[0].endswith(f"{{imdb-{movie['imdb_id']}}}")
    ):
        raise MovieCleanupUnavailable("riven_binding_unverified")
    return binding


async def _plex_binding(service, movie, binding):
    video, section = await service._plex_movie(movie)
    parts = video.findall(".//Part")
    key = video.get("ratingKey", "")
    if (
        video.get("type") != "movie"
        or video.get("librarySectionID") != str(section)
        or service._guids(video, "imdb") != {movie["imdb_id"]}
        or service._guids(video, "tmdb") not in (set(), {str(movie["tmdb_id"])})
        or not _matches(key, r"[1-9][0-9]{0,19}")
        or len(parts) != 1
    ):
        raise MovieCleanupUnavailable("plex_binding_unverified")
    path = PurePosixPath(parts[0].get("file", ""))
    size = parts[0].get("size", "")
    if (
        not path.is_absolute()
        or ".." in path.parts
        or list(path.parts[-2:]) != binding["publication"]
        or not _matches(size, r"[1-9][0-9]{0,18}")
    ):
        raise MovieCleanupUnavailable("plex_binding_unverified")
    prefs = await service._xml("/:/prefs")
    trash = [
        node.get("value") for node in prefs.findall("Setting") if node.get("id") == "autoEmptyTrash"
    ]
    if trash not in (["1"], ["true"]):
        raise MovieCleanupUnavailable("plex_scan_unverified")
    sessions = await service._xml("/status/sessions")
    if any(current.get("ratingKey") == key for current in sessions.findall(".//Video")):
        raise MovieCleanupUnavailable("movie_is_playing")
    return key, section, int(size)


async def verify_movie_cleanup_copy(service, user_id: int, imdb_id: str) -> MovieCleanupProof:
    """Match the user's exact provider item to Plex without changing either service."""
    imdb_id = _imdb(imdb_id)
    movie = _owner_movie(service, user_id, imdb_id)
    key = read_secret(service.db, user_id, "realdebrid_api_key")
    if not key:
        raise MovieCleanupUnavailable("real_debrid_disconnected")
    try:
        binding = await _riven_binding(service, movie)
        plex_key, section, file_size = await _plex_binding(service, movie, binding)
        torrent = await RealDebridClient(key, timeout=6).get_torrent(
            binding["torrent_id"], require_definite_absence=True
        )
    except MovieCleanupUnavailable:
        raise
    except (
        ValueError,
        TypeError,
        OverflowError,
        RecursionError,
        httpx.HTTPError,
        ProviderRateLimit,
    ) as error:
        raise MovieCleanupUnavailable("copy_lookup_failed") from error
    selected = [file for file in torrent.files if file.selected] if torrent else []
    if (
        torrent is None
        or torrent.id != binding["torrent_id"]
        or torrent.torrent_hash.lower() != binding["torrent_hash"]
        or torrent.status != "downloaded"
        or len(selected) != 1
        or PurePosixPath(selected[0].path).name != binding["file_name"]
        or selected[0].bytes != file_size
    ):
        raise MovieCleanupUnavailable("real_debrid_binding_unverified")
    return MovieCleanupProof(
        binding["riven_id"],
        binding["torrent_id"],
        binding["torrent_hash"],
        binding["file_name"],
        tuple(binding["publication"]),
        plex_key,
        section,
        file_size,
    )
