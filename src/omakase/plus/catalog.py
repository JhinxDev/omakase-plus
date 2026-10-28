"""Cross-alias, multi-catalog search with deterministic quality ranking."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Protocol

from omakase.plus.acquisition_policy import AcquisitionPolicy
from omakase.plus.btih import torrent_hash_from_magnet as _torrent_hash_from_magnet
from omakase.plus.coverage import (
    BLEACH_COUR4_ANILIST_ID,
    SLIME_S4_ANILIST_ID,
    parse_episode_coverage,
)
from omakase.plus.identity import identity_rejection_reason
from omakase.plus.media_types import MediaCandidate
from omakase.plus.nyaa import NyaaTorrent, search

_QUALITY_PATTERNS = (
    ("2160p", re.compile(r"\b(?:2160p|4k|uhd)\b", re.IGNORECASE)),
    ("1080p", re.compile(r"\b1080p\b", re.IGNORECASE)),
    ("720p", re.compile(r"\b720p\b", re.IGNORECASE)),
)
_LOW_RESOLUTION_RE = re.compile(r"\b(?:240|360|480|576)p\b", re.IGNORECASE)
_RAW_RE = re.compile(r"\braws?\b", re.IGNORECASE)
_SUBTITLE_SIGNAL_RE = re.compile(
    r"\b(?:multi[\s._-]*subs?|english[\s._-]*subs?|eng[\s._-]*subs?|"
    r"subtitles?)\b",
    re.IGNORECASE,
)
_HARDSUB_RE = re.compile(r"\bhard[\s._-]*subs?\b", re.IGNORECASE)
_NO_SUBS_RE = re.compile(r"\bno[\s._-]*subs?(?:titles?)?\b", re.IGNORECASE)
_CAM_RE = re.compile(
    r"\b(?:hdcam|camrip|cam|hdts|telesync|telecine|dvdscr|screener)\b",
    re.IGNORECASE,
)
_EXPLICIT_SEASON_RE = re.compile(
    r"\b(?:SEASON[\s._-]*|S)(?P<season>\d{1,2})(?:\b|E)", re.IGNORECASE
)
_KNOWN_GROUPS = ("subsplease", "judas", "ember", "mtbb", "asenshi", "commie", "deanzel")
# Prowlarr searches every enabled indexer and returns broad synonym matches. Two canonical
# aliases retain that breadth without multiplying proxy-link resolution for every AniList name.
_MAX_ADDITIONAL_CATALOG_ALIASES = 2


class CatalogSource(Protocol):
    """One optional catalog searched alongside the built-in Nyaa feed."""

    kind: str

    async def search(self, query: str) -> Sequence[NyaaTorrent]:
        """Return a bounded torrent result window for one search alias."""


@dataclass(frozen=True, slots=True)
class AliasCatalogWindow:
    """One bounded provider-completeness window for a normalized alias."""

    alias: str
    candidates: tuple[MediaCandidate, ...]
    provider_complete: bool
    error_code: str = ""
    source_receipts: tuple[CatalogSearchReceipt, ...] = ()


@dataclass(frozen=True, slots=True)
class CatalogSearchReceipt:
    """One secret-free catalog outcome for one search alias."""

    catalog_kind: str
    status: str
    candidate_count: int
    error_code: str = ""


def torrent_hash_from_magnet(magnet: str) -> str | None:
    """Compatibility wrapper around the canonical BTIH parser."""
    return _torrent_hash_from_magnet(magnet)


def quality_from_title(title: str) -> str:
    for quality, pattern in _QUALITY_PATTERNS:
        if pattern.search(title):
            return quality
    return ""


def is_hardsub_release(title: str) -> bool:
    """Return whether the release advertises subtitles burned into the video."""
    return bool(_HARDSUB_RE.search(title or ""))


def _seed_score(seeders: int) -> int:
    for minimum, score in ((500, 70), (100, 60), (50, 50), (20, 40), (10, 30), (5, 20), (1, 10)):
        if seeders >= minimum:
            return score
    return 0


def _release_score(
    torrent: NyaaTorrent,
    coverage: frozenset[int],
    policy: AcquisitionPolicy | None = None,
) -> int:
    policy = policy or AcquisitionPolicy()
    title = torrent.title.casefold()
    score = len(coverage) * 10 + _seed_score(torrent.seeders)
    if torrent.seeders >= policy.preferred_seeders:
        score += 20
    if policy.prefer_batches and torrent.is_batch:
        score += 50
    elif not policy.prefer_batches and not torrent.is_batch:
        score += 50
    if policy.prefer_trusted and torrent.is_trusted:
        score += 40
    if policy.prefer_trusted and any(
        re.search(rf"(?:^|[\[\s._-]){re.escape(group)}(?:$|[\]\s._-])", title)
        for group in _KNOWN_GROUPS
    ):
        score += 40
    if re.search(r"\b(?:hevc|x265|10[\s._-]?bit)\b", title):
        score += 25
    if re.search(r"\b(?:dual[\s._-]?audio|multi[\s._-]?subs?|english[\s._-]?subs?)\b", title):
        score += 30
    return score


def candidate_from_torrent(
    torrent: NyaaTorrent,
    *,
    search_alias: str,
    season: int,
    expected: int | None,
    anilist_id: int | None = None,
    catalog_kind: str = "nyaa",
    policy: AcquisitionPolicy | None = None,
) -> MediaCandidate | None:
    policy = policy or AcquisitionPolicy()
    torrent_hash = torrent_hash_from_magnet(torrent.magnet)
    if torrent_hash is None:
        return None
    parsed = parse_episode_coverage(
        torrent.title,
        season,
        expected=expected,
        anilist_id=anilist_id,
    )
    return MediaCandidate(
        torrent_hash=torrent_hash,
        source_title=torrent.title,
        magnet=torrent.magnet,
        seeders=max(0, torrent.seeders),
        size_bytes=max(0, torrent.size_bytes),
        quality=quality_from_title(torrent.title),
        score=_release_score(torrent, parsed.episodes, policy),
        coverage=parsed.episodes,
        search_alias=search_alias,
        is_trusted=torrent.is_trusted,
        is_batch=torrent.is_batch,
        catalog_kind=catalog_kind,
    )


def candidate_rejection_reason(
    candidate: MediaCandidate,
    *,
    season: int,
    target_title: str = "",
    target_aliases: Sequence[str] = (),
    anilist_id: int | None = None,
    media_format: str = "TV",
    allow_hardsub: bool = False,
    policy: AcquisitionPolicy | None = None,
    enforce_seeders: bool = False,
) -> str:
    policy = policy or AcquisitionPolicy()
    title = candidate.source_title
    if _RAW_RE.search(title) and not _SUBTITLE_SIGNAL_RE.search(title):
        return "RAW_RELEASE"
    if is_hardsub_release(title) and not allow_hardsub:
        return "HARDSUB_RELEASE"
    if _LOW_RESOLUTION_RE.search(title):
        return "LOW_RESOLUTION"
    if _NO_SUBS_RE.search(title):
        return "NO_SUBTITLES"
    if _CAM_RE.search(title):
        return "CAM_SOURCE"
    if target_title:
        identity_reason = identity_rejection_reason(
            title,
            target_title=target_title,
            target_aliases=target_aliases,
            season=season,
            anilist_id=anilist_id,
            media_format=media_format,
        )
        if identity_reason:
            return identity_reason
    if not target_title:
        season_matches = {
            int(match.group("season")) for match in _EXPLICIT_SEASON_RE.finditer(title)
        }
        if season_matches and season_matches != {season}:
            return "WRONG_SEASON"
    if candidate.quality not in policy.allowed_resolutions:
        return "UNSUPPORTED_RESOLUTION"
    if enforce_seeders and candidate.seeders < policy.minimum_seeders:
        return "LOW_AVAILABILITY"
    if candidate.size_bytes > policy.max_release_size_bytes:
        return "RELEASE_TOO_LARGE"
    if not candidate.coverage:
        return "NO_EPISODE_COVERAGE"
    return ""


def is_eligible(
    candidate: MediaCandidate,
    *,
    season: int,
    target_title: str = "",
    target_aliases: Sequence[str] = (),
    anilist_id: int | None = None,
    media_format: str = "TV",
    allow_hardsub: bool = False,
    policy: AcquisitionPolicy | None = None,
    enforce_seeders: bool = False,
) -> bool:
    return not candidate_rejection_reason(
        candidate,
        season=season,
        target_title=target_title,
        target_aliases=target_aliases,
        anilist_id=anilist_id,
        media_format=media_format,
        allow_hardsub=allow_hardsub,
        policy=policy,
        enforce_seeders=enforce_seeders,
    )


def _quality_tier(candidate: MediaCandidate, policy: AcquisitionPolicy) -> int:
    try:
        index = policy.allowed_resolutions.index(candidate.quality)
    except ValueError:
        return 0
    return len(policy.allowed_resolutions) - index


def _preference_key(
    candidate: MediaCandidate,
    policy: AcquisitionPolicy,
) -> tuple[int, int, int, int]:
    return (
        _quality_tier(candidate, policy),
        candidate.score,
        candidate.seeders,
        -candidate.size_bytes,
    )


def rank_candidates(
    candidates: Sequence[MediaCandidate],
    *,
    policy: AcquisitionPolicy | None = None,
) -> tuple[MediaCandidate, ...]:
    policy = policy or AcquisitionPolicy()
    return tuple(
        sorted(
            candidates,
            key=lambda candidate: (
                -_quality_tier(candidate, policy),
                -candidate.score,
                -candidate.seeders,
                candidate.size_bytes,
                candidate.torrent_hash,
            ),
        )
    )


async def search_alias_windows(
    aliases: Sequence[str],
    *,
    season: int,
    expected: int | None,
    anilist_id: int | None = None,
    heartbeat: Callable[[], None] | None = None,
    additional_catalogs: Sequence[CatalogSource] = (),
    policy: AcquisitionPolicy | None = None,
    missing_episodes: Sequence[int] = (),
    media_format: str = "TV",
    metadata_excluded_hashes: frozenset[str] = frozenset(),
    _metadata_urls: dict[str, str] | None = None,
) -> tuple[AliasCatalogWindow, ...]:
    """Search every alias across configured catalogs and retain completeness evidence."""
    metadata_urls = {} if _metadata_urls is None else _metadata_urls
    windows: list[AliasCatalogWindow] = []
    seen_aliases: set[str] = set()

    additional_catalog_aliases_searched = 0
    for raw_alias in aliases:
        alias = raw_alias.strip()
        normalized_alias = " ".join(alias.casefold().split())
        if not alias or normalized_alias in seen_aliases:
            continue
        seen_aliases.add(normalized_alias)

        source_results: list[tuple[str, Sequence[NyaaTorrent]]] = []
        source_receipts: list[CatalogSearchReceipt] = []
        errors: list[str] = []
        try:
            if heartbeat is not None:
                heartbeat()
            torrents = await search(alias)
            if heartbeat is not None:
                heartbeat()
        except Exception:
            torrents = []
            errors.append("NYAA_ERROR")
        source_results.append(("nyaa", torrents))
        source_receipts.append(
            CatalogSearchReceipt(
                catalog_kind="nyaa",
                status="error" if "NYAA_ERROR" in errors else "healthy",
                candidate_count=len(torrents),
                error_code="NYAA_ERROR" if "NYAA_ERROR" in errors else "",
            )
        )

        search_additional_catalogs = (
            additional_catalog_aliases_searched < _MAX_ADDITIONAL_CATALOG_ALIASES
        )
        if search_additional_catalogs:
            additional_catalog_aliases_searched += 1
        for source in additional_catalogs if search_additional_catalogs else ():
            kind = _catalog_kind(source.kind)
            source_error = ""
            try:
                if heartbeat is not None:
                    heartbeat()
                source_torrents = await source.search(alias)
                if heartbeat is not None:
                    heartbeat()
            except Exception:
                source_torrents = ()
                source_error = f"{kind.upper()}_ERROR"
                errors.append(source_error)
            source_results.append((kind, source_torrents))
            source_receipts.append(
                CatalogSearchReceipt(
                    catalog_kind=kind,
                    status="error" if source_error else "healthy",
                    candidate_count=len(source_torrents),
                    error_code=source_error,
                )
            )

        candidates: list[MediaCandidate] = []
        for catalog_kind, catalog_torrents in source_results:
            for torrent in catalog_torrents:
                candidate = candidate_from_torrent(
                    torrent,
                    search_alias=alias,
                    season=season,
                    expected=expected,
                    anilist_id=anilist_id,
                    catalog_kind=catalog_kind,
                    policy=policy,
                )
                if candidate is None:
                    continue
                if catalog_kind == "nyaa" and torrent.torrent_url:
                    metadata_urls.setdefault(candidate.torrent_hash, torrent.torrent_url)
                candidates.append(candidate)
        windows.append(
            AliasCatalogWindow(
                alias=alias,
                candidates=_deduplicate_candidates(candidates, policy=policy),
                provider_complete=not errors,
                error_code=",".join(errors),
                source_receipts=tuple(source_receipts),
            )
        )

    # RSS/catalog windows are bounded. A title-wide window can omit older gaps.
    # Keep query variants attached to their canonical alias so a narrow search
    # never masquerades as independent full-alias completeness evidence.
    gaps = sorted(
        {
            episode
            for episode in missing_episodes
            if type(episode) is int and 0 < episode <= (expected or 1000)
        }
    )
    boundary_gaps = tuple(dict.fromkeys((gaps[0], gaps[-1]))) if gaps else ()
    offset = 0
    if anilist_id == BLEACH_COUR4_ANILIST_ID and season in {2, 4}:
        offset = 40
        boundary_gaps = tuple(episode for episode in boundary_gaps if episode <= 10)
    elif anilist_id == SLIME_S4_ANILIST_ID and season == 4:
        offset = 72
        boundary_gaps = tuple(episode for episode in boundary_gaps if episode <= 24)
    for index, window in enumerate(windows[:2]):
        extra_windows: list[AliasCatalogWindow] = []
        gap_alias = (
            "Bleach Sennen Kessen Hen"
            if index == 0 and anilist_id == BLEACH_COUR4_ANILIST_ID and season in {2, 4}
            else window.alias
        )
        for episode in boundary_gaps:
            # Four extra Nyaa queries at most; optional catalogs receive only
            # two extras. No new title aliases or fuzzy identity rules.
            extra_windows.extend(
                await search_alias_windows(
                    [f"{gap_alias} {episode + offset:02d}"],
                    season=season,
                    expected=expected,
                    anilist_id=anilist_id,
                    heartbeat=heartbeat,
                    additional_catalogs=additional_catalogs if index == 0 else (),
                    policy=policy,
                    media_format=media_format,
                    _metadata_urls=metadata_urls,
                )
            )
        if extra_windows:
            candidates = [*window.candidates]
            for extra in extra_windows:
                candidates.extend(
                    replace(candidate, search_alias=window.alias) for candidate in extra.candidates
                )
            all_windows = (window, *extra_windows)
            windows[index] = AliasCatalogWindow(
                alias=window.alias,
                candidates=_deduplicate_candidates(candidates, policy=policy),
                provider_complete=all(item.provider_complete for item in all_windows),
                error_code=",".join(
                    dict.fromkeys(item.error_code for item in all_windows if item.error_code)
                ),
                source_receipts=tuple(
                    receipt for item in all_windows for receipt in item.source_receipts
                ),
            )
    if gaps and _metadata_urls is None:
        from omakase.plus.torrent_metadata import download_url_allowed, fetch_planned_coverage

        enriched = {}
        attempted = 0
        for candidate in merge_alias_windows(windows, policy=policy):
            if attempted >= 2:
                break
            if (
                candidate.catalog_kind != "nyaa"
                or candidate.coverage
                or candidate.torrent_hash in metadata_excluded_hashes
            ):
                continue
            if (
                candidate_rejection_reason(
                    candidate,
                    season=season,
                    target_title=aliases[0],
                    target_aliases=aliases[1:],
                    anilist_id=anilist_id,
                    media_format=media_format,
                    policy=policy,
                )
                != "NO_EPISODE_COVERAGE"
            ):
                continue
            url = metadata_urls.get(candidate.torrent_hash)
            if not url or not download_url_allowed(url):
                continue
            attempted += 1
            if heartbeat is not None:
                heartbeat()
            coverage = await fetch_planned_coverage(
                url,
                torrent_hash=candidate.torrent_hash,
                target_title=aliases[0],
                target_aliases=tuple(aliases[1:]),
                season=season,
                expected=expected,
                anilist_id=anilist_id,
                media_format=media_format,
            )
            if heartbeat is not None:
                heartbeat()
            if coverage & set(gaps):
                enriched[candidate.torrent_hash] = coverage
        windows = [
            replace(
                window,
                candidates=tuple(
                    replace(
                        candidate, coverage=enriched[candidate.torrent_hash], metadata_backed=True
                    )
                    if candidate.torrent_hash in enriched
                    else candidate
                    for candidate in window.candidates
                ),
            )
            for window in windows
        ]
    return tuple(windows)


def _catalog_kind(value: object) -> str:
    normalized = re.sub(r"[^a-z0-9_-]+", "-", str(value or "").strip().casefold()).strip("-")
    if not normalized or len(normalized) > 100:
        raise ValueError("catalog kind is invalid")
    return normalized


def _deduplicate_candidates(
    candidates: Sequence[MediaCandidate],
    *,
    policy: AcquisitionPolicy | None = None,
) -> tuple[MediaCandidate, ...]:
    policy = policy or AcquisitionPolicy()
    deduplicated: dict[str, MediaCandidate] = {}
    for candidate in candidates:
        existing = deduplicated.get(candidate.torrent_hash)
        if existing is None or _preference_key(candidate, policy) > _preference_key(
            existing,
            policy,
        ):
            deduplicated[candidate.torrent_hash] = candidate
    return rank_candidates(tuple(deduplicated.values()), policy=policy)


def merge_alias_windows(
    windows: Sequence[AliasCatalogWindow],
    *,
    policy: AcquisitionPolicy | None = None,
) -> tuple[MediaCandidate, ...]:
    """Globally deduplicate and rank candidates from complete or partial alias windows."""
    return _deduplicate_candidates(
        tuple(candidate for window in windows for candidate in window.candidates),
        policy=policy,
    )


async def search_all_aliases(
    aliases: Sequence[str],
    *,
    season: int,
    expected: int | None,
    anilist_id: int | None = None,
    heartbeat: Callable[[], None] | None = None,
    additional_catalogs: Sequence[CatalogSource] = (),
    policy: AcquisitionPolicy | None = None,
) -> tuple[MediaCandidate, ...]:
    """Search every unique alias and globally rank hash-deduplicated results."""
    windows = await search_alias_windows(
        aliases,
        season=season,
        expected=expected,
        anilist_id=anilist_id,
        heartbeat=heartbeat,
        additional_catalogs=additional_catalogs,
        policy=policy,
    )
    return merge_alias_windows(windows, policy=policy)
