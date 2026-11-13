"""Owner-scoped identity checks shared by movie cleanup preview and execution."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from hmac import compare_digest
from pathlib import PurePosixPath

import httpx

from omakase.plus.household_media import managed_removal_guard
from omakase.plus.movies import _imdb
from omakase.plus.movies_store import get_movie, get_submission
from omakase.plus.provider_limits import ProviderRateLimit, cooldown_hooks
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


def movie_cleanup_preference(db, user_id: int) -> bool:
    row = db.execute(
        "SELECT enabled FROM movie_cleanup_preferences WHERE user_id=?", (user_id,)
    ).fetchone()
    return bool(row and row["enabled"])


def set_movie_cleanup_preference(db, user_id: int, enabled: bool) -> None:
    db.execute(
        """INSERT INTO movie_cleanup_preferences (user_id, enabled) VALUES (?, ?)
           ON CONFLICT (user_id) DO UPDATE SET enabled=excluded.enabled""",
        (user_id, int(enabled)),
    )
    if not enabled:
        db.execute(
            """UPDATE movie_cleanup_jobs SET state='cancelled'
               WHERE user_id=? AND intent='automatic' AND state='queued'
                 AND stage NOT IN ('complete', 'blocked')""",
            (user_id,),
        )


def watched_and_rated_manually(history) -> bool:
    return bool(
        history
        and history["status"] == "completed"
        and history["status_source"] == "manual"
        and history["rating"] is not None
        and history["rating_source"] == "manual"
    )


def record_movie_cleanup_intent(db, *, user_id, imdb_id, backend_scope, before, after, now):
    """Record only an explicit new completion in the movie history transaction."""
    if not watched_and_rated_manually(after):
        db.execute(
            """UPDATE movie_cleanup_jobs SET state='cancelled'
               WHERE user_id=? AND imdb_id=? AND state='queued'
                 AND (intent='automatic' OR ?)
                 AND stage NOT IN ('complete', 'blocked')""",
            (user_id, imdb_id, not after or after["status"] != "completed"),
        )
        return
    if watched_and_rated_manually(before) or not movie_cleanup_enabled():
        return
    db.execute(
        """INSERT INTO movie_cleanup_jobs (user_id, imdb_id, cleanup_tag, backend_scope, queued_at)
           SELECT s.cleanup_owner_id, s.imdb_id, s.cleanup_tag, s.backend_scope, ?
           FROM movie_submissions s
           JOIN movie_cleanup_preferences p ON p.user_id=s.cleanup_owner_id
           WHERE s.cleanup_owner_id=? AND s.imdb_id=? AND s.backend_scope=?
             AND s.cleanup_tag IS NOT NULL AND p.enabled=1
           ON CONFLICT (user_id, imdb_id, cleanup_tag) DO UPDATE
             SET queued_at=excluded.queued_at, state='queued', attempts=0, retry_at=NULL,
                 reason=NULL, intent='automatic', selection_digest=NULL
             WHERE movie_cleanup_jobs.state='cancelled' AND movie_cleanup_jobs.stage='prepare'""",
        (now, user_id, imdb_id, backend_scope),
    )


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
    owned: bool


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
        or (
            binding.get("acquisition_tag") is not None
            and not _matches(binding["acquisition_tag"], r"[0-9a-f]{32}")
        )
    ):
        raise MovieCleanupUnavailable("riven_binding_unverified")
    return binding


def _owned_acquisition(service, user_id, movie, binding):
    tag = binding.get("acquisition_tag")
    if tag is None:
        return False
    submission = get_submission(service.db, service._scope, movie["imdb_id"])
    if (
        submission is None
        or submission["tmdb_id"] != movie["tmdb_id"]
        or submission["cleanup_owner_id"] != user_id
        or submission["cleanup_tag"] is None
        or not compare_digest(submission["cleanup_tag"], tag)
    ):
        return False
    return (
        service.db.execute(
            """SELECT 1 FROM movie_requests
           WHERE imdb_id=? AND user_id<>? AND backend_id=? AND state<>'unavailable' LIMIT 1""",
            (movie["imdb_id"], user_id, binding["riven_id"]),
        ).fetchone()
        is None
    )


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


def _torrent_matches(torrent, torrent_id, torrent_hash, file_name, file_size):
    selected = [file for file in torrent.files if file.selected] if torrent else []
    return (
        torrent is not None
        and torrent.id == torrent_id
        and torrent.torrent_hash.lower() == torrent_hash
        and torrent.status == "downloaded"
        and len(selected) == 1
        and PurePosixPath(selected[0].path).name == file_name
        and selected[0].bytes == file_size
    )


def _rd_client(service, user_id, key):
    return RealDebridClient(key, timeout=6, **cooldown_hooks(service.db, user_id))


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
        torrent = await _rd_client(service, user_id, key).get_torrent(
            binding["torrent_id"], require_definite_absence=True
        )
    except (MovieCleanupUnavailable, ProviderRateLimit):
        raise
    except (
        ValueError,
        TypeError,
        OverflowError,
        RecursionError,
        httpx.HTTPError,
    ) as error:
        raise MovieCleanupUnavailable("copy_lookup_failed") from error
    if not _torrent_matches(
        torrent, binding["torrent_id"], binding["torrent_hash"], binding["file_name"], file_size
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
        _owned_acquisition(service, user_id, movie, binding),
    )
