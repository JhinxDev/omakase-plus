"""Immutable contracts shared by the private media orchestrator."""

from __future__ import annotations

from dataclasses import dataclass

from omakase.plus.language_policy import normalize_language


@dataclass(frozen=True, slots=True)
class MediaTarget:
    """Resolved AniList identity and the facts needed to place and monitor it."""

    anilist_id: int
    title: str
    search_titles: tuple[str, ...]
    format: str
    status: str
    episodes: int | None
    aired_episodes: int
    season_number: int | None
    season: str
    season_year: int | None
    genres: tuple[str, ...]
    next_airing_at: int | None


@dataclass(frozen=True, slots=True)
class EpisodeCoverage:
    """Normalized numbered and special episode claims parsed from a release."""

    episodes: frozenset[int] = frozenset()
    specials: frozenset[int] = frozenset()
    numbering_override: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "episodes", frozenset(self.episodes))
        object.__setattr__(self, "specials", frozenset(self.specials))


@dataclass(frozen=True, slots=True)
class MediaCandidate:
    """One globally comparable release candidate before any RD mutation."""

    torrent_hash: str
    source_title: str
    magnet: str
    seeders: int
    size_bytes: int
    quality: str
    score: int
    coverage: frozenset[int] = frozenset()
    search_alias: str = ""
    is_trusted: bool = False
    is_batch: bool = False
    catalog_kind: str = "nyaa"
    metadata_backed: bool = False
    advertised_audio_languages: tuple[str, ...] = ()
    advertised_subtitle_languages: tuple[str, ...] = ()
    language_advertisement_complete: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "torrent_hash", self.torrent_hash.upper())
        object.__setattr__(self, "coverage", frozenset(self.coverage))
        object.__setattr__(self, "catalog_kind", self.catalog_kind.strip().casefold())
        for field in ("advertised_audio_languages", "advertised_subtitle_languages"):
            values = getattr(self, field)
            if not isinstance(values, (tuple, list)) or len(values) > 8:
                raise ValueError("Advertised language hints must be a bounded list.")
            normalized = tuple(
                dict.fromkeys(
                    tag for value in values if (tag := normalize_language(value, observation=True))
                )
            )
            object.__setattr__(self, field, normalized)
        if type(self.language_advertisement_complete) is not bool:
            raise ValueError("Advertised language completeness must be a boolean.")


@dataclass(frozen=True, slots=True)
class MediaAsset:
    """Sanitized in-process view of one persisted RD-backed media asset."""

    id: int
    monitor_id: int
    torrent_hash: str
    rd_torrent_id: str
    source_title: str
    source_folder: str
    quality: str
    planned_coverage: frozenset[int]
    usable_coverage: frozenset[int]
    numbering_override: str | None
    status: str
    inventory_verified_at: str | None
    provider_id: int | None = None
    provider_asset_id: str = ""
    storage_backend: str = ""
    storage_locator: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "torrent_hash", self.torrent_hash.upper())
        object.__setattr__(self, "planned_coverage", frozenset(self.planned_coverage))
        object.__setattr__(self, "usable_coverage", frozenset(self.usable_coverage))


@dataclass(frozen=True, slots=True)
class MediaSyncResult:
    """Immutable outcome from one monitor reconciliation."""

    monitor_id: int
    run_id: int
    outcome: str
    required: frozenset[int]
    covered: frozenset[int]
    accepted_assets: tuple[MediaAsset, ...]
    detail: str
    retry_after_seconds: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "required", frozenset(self.required))
        object.__setattr__(self, "covered", frozenset(self.covered))
        object.__setattr__(self, "accepted_assets", tuple(self.accepted_assets))


@dataclass(frozen=True, slots=True)
class SyncLeaseReceipt:
    """Token-safe outcome from one atomic global lease acquisition."""

    acquired: bool
    outcome: str
    expires_at: str
    recovered_owner_hash: str = ""
