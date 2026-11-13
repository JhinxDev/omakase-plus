"""Manifest-backed, atomic Anime overlays for the CT111 Plex curator."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from episode_parser import EpisodeParseError, parse_file_episode
from subtitle_support import (
    EmbeddedSubtitle,
    SubtitleAuditError,
    SubtitleProbe,
    plan_english_embedded,
)

from omakase.plus.manifest import validate_library_manifest

_PLAN_SCHEMA = "overlay-plan-v6"
_GENERATION_LAYOUT = "plex-alias-v5"
_MEDIA_SUFFIXES = frozenset({".avi", ".m2ts", ".m4v", ".mkv", ".mp4", ".ts"})
_SECTIONS = frozenset({"Anime", "Anime Movies"})
_BLOCK_CODES = frozenset(
    {
        "ASSET_COVERAGE_INVALID",
        "ASSET_VALIDATION_FAILED",
        "DESTINATION_CONFLICT",
        "ENGLISH_SUBTITLE_MISSING",
        "MEDIA_PROBE_FAILED",
        "LANGUAGE_POLICY_UNCONFIRMED",
        "POLICY_EXCLUDED",
        "SOURCE_UNAVAILABLE",
    }
)
_BLOCK_DETAILS = {
    "LANGUAGE_POLICY_UNCONFIRMED": "This exact file does not confirm its approved language publication policy.",
    "ASSET_COVERAGE_INVALID": (
        "The verified episode coverage does not match the readable media files."
    ),
    "ASSET_VALIDATION_FAILED": "This release failed a safe publication validation.",
    "DESTINATION_CONFLICT": "This release would collide with another managed Plex destination.",
    "ENGLISH_SUBTITLE_MISSING": (
        "This release has no safe selectable full-dialogue English subtitle track."
    ),
    "MEDIA_PROBE_FAILED": (
        "The provider-backed media could not be read during the subtitle probe. "
        "It may be locked or temporarily unavailable."
    ),
    "POLICY_EXCLUDED": "This source is excluded from managed Plex publication by policy.",
    "SOURCE_UNAVAILABLE": (
        "The provider-backed folder is unavailable or no longer matches this asset."
    ),
}
_NONRETRYABLE_BLOCK_CODES = frozenset(
    {"DESTINATION_CONFLICT", "POLICY_EXCLUDED", "LANGUAGE_POLICY_UNCONFIRMED"}
)


class CuratorValidationError(ValueError):
    """The manifest or filesystem cannot produce one safe complete overlay."""


class AssetPublicationBlocked(CuratorValidationError):
    """A single verified provider asset cannot safely enter this generation."""

    def __init__(self, code: str) -> None:
        if code not in _BLOCK_CODES:
            raise ValueError("unknown asset publication block code")
        self.code = code
        super().__init__(_BLOCK_DETAILS[code])


@dataclass(frozen=True, slots=True)
class TrustedAnimeFolder:
    """One explicitly reviewed historical folder admitted outside the manifest."""

    source_folder: str
    section: str
    canonical_title: str
    season_number: int
    reason: str

    def __post_init__(self) -> None:
        _contained_component(self.source_folder, "trusted source folder")
        _contained_component(self.canonical_title, "trusted canonical title")
        if self.section not in _SECTIONS:
            raise CuratorValidationError("trusted section is not an Anime section")
        if not 1 <= self.season_number <= 99:
            raise CuratorValidationError("trusted season is out of bounds")
        if not self.reason.strip() or len(self.reason) > 300:
            raise CuratorValidationError("trusted reason is missing or too long")


@dataclass(frozen=True, slots=True)
class PlexMatchHint:
    """One reviewed Plex series identity written as a local .plexmatch contract."""

    canonical_title: str
    season_number: int
    source: str
    source_id: str

    def __post_init__(self) -> None:
        _contained_component(self.canonical_title, "match-hint canonical title")
        if not 1 <= self.season_number <= 99:
            raise CuratorValidationError("match-hint season is out of bounds")
        if self.source not in {"imdb", "tmdb", "tvdb"}:
            raise CuratorValidationError("match-hint source is unsupported")
        if not isinstance(self.source_id, str) or not self.source_id:
            raise CuratorValidationError("match-hint source ID is invalid")
        if self.source == "imdb":
            valid = self.source_id.startswith("tt") and self.source_id[2:].isdigit()
        else:
            valid = self.source_id.isdigit() and int(self.source_id) > 0
        if not valid or len(self.source_id) > 32:
            raise CuratorValidationError("match-hint source ID is invalid")

    @property
    def destination(self) -> str:
        return PurePosixPath("Anime", self.canonical_title, ".plexmatch").as_posix()

    @property
    def content(self) -> str:
        return f"{self.source}id: {self.source_id}\nseason: {self.season_number}\n"

    def to_dict(self) -> dict[str, object]:
        return {
            "canonical_title": self.canonical_title,
            "season_number": self.season_number,
            "source": self.source,
            "source_id": self.source_id,
        }

    @classmethod
    def from_dict(cls, payload: object) -> PlexMatchHint:
        if not isinstance(payload, dict) or set(payload) != {
            "canonical_title",
            "season_number",
            "source",
            "source_id",
        }:
            raise CuratorValidationError("match hint has an unknown shape")
        return cls(**payload)


@dataclass(frozen=True, slots=True)
class CuratorConfig:
    """Contained filesystem roots and the versioned historical-admission policy."""

    source_root: Path
    overlay_root: Path
    riven_roots: tuple[Path, ...] = ()
    mount_aliases: tuple[tuple[Path, Path], ...] = ()
    trusted_allowlist: tuple[TrustedAnimeFolder, ...] = ()
    excluded_source_folders: tuple[str, ...] = ()
    plex_match_hints: tuple[PlexMatchHint, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_root", Path(self.source_root))
        object.__setattr__(self, "overlay_root", Path(self.overlay_root))
        object.__setattr__(self, "riven_roots", tuple(Path(p) for p in self.riven_roots))
        object.__setattr__(
            self,
            "mount_aliases",
            tuple((Path(alias), Path(target)) for alias, target in self.mount_aliases),
        )
        object.__setattr__(self, "trusted_allowlist", tuple(self.trusted_allowlist))
        object.__setattr__(self, "plex_match_hints", tuple(self.plex_match_hints))
        object.__setattr__(
            self,
            "excluded_source_folders",
            tuple(
                _contained_component(value, "excluded source folder")
                for value in self.excluded_source_folders
            ),
        )
        if len({hint.destination.casefold() for hint in self.plex_match_hints}) != len(
            self.plex_match_hints
        ):
            raise CuratorValidationError("Plex match-hint destinations are duplicated")
        if self.source_root == self.overlay_root:
            raise CuratorValidationError("source and overlay roots must differ")

    @property
    def generations_root(self) -> Path:
        return self.overlay_root / "generations"

    @property
    def current_pointer(self) -> Path:
        return self.overlay_root / "current"


@dataclass(frozen=True, slots=True)
class OverlayEntry:
    source: str
    destination: str
    section: str

    def __post_init__(self) -> None:
        source = _contained_relative_path(self.source, "plan source")
        destination = _contained_relative_path(self.destination, "plan destination")
        if self.section not in _SECTIONS or destination.parts[0] != self.section:
            raise CuratorValidationError("plan entry section mismatch")
        object.__setattr__(self, "source", source.as_posix())
        object.__setattr__(self, "destination", destination.as_posix())

    def to_dict(self) -> dict[str, str]:
        return {
            "source": self.source,
            "destination": self.destination,
            "section": self.section,
        }


@dataclass(frozen=True, slots=True)
class PublishedTitle:
    """The exact usable episodes that this generation is allowed to expose."""

    anilist_id: int
    section: str
    season_number: int | None
    usable_episodes: tuple[int, ...]
    published_episodes: tuple[int, ...]

    def __post_init__(self) -> None:
        if (
            isinstance(self.anilist_id, bool)
            or not isinstance(self.anilist_id, int)
            or not 1 <= self.anilist_id <= 2_147_483_647
        ):
            raise CuratorValidationError("published title AniList ID is invalid")
        if self.section not in _SECTIONS:
            raise CuratorValidationError("published title section is invalid")
        if self.section == "Anime Movies":
            if self.season_number is not None:
                raise CuratorValidationError("published movie season is invalid")
        elif (
            isinstance(self.season_number, bool)
            or not isinstance(self.season_number, int)
            or not 1 <= self.season_number <= 99
        ):
            raise CuratorValidationError("published series season is invalid")
        usable = _canonical_episode_tuple(self.usable_episodes, "published usable episodes")
        published = _canonical_episode_tuple(
            self.published_episodes,
            "published overlay episodes",
            allow_empty=True,
        )
        if not set(published) <= set(usable):
            raise CuratorValidationError("published overlay episodes escape usable coverage")
        object.__setattr__(self, "usable_episodes", usable)
        object.__setattr__(self, "published_episodes", published)

    def to_dict(self) -> dict[str, object]:
        return {
            "anilist_id": self.anilist_id,
            "section": self.section,
            "season_number": self.season_number,
            "usable_episodes": list(self.usable_episodes),
            "published_episodes": list(self.published_episodes),
        }

    @classmethod
    def from_dict(cls, payload: object) -> PublishedTitle:
        if not isinstance(payload, dict) or set(payload) != {
            "anilist_id",
            "section",
            "season_number",
            "usable_episodes",
            "published_episodes",
        }:
            raise CuratorValidationError("published title has an unknown shape")
        return cls(
            anilist_id=payload["anilist_id"],
            section=payload["section"],
            season_number=payload["season_number"],
            usable_episodes=tuple(payload["usable_episodes"])
            if isinstance(payload["usable_episodes"], list)
            else (),
            published_episodes=tuple(payload["published_episodes"])
            if isinstance(payload["published_episodes"], list)
            else (),
        )


@dataclass(frozen=True, slots=True)
class BlockedAsset:
    """One safe, non-secret reason why a verified provider asset was withheld."""

    anilist_id: int
    section: str
    season_number: int | None
    torrent_hash: str
    affected_episodes: tuple[int, ...]
    code: str
    detail: str
    retryable: bool

    def __post_init__(self) -> None:
        PublishedTitle(
            anilist_id=self.anilist_id,
            section=self.section,
            season_number=self.season_number,
            usable_episodes=self.affected_episodes,
            published_episodes=(),
        )
        torrent_hash = self.torrent_hash
        if (
            not isinstance(torrent_hash, str)
            or len(torrent_hash) not in {40, 64}
            or any(character not in "0123456789ABCDEF" for character in torrent_hash)
        ):
            raise CuratorValidationError("blocked asset hash is invalid")
        episodes = _canonical_episode_tuple(
            self.affected_episodes,
            "blocked asset episodes",
        )
        if self.code not in _BLOCK_CODES:
            raise CuratorValidationError("blocked asset code is invalid")
        if not isinstance(self.detail, str) or self.detail != _BLOCK_DETAILS[self.code]:
            raise CuratorValidationError("blocked asset detail is invalid")
        if not isinstance(self.retryable, bool) or self.retryable != (
            self.code not in _NONRETRYABLE_BLOCK_CODES
        ):
            raise CuratorValidationError("blocked asset retry policy is invalid")
        object.__setattr__(self, "affected_episodes", episodes)

    def to_dict(self) -> dict[str, object]:
        return {
            "anilist_id": self.anilist_id,
            "section": self.section,
            "season_number": self.season_number,
            "torrent_hash": self.torrent_hash,
            "affected_episodes": list(self.affected_episodes),
            "code": self.code,
            "detail": self.detail,
            "retryable": self.retryable,
        }

    @classmethod
    def from_dict(cls, payload: object) -> BlockedAsset:
        if not isinstance(payload, dict) or set(payload) != {
            "anilist_id",
            "section",
            "season_number",
            "torrent_hash",
            "affected_episodes",
            "code",
            "detail",
            "retryable",
        }:
            raise CuratorValidationError("blocked asset has an unknown shape")
        return cls(
            anilist_id=payload["anilist_id"],
            section=payload["section"],
            season_number=payload["season_number"],
            torrent_hash=payload["torrent_hash"],
            affected_episodes=tuple(payload["affected_episodes"])
            if isinstance(payload["affected_episodes"], list)
            else (),
            code=payload["code"],
            detail=payload["detail"],
            retryable=payload["retryable"],
        )


@dataclass(frozen=True, slots=True)
class OverlayPlan:
    manifest_digest: str
    excluded_riven_folders: tuple[str, ...]
    entries: tuple[OverlayEntry, ...]
    embedded_subtitles: tuple[EmbeddedSubtitle, ...]
    plex_match_hints: tuple[PlexMatchHint, ...]
    title_publications: tuple[PublishedTitle, ...]
    blocked_assets: tuple[BlockedAsset, ...]
    plan_hash: str
    language_files: tuple[dict, ...] = ()
    language_v3: bool = False
    publication_files: tuple[dict, ...] = ()
    quality_v4: bool = False
    quality_hold: bool = False

    @property
    def holds_current_generation(self) -> bool:
        """Avoid an empty destructive swap when every current title was blocked."""
        return (
            self.quality_hold
            or bool(self.title_publications)
            and not any(publication.published_episodes for publication in self.title_publications)
        )

    def _unsigned(self) -> dict:
        result = {
            "schema_version": _PLAN_SCHEMA,
            "manifest_digest": self.manifest_digest,
            "excluded_riven_folders": list(self.excluded_riven_folders),
            "entries": [entry.to_dict() for entry in self.entries],
            "embedded_subtitles": [subtitle.to_dict() for subtitle in self.embedded_subtitles],
            "plex_match_hints": [hint.to_dict() for hint in self.plex_match_hints],
            "title_publications": [
                publication.to_dict() for publication in self.title_publications
            ],
            "blocked_assets": [asset.to_dict() for asset in self.blocked_assets],
        }

        if self.language_v3:
            result["schema_version"] = "overlay-plan-v7"
            result["language_files"] = list(self.language_files)
        if self.quality_v4:
            result["schema_version"] = "overlay-plan-v8"
            result["publication_files"] = list(self.publication_files)
            result["quality_hold"] = self.quality_hold
        return result

    def to_dict(self) -> dict:
        return {**self._unsigned(), "plan_hash": self.plan_hash}

    @classmethod
    def from_dict(cls, payload: dict) -> OverlayPlan:
        v4 = isinstance(payload, dict) and payload.get("schema_version") == "overlay-plan-v8"
        v3 = isinstance(payload, dict) and payload.get("schema_version") in {
            "overlay-plan-v7",
            "overlay-plan-v8",
        }
        if not isinstance(payload, dict) or set(payload) != (
            {
                "schema_version",
                "manifest_digest",
                "excluded_riven_folders",
                "entries",
                "embedded_subtitles",
                "plex_match_hints",
                "title_publications",
                "blocked_assets",
                "plan_hash",
            }
            | ({"language_files"} if v3 else set())
            | ({"publication_files", "quality_hold"} if v4 else set())
        ):
            raise CuratorValidationError("overlay plan has an unknown shape")
        if payload["schema_version"] not in {_PLAN_SCHEMA, "overlay-plan-v7", "overlay-plan-v8"}:
            raise CuratorValidationError("overlay plan schema is unsupported")
        digest = _sha256_value(payload["manifest_digest"], "manifest digest")
        plan_hash = _sha256_value(payload["plan_hash"], "plan hash")
        excluded_raw = payload["excluded_riven_folders"]
        entries_raw = payload["entries"]
        embedded_raw = payload["embedded_subtitles"]
        hints_raw = payload["plex_match_hints"]
        publications_raw = payload["title_publications"]
        blocked_raw = payload["blocked_assets"]
        if not isinstance(excluded_raw, list) or excluded_raw != sorted(set(excluded_raw)):
            raise CuratorValidationError("overlay exclusions are not canonical")
        excluded = tuple(
            _contained_component(value, "excluded Riven folder") for value in excluded_raw
        )
        if not isinstance(entries_raw, list):
            raise CuratorValidationError("overlay entries are not a list")
        entries: list[OverlayEntry] = []
        for raw in entries_raw:
            if not isinstance(raw, dict) or set(raw) != {"source", "destination", "section"}:
                raise CuratorValidationError("overlay entry has an unknown shape")
            entries.append(OverlayEntry(**raw))
        if tuple(entries) != tuple(sorted(entries, key=lambda item: item.destination)):
            raise CuratorValidationError("overlay entries are not canonical")
        if len({entry.destination.casefold() for entry in entries}) != len(entries):
            raise CuratorValidationError("overlay contains duplicate destinations")
        if not isinstance(embedded_raw, list):
            raise CuratorValidationError("embedded subtitles are not a list")
        try:
            embedded = tuple(EmbeddedSubtitle.from_dict(raw) for raw in embedded_raw)
        except SubtitleAuditError as exc:
            raise CuratorValidationError("embedded subtitle is invalid") from exc
        if embedded != tuple(
            sorted(embedded, key=lambda item: (item.source, item.stream_index, item.role))
        ):
            raise CuratorValidationError("embedded subtitles are not canonical")
        if len({(item.source, item.stream_index) for item in embedded}) != len(embedded):
            raise CuratorValidationError("embedded subtitle streams are duplicated")
        if not isinstance(hints_raw, list):
            raise CuratorValidationError("Plex match hints are not a list")
        hints = tuple(PlexMatchHint.from_dict(raw) for raw in hints_raw)
        if hints != tuple(
            sorted(
                hints,
                key=lambda item: (
                    item.canonical_title.casefold(),
                    item.season_number,
                    item.source,
                    item.source_id,
                ),
            )
        ):
            raise CuratorValidationError("Plex match hints are not canonical")
        if len({hint.destination.casefold() for hint in hints}) != len(hints):
            raise CuratorValidationError("Plex match-hint destinations are duplicated")
        if not isinstance(publications_raw, list):
            raise CuratorValidationError("title publications are not a list")
        publications = tuple(PublishedTitle.from_dict(raw) for raw in publications_raw)

        def publication_key(item: PublishedTitle) -> tuple[int, str, int]:
            return (item.anilist_id, item.section, item.season_number or 0)

        if publications != tuple(sorted(publications, key=publication_key)):
            raise CuratorValidationError("title publications are not canonical")
        if len({publication_key(item) for item in publications}) != len(publications):
            raise CuratorValidationError("title publications are duplicated")
        if not isinstance(blocked_raw, list):
            raise CuratorValidationError("blocked assets are not a list")
        blocked = tuple(BlockedAsset.from_dict(raw) for raw in blocked_raw)

        def block_key(item: BlockedAsset) -> tuple[int, str, int, str]:
            return (
                item.anilist_id,
                item.section,
                item.season_number or 0,
                item.torrent_hash,
            )

        if blocked != tuple(sorted(blocked, key=block_key)):
            raise CuratorValidationError("blocked assets are not canonical")
        if len({block_key(item) for item in blocked}) != len(blocked):
            raise CuratorValidationError("blocked assets are duplicated")
        publication_by_key = {publication_key(item): item for item in publications}
        for asset in blocked:
            publication = publication_by_key.get(
                (asset.anilist_id, asset.section, asset.season_number or 0)
            )
            if publication is None or not set(asset.affected_episodes) <= set(
                publication.usable_episodes
            ):
                raise CuratorValidationError("blocked asset coverage does not match a title")
        destinations = [entry.destination.casefold() for entry in entries]
        if len(set(destinations)) != len(destinations):
            raise CuratorValidationError("overlay contains duplicate destinations")
        language_files = tuple(payload.get("language_files", ()))
        if v3:
            from omakase.plus.language_publication import validate_file

            if not isinstance(payload["language_files"], list) or len(language_files) > 4000:
                raise CuratorValidationError("Invalid language publication files")
            for proof in language_files:
                validate_file(proof)
                if not v4 and proof["source"] not in {entry.source for entry in entries}:
                    raise CuratorValidationError("Language proof is not a staged media source")
        publication_files = tuple(payload.get("publication_files", ()))
        if v4:
            from omakase.plus.quality_publication import validate_publication_file

            if (
                not isinstance(payload["publication_files"], list)
                or len(publication_files) > 4000
                or type(payload["quality_hold"]) is not bool
            ):
                raise CuratorValidationError("Invalid quality publication facts")
            for file in publication_files:
                validate_publication_file(file)
                if file["selected"] and file["source"] not in {entry.source for entry in entries}:
                    raise CuratorValidationError("Selected source is not staged")
            if any(
                proof["source"] not in {file["source"] for file in publication_files}
                for proof in language_files
            ):
                raise CuratorValidationError("Language proof has no qualified candidate")
        plan = cls(
            manifest_digest=digest,
            excluded_riven_folders=excluded,
            entries=tuple(entries),
            embedded_subtitles=embedded,
            plex_match_hints=hints,
            title_publications=publications,
            blocked_assets=blocked,
            plan_hash=plan_hash,
            language_files=language_files,
            language_v3=v3,
            publication_files=publication_files,
            quality_v4=v4,
            quality_hold=payload.get("quality_hold", False),
        )
        if not hashlib.sha256(_canonical_bytes(plan._unsigned())).hexdigest() == plan_hash:
            raise CuratorValidationError("overlay plan hash does not match its content")
        return plan


@dataclass(frozen=True, slots=True)
class OverlayApplyResult:
    generation: Path | None
    previous_generation: Path | None
    plan_hash: str
    changed: bool


@dataclass(frozen=True, slots=True)
class StagedOverlay:
    root: Path
    links: tuple[Path, ...]
    plan_hash: str


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _sha256_value(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise CuratorValidationError(f"{field} is not a lowercase SHA-256")
    return value


def _canonical_episode_tuple(
    value: object,
    field: str,
    *,
    allow_empty: bool = False,
) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)):
        raise CuratorValidationError(f"{field} are not an episode list")
    episodes = tuple(value)
    if not episodes and not allow_empty:
        raise CuratorValidationError(f"{field} are empty")
    if any(
        isinstance(episode, bool) or not isinstance(episode, int) or not 1 <= episode <= 10_000
        for episode in episodes
    ):
        raise CuratorValidationError(f"{field} contain an invalid episode")
    if episodes != tuple(sorted(set(episodes))):
        raise CuratorValidationError(f"{field} are not canonical")
    return episodes


def _contained_component(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise CuratorValidationError(f"{field} is not one contained path component")
    return value


def _contained_relative_path(value: object, field: str) -> PurePosixPath:
    if not isinstance(value, str) or "\\" in value:
        raise CuratorValidationError(f"{field} is not a contained relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise CuratorValidationError(f"{field} is not a contained relative path")
    return path


def _destination_name(title: str, season: int, start: int, end: int, suffix: str) -> str:
    episode = f"S{season:02d}E{start:02d}"
    if end != start:
        episode += f"-E{end:02d}"
    return f"{title} - {episode}{suffix.lower()}"


def _apply_mount_alias(path: Path, aliases: tuple[tuple[Path, Path], ...]) -> Path:
    raw = str(path)
    if os.name == "nt" and raw.startswith("\\\\?\\"):
        path = Path(raw[4:])
    for alias, canonical in sorted(aliases, key=lambda pair: len(str(pair[0])), reverse=True):
        try:
            relative = path.relative_to(alias)
        except ValueError:
            continue
        return canonical / relative
    return path


def _plex_visible_source(path: Path, aliases: tuple[tuple[Path, Path], ...]) -> Path:
    """Map one validated host source back into the Plex container mount namespace."""
    for alias, canonical in sorted(aliases, key=lambda pair: len(str(pair[1])), reverse=True):
        try:
            relative = path.relative_to(canonical.resolve(strict=True))
        except (OSError, RuntimeError, ValueError):
            continue
        return alias / relative
    return path


def derive_riven_exclusions(config: CuratorConfig) -> tuple[str, ...]:
    """Resolve every recursive Riven symlink through aliases to one zurg folder."""
    if not config.riven_roots:
        return ()
    source_root = config.source_root.resolve(strict=True)
    exclusions: set[str] = set()
    for configured_root in config.riven_roots:
        riven_root = configured_root.resolve(strict=True)
        if not riven_root.is_dir():
            raise CuratorValidationError("Riven exclusion root is not a directory")
        for candidate in sorted(riven_root.rglob("*"), key=lambda path: str(path)):
            if not candidate.is_symlink():
                continue
            try:
                raw_target = candidate.readlink()
                target = raw_target if raw_target.is_absolute() else candidate.parent / raw_target
                target = _apply_mount_alias(target, config.mount_aliases).resolve(strict=True)
                relative = target.relative_to(source_root)
            except (OSError, RuntimeError, ValueError) as exc:
                raise CuratorValidationError("Riven exclusion target cannot be resolved") from exc
            if not relative.parts:
                raise CuratorValidationError("Riven exclusion target is the source root")
            exclusions.add(relative.parts[0])
    return tuple(sorted(exclusions))


def _source_files(folder: Path, source_root: Path) -> tuple[Path, ...]:
    files: list[Path] = []
    for candidate in sorted(folder.rglob("*"), key=lambda path: str(path)):
        if candidate.suffix.casefold() not in _MEDIA_SUFFIXES or not candidate.is_file():
            continue
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(folder)
            resolved.relative_to(source_root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise CuratorValidationError("source media escapes its manifest folder") from exc
        files.append(resolved)
    return tuple(files)


def _append_entry(
    entries: list[OverlayEntry],
    destinations: set[str],
    *,
    source: Path,
    source_root: Path,
    section: str,
    title: str,
    season: int,
    start: int,
    end: int,
) -> OverlayEntry:
    season_dir = "Season 00" if season == 0 else f"Season {season:02d}"
    destination = PurePosixPath(section, title, season_dir) / _destination_name(
        title, season, start, end, source.suffix
    )
    destination_key = destination.as_posix().casefold()
    if destination_key in destinations:
        raise CuratorValidationError("duplicate normalized destination")
    destinations.add(destination_key)
    entry = OverlayEntry(
        source=source.relative_to(source_root).as_posix(),
        destination=destination.as_posix(),
        section=section,
    )
    entries.append(entry)
    return entry


def _append_movie_entry(
    entries: list[OverlayEntry],
    destinations: set[str],
    *,
    source: Path,
    source_root: Path,
    title: str,
    quality: str,
    torrent_hash: str,
) -> OverlayEntry:
    destination = PurePosixPath("Anime Movies", title) / (
        f"{title} - {quality} [{torrent_hash[:8]}]{source.suffix.lower()}"
    )
    destination_key = destination.as_posix().casefold()
    if destination_key in destinations:
        raise CuratorValidationError("duplicate normalized destination")
    destinations.add(destination_key)
    entry = OverlayEntry(
        source=source.relative_to(source_root).as_posix(),
        destination=destination.as_posix(),
        section="Anime Movies",
    )
    entries.append(entry)
    return entry


def _append_english_subtitles(
    embedded_subtitles: list[EmbeddedSubtitle],
    *,
    source: Path,
    source_root: Path,
    probe: SubtitleProbe,
) -> None:
    relative_source = PurePosixPath(source.relative_to(source_root).as_posix())
    try:
        embedded = plan_english_embedded(source=relative_source, probe=probe)
    except SubtitleAuditError as exc:
        raise AssetPublicationBlocked("ENGLISH_SUBTITLE_MISSING") from exc
    embedded_subtitles.extend(embedded)


def build_overlay_plan(
    config: CuratorConfig,
    manifest: dict,
    *,
    subtitle_probe: Callable[[Path], SubtitleProbe] | None = None,
    require_english_subtitles: bool = False,
    language_probe=None,
    language_qualification: bool = False,
    quality_qualification: bool = False,
) -> OverlayPlan:
    """Produce one mutation-free, partial-safe plan for both Anime sections."""
    if require_english_subtitles and subtitle_probe is None:
        raise CuratorValidationError("English subtitle audit requires a subtitle probe")
    try:
        manifest = validate_library_manifest(manifest)
    except ValueError as exc:
        raise CuratorValidationError("manifest-v2 validation failed") from exc

    if manifest["schema_version"] == "manifest-v4":
        from quality_selection import build_quality_plan

        return build_quality_plan(
            config,
            manifest,
            subtitle_probe=subtitle_probe,
            require_english_subtitles=require_english_subtitles,
            language_probe=language_probe,
            language_qualification=language_qualification,
            quality_qualification=quality_qualification,
        )

    v3 = manifest["schema_version"] == "manifest-v3"
    if v3:
        from omakase.plus.language_publication import LANGUAGE_PUBLICATION_QUALIFIED

        if (
            not (LANGUAGE_PUBLICATION_QUALIFIED or language_qualification is True)
            or language_probe is None
        ):
            raise CuratorValidationError("Manifest3 language publication is not qualified.")
    language_files = []
    source_root = config.source_root.resolve(strict=True)
    excluded_riven = derive_riven_exclusions(config)
    excluded = {value.casefold() for value in excluded_riven}
    excluded.update(value.casefold() for value in config.excluded_source_folders)
    entries: list[OverlayEntry] = []
    embedded_subtitles: list[EmbeddedSubtitle] = []
    title_publications: list[PublishedTitle] = []
    blocked_assets: list[BlockedAsset] = []
    destinations: set[str] = set()
    claimed_folders: set[str] = set()
    staged_series: set[tuple[str, int]] = set()
    blocked_manifest_series: set[tuple[str, int]] = set()
    probe_cache: dict[Path, SubtitleProbe] = {}

    def audit(
        source: Path,
        bindings: list[EmbeddedSubtitle],
        proof_files: list,
        *,
        media=None,
        asset=None,
        episodes=(),
        sole_file=False,
    ) -> None:
        if asset is not None and "language_contract" in asset:
            try:
                proof_files.append(
                    language_probe(
                        source,
                        contract=asset["language_contract"],
                        anilist_id=media["anilist_id"],
                        season_number=media["season_number"] or 1,
                        torrent_hash=asset["torrent_hash"],
                        file_locator=(
                            source.relative_to(source_root).as_posix()
                            if asset["storage_backend"] == "local"
                            else source.relative_to(source_root / asset["source_folder"]).as_posix()
                        ),
                        source_relative=source.relative_to(source_root).as_posix(),
                        episodes=tuple(sorted(episodes)),
                        media_format=media["format"],
                        sole_file=sole_file,
                        expected_episodes=media["expected_episode_count"],
                        numbering_override=asset.get("numbering_override"),
                        approved_coverage=asset["episode_coverage"],
                        storage_backend=asset["storage_backend"],
                    )
                )
            except (ValueError, OSError, subprocess.SubprocessError) as exc:
                raise AssetPublicationBlocked("LANGUAGE_POLICY_UNCONFIRMED") from exc
            return
        if not require_english_subtitles:
            return
        assert subtitle_probe is not None
        try:
            probe = probe_cache.get(source)
            if probe is None:
                probe = subtitle_probe(source)
                probe_cache[source] = probe
        except (OSError, subprocess.SubprocessError, SubtitleAuditError) as exc:
            raise AssetPublicationBlocked("MEDIA_PROBE_FAILED") from exc
        _append_english_subtitles(
            bindings,
            source=source,
            source_root=source_root,
            probe=probe,
        )

    def record_block(
        media: dict,
        asset: dict,
        *,
        section: str,
        season_number: int | None,
        code: str,
    ) -> None:
        blocked_assets.append(
            BlockedAsset(
                anilist_id=media["anilist_id"],
                section=section,
                season_number=season_number,
                torrent_hash=asset["torrent_hash"].upper(),
                affected_episodes=tuple(asset["episode_coverage"]),
                code=code,
                detail=_BLOCK_DETAILS[code],
                retryable=code not in _NONRETRYABLE_BLOCK_CODES,
            )
        )

    def resolve_manifest_folder(folder_name: str) -> Path:
        try:
            folder = (source_root / folder_name).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise AssetPublicationBlocked("SOURCE_UNAVAILABLE") from exc
        try:
            folder.relative_to(source_root)
        except ValueError as exc:
            raise CuratorValidationError("manifest source folder escapes root") from exc
        if not folder.is_dir():
            raise AssetPublicationBlocked("SOURCE_UNAVAILABLE")
        return folder

    for media in manifest["titles"]:
        title = _contained_component(media["canonical_title"], "canonical title")
        is_movie = media["format"] == "MOVIE"
        section = "Anime Movies" if is_movie else "Anime"
        season = media["season_number"] or 1
        season_number = None if is_movie else season
        expected = media["expected_episode_count"]
        usable = set(media["coverage"]["usable_episodes"])
        staged_coverage: set[int] = set()
        blocked_before = len(blocked_assets)

        for asset in media["assets"]:
            folder_name = _contained_component(asset["source_folder"], "source folder")
            folder_key = folder_name.casefold()
            if folder_key in claimed_folders:
                raise CuratorValidationError("source folder matches more than one manifest asset")
            claimed_folders.add(folder_key)
            asset_coverage = set(asset["episode_coverage"])
            if not asset_coverage <= usable:
                raise CuratorValidationError(
                    "manifest asset coverage escapes title usable coverage"
                )
            if folder_key in excluded:
                record_block(
                    media,
                    asset,
                    section=section,
                    season_number=season_number,
                    code="POLICY_EXCLUDED",
                )
                continue

            asset_entries: list[OverlayEntry] = []
            asset_subtitles: list[EmbeddedSubtitle] = []
            asset_language_files = []
            asset_destinations = set(destinations)
            try:
                if v3 and asset.get("language_contract") and asset["storage_backend"] == "local":
                    exact = (source_root / asset["storage_locator"]).resolve(strict=True)
                    exact.relative_to(source_root)
                    if not exact.is_file() or exact.suffix.casefold() not in _MEDIA_SUFFIXES:
                        raise AssetPublicationBlocked("SOURCE_UNAVAILABLE")
                    source_files = (exact,)
                else:
                    folder = resolve_manifest_folder(folder_name)
                    source_files = _source_files(folder, source_root)
                asset_staged: set[int] = set()
                if is_movie:
                    if asset_coverage != {1} or len(source_files) != 1:
                        raise AssetPublicationBlocked("ASSET_COVERAGE_INVALID")
                    _append_movie_entry(
                        asset_entries,
                        asset_destinations,
                        source=source_files[0],
                        source_root=source_root,
                        title=title,
                        quality=asset["quality"],
                        torrent_hash=asset["torrent_hash"],
                    )
                    audit(
                        source_files[0],
                        asset_subtitles,
                        asset_language_files,
                        media=media,
                        asset=asset,
                        episodes=(1,),
                        sole_file=len(source_files) == 1,
                    )
                    asset_staged.add(1)
                else:
                    for source in source_files:
                        try:
                            episode = parse_file_episode(
                                source.name,
                                season=season,
                                expected=expected,
                                numbering_override=asset.get("numbering_override"),
                                anilist_id=media["anilist_id"],
                            )
                        except EpisodeParseError:
                            continue
                        if episode.special:
                            if asset.get("language_contract"):
                                continue
                            _append_entry(
                                asset_entries,
                                asset_destinations,
                                source=source,
                                source_root=source_root,
                                section=section,
                                title=title,
                                season=0,
                                start=episode.start,
                                end=episode.end,
                            )
                            audit(
                                source,
                                asset_subtitles,
                                asset_language_files,
                                media=media,
                                asset=asset,
                                episodes=episode.episodes,
                            )
                            continue
                        if not episode.episodes <= asset_coverage:
                            continue
                        _append_entry(
                            asset_entries,
                            asset_destinations,
                            source=source,
                            source_root=source_root,
                            section=section,
                            title=title,
                            season=season,
                            start=episode.start,
                            end=episode.end,
                        )
                        audit(
                            source,
                            asset_subtitles,
                            asset_language_files,
                            media=media,
                            asset=asset,
                            episodes=episode.episodes,
                            sole_file=len(source_files) == 1,
                        )
                        asset_staged.update(episode.episodes)
                if asset_staged != asset_coverage:
                    raise AssetPublicationBlocked("ASSET_COVERAGE_INVALID")
            except AssetPublicationBlocked as exc:
                record_block(
                    media,
                    asset,
                    section=section,
                    season_number=season_number,
                    code=exc.code,
                )
                continue
            except CuratorValidationError as exc:
                if str(exc) != "duplicate normalized destination":
                    raise
                record_block(
                    media,
                    asset,
                    section=section,
                    season_number=season_number,
                    code="DESTINATION_CONFLICT",
                )
                continue

            entries.extend(asset_entries)
            embedded_subtitles.extend(asset_subtitles)
            language_files.extend(asset_language_files)
            destinations.update(entry.destination.casefold() for entry in asset_entries)
            staged_coverage.update(asset_staged)

        if usable:
            title_publications.append(
                PublishedTitle(
                    anilist_id=media["anilist_id"],
                    section=section,
                    season_number=season_number,
                    usable_episodes=tuple(sorted(usable)),
                    published_episodes=tuple(sorted(staged_coverage)),
                )
            )
        if not is_movie:
            series_key = (title.casefold(), season)
            if staged_coverage:
                staged_series.add(series_key)
            elif usable and len(blocked_assets) > blocked_before:
                blocked_manifest_series.add(series_key)

    for trusted in config.trusted_allowlist:
        folder_key = trusted.source_folder.casefold()
        if folder_key in excluded:
            continue
        if folder_key in claimed_folders:
            raise CuratorValidationError("trusted folder overlaps a manifest asset")
        claimed_folders.add(folder_key)
        staged_series.add((trusted.canonical_title.casefold(), trusted.season_number))
        try:
            folder = (source_root / trusted.source_folder).resolve(strict=True)
            folder.relative_to(source_root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise CuratorValidationError(
                "trusted source folder is missing or escapes root"
            ) from exc
        for source in _source_files(folder, source_root):
            try:
                episode = parse_file_episode(
                    source.name,
                    season=trusted.season_number,
                    expected=None,
                )
            except EpisodeParseError:
                continue
            normalized_season = 0 if episode.special else trusted.season_number
            _append_entry(
                entries,
                destinations,
                source=source,
                source_root=source_root,
                section=trusted.section,
                title=trusted.canonical_title,
                season=normalized_season,
                start=episode.start,
                end=episode.end,
            )

    entries.sort(key=lambda entry: entry.destination)
    embedded_subtitles.sort(
        key=lambda subtitle: (subtitle.source, subtitle.stream_index, subtitle.role)
    )
    title_publications.sort(
        key=lambda item: (item.anilist_id, item.section, item.season_number or 0)
    )
    blocked_assets.sort(
        key=lambda item: (
            item.anilist_id,
            item.section,
            item.season_number or 0,
            item.torrent_hash,
        )
    )
    if len({(subtitle.source, subtitle.stream_index) for subtitle in embedded_subtitles}) != len(
        embedded_subtitles
    ):
        raise CuratorValidationError("embedded subtitle streams are duplicated")
    plex_match_hints = tuple(
        sorted(
            config.plex_match_hints,
            key=lambda item: (
                item.canonical_title.casefold(),
                item.season_number,
                item.source,
                item.source_id,
            ),
        )
    )
    for hint in plex_match_hints:
        series_key = (hint.canonical_title.casefold(), hint.season_number)
        if series_key in staged_series or series_key in blocked_manifest_series:
            continue
        raise CuratorValidationError("Plex match hint does not identify a staged series")

    # The endpoint stamps every response with a fresh generated_at. The approved
    # plan binds the actual media contract, while the run receipt records the
    # observation timestamp separately.
    manifest_contract = {
        "schema_version": manifest["schema_version"],
        "titles": manifest["titles"],
    }
    manifest_digest = hashlib.sha256(_canonical_bytes(manifest_contract)).hexdigest()
    unsigned = {
        "schema_version": _PLAN_SCHEMA,
        "manifest_digest": manifest_digest,
        "excluded_riven_folders": list(excluded_riven),
        "entries": [entry.to_dict() for entry in entries],
        "embedded_subtitles": [subtitle.to_dict() for subtitle in embedded_subtitles],
        "plex_match_hints": [hint.to_dict() for hint in plex_match_hints],
        "title_publications": [publication.to_dict() for publication in title_publications],
        "blocked_assets": [asset.to_dict() for asset in blocked_assets],
    }
    if v3:
        unsigned["schema_version"] = "overlay-plan-v7"
        unsigned["language_files"] = language_files
    plan_hash = hashlib.sha256(_canonical_bytes(unsigned)).hexdigest()
    return OverlayPlan.from_dict({**unsigned, "plan_hash": plan_hash})


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _make_directories_traversable(root: Path) -> None:
    """Override the hardened service umask only for public media path traversal."""
    root.chmod(0o755)
    for directory in (path for path in root.rglob("*") if path.is_dir()):
        directory.chmod(0o755)


def _stage_entries(
    config: CuratorConfig,
    plan: OverlayPlan,
    staging: Path,
    *,
    embedded_subtitle_validator: Callable[[Path, EmbeddedSubtitle], None] | None = None,
) -> tuple[Path, ...]:
    if staging.exists() or staging.is_symlink():
        raise CuratorValidationError("staging generation already exists")
    source_root = config.source_root.resolve(strict=True)
    staging.mkdir(parents=True)
    (staging / "Anime").mkdir()
    (staging / "Anime Movies").mkdir()
    links: list[Path] = []
    try:
        for entry in plan.entries:
            relative_source = _contained_relative_path(entry.source, "plan source")
            source = (source_root / Path(*relative_source.parts)).resolve(strict=True)
            try:
                source.relative_to(source_root)
            except ValueError as exc:
                raise CuratorValidationError("planned source escapes canonical root") from exc
            if not source.is_file():
                raise CuratorValidationError("planned source is no longer a file")
            relative_destination = _contained_relative_path(entry.destination, "plan destination")
            destination = staging / Path(*relative_destination.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(_plex_visible_source(source, config.mount_aliases), destination)
            if not destination.is_symlink():
                raise CuratorValidationError("staged link was not created")
            links.append(destination)
        for hint in plan.plex_match_hints:
            relative_destination = _contained_relative_path(
                hint.destination,
                "match-hint destination",
            )
            destination = staging / Path(*relative_destination.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("x", encoding="utf-8", newline="\n") as file:
                file.write(hint.content)
                file.flush()
                os.fsync(file.fileno())
            destination.chmod(0o644)
        if plan.embedded_subtitles and embedded_subtitle_validator is None:
            raise CuratorValidationError("embedded subtitles require a validator")
        for binding in plan.embedded_subtitles:
            relative_source = _contained_relative_path(
                binding.source,
                "embedded subtitle source",
            )
            source = (source_root / Path(*relative_source.parts)).resolve(strict=True)
            try:
                source.relative_to(source_root)
            except ValueError as exc:
                raise CuratorValidationError(
                    "embedded subtitle source escapes canonical root"
                ) from exc
            assert embedded_subtitle_validator is not None
            embedded_subtitle_validator(source, binding)
        _make_directories_traversable(staging)
        for directory in sorted(
            (path for path in staging.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            _fsync_directory(directory)
        _fsync_directory(staging)
        _fsync_directory(staging.parent)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return tuple(links)


def _selected_generation(config: CuratorConfig) -> Path | None:
    current = config.current_pointer
    if not current.exists() and not current.is_symlink():
        return None
    if not current.is_symlink():
        raise CuratorValidationError("current overlay pointer is not a symlink")
    try:
        selected = current.resolve(strict=True)
        selected.relative_to(config.generations_root.resolve(strict=True))
    except (OSError, RuntimeError, ValueError) as exc:
        raise CuratorValidationError("current overlay pointer is invalid") from exc
    if not (selected / "Anime").is_dir() or not (selected / "Anime Movies").is_dir():
        raise CuratorValidationError("selected overlay generation is incomplete")
    return selected


def _replace_pointer(config: CuratorConfig, generation: Path) -> None:
    temporary = config.overlay_root / f".current-{generation.name}.tmp"
    if temporary.exists() or temporary.is_symlink():
        temporary.unlink()
    target = Path("generations") / generation.name
    os.symlink(target, temporary, target_is_directory=True)
    try:
        os.replace(temporary, config.current_pointer)
    except PermissionError:
        # Windows cannot replace an existing directory symlink. CT111 is Linux,
        # where the branch above is one atomic rename; this fallback keeps the
        # filesystem contract testable on the development workstation.
        if os.name != "nt" or not config.current_pointer.is_symlink():
            raise
        config.current_pointer.unlink()
        os.replace(temporary, config.current_pointer)
    _fsync_directory(config.overlay_root)


def _remove_generation(config: CuratorConfig, generation: Path) -> None:
    try:
        generation.resolve(strict=True).relative_to(config.generations_root.resolve(strict=True))
    except (OSError, RuntimeError, ValueError) as exc:
        raise CuratorValidationError("refusing to remove a non-generation path") from exc
    shutil.rmtree(generation)


def _require_matching_generation(expected: Path, retained: Path) -> None:
    """Compare an unselected tree with freshly validated staging, without following links."""

    def mismatch() -> None:
        raise CuratorValidationError("retained generation does not match the approved content")

    def compare(expected_directory: Path, retained_directory: Path) -> None:
        if not stat.S_ISDIR(expected_directory.lstat().st_mode) or not stat.S_ISDIR(
            retained_directory.lstat().st_mode
        ):
            mismatch()
        with os.scandir(expected_directory) as entries:
            expected_entries = {entry.name: entry for entry in entries}
        with os.scandir(retained_directory) as entries:
            retained_entries = {entry.name: entry for entry in entries}
        if expected_entries.keys() != retained_entries.keys():
            mismatch()
        for name, expected_entry in expected_entries.items():
            retained_entry = retained_entries[name]
            if expected_entry.is_symlink():
                if not retained_entry.is_symlink() or os.readlink(
                    expected_entry.path
                ) != os.readlink(retained_entry.path):
                    mismatch()
            elif expected_entry.is_dir(follow_symlinks=False):
                compare(Path(expected_entry.path), Path(retained_entry.path))
            else:
                if (
                    name != ".plexmatch"
                    or not expected_entry.is_file(follow_symlinks=False)
                    or not retained_entry.is_file(follow_symlinks=False)
                ):
                    mismatch()
                content = Path(expected_entry.path).read_bytes()
                if retained_entry.stat(follow_symlinks=False).st_size != len(content):
                    mismatch()
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
                descriptor = os.open(retained_entry.path, flags)
                with os.fdopen(descriptor, "rb") as stream:
                    if (
                        not stat.S_ISREG(os.fstat(stream.fileno()).st_mode)
                        or stream.read(len(content) + 1) != content
                    ):
                        mismatch()

    compare(expected, retained)


def apply_overlay_plan(
    config: CuratorConfig,
    plan: OverlayPlan,
    *,
    expected_plan_hash: str,
    after_swap: Callable[[OverlayApplyResult], None] | None = None,
    embedded_subtitle_validator: Callable[[Path, EmbeddedSubtitle], None] | None = None,
) -> OverlayApplyResult:
    """Stage both children, atomically swap once, and roll back handled failures."""
    plan = OverlayPlan.from_dict(plan.to_dict())
    expected_plan_hash = _sha256_value(expected_plan_hash, "expected plan hash")
    if not hmac.compare_digest(plan.plan_hash, expected_plan_hash):
        raise CuratorValidationError("live plan hash differs from approved dry-run")

    if plan.language_v3:
        from language_publication import validate_source

        source_root = config.source_root.resolve(strict=True)
        for proof in plan.language_files:
            path = (source_root / proof["source"]).resolve(strict=True)
            path.relative_to(source_root)
            validate_source(path, proof)
    if plan.quality_v4:
        source_root = config.source_root.resolve(strict=True)
        for file in plan.publication_files:
            path = (source_root / file["source"]).resolve(strict=True)
            path.relative_to(source_root)
            current = path.stat()
            if file["stat"] != {
                "size": current.st_size,
                "mtime_ns": current.st_mtime_ns,
                "file_device": current.st_dev,
                "file_inode": current.st_ino,
            }:
                raise CuratorValidationError("qualified source stat changed before publication")
    previous = _selected_generation(config)
    if plan.holds_current_generation:
        return OverlayApplyResult(previous, previous, plan.plan_hash, changed=False)

    config.overlay_root.mkdir(parents=True, exist_ok=True)
    config.overlay_root.chmod(0o755)
    config.generations_root.mkdir(exist_ok=True)
    config.generations_root.chmod(0o755)
    generation = config.generations_root / f"generation-{plan.plan_hash}-{_GENERATION_LAYOUT}"
    reuse = generation.exists() or generation.is_symlink()
    if reuse:
        if generation.is_symlink() or not generation.is_dir():
            raise CuratorValidationError("retained generation is not a real directory")
        if previous == generation:
            _make_directories_traversable(generation)
            result = OverlayApplyResult(generation, previous, plan.plan_hash, changed=False)
            if after_swap is not None:
                after_swap(result)
            return result
    staging = config.generations_root / f".staging-{plan.plan_hash}"
    _stage_entries(
        config,
        plan,
        staging,
        embedded_subtitle_validator=embedded_subtitle_validator,
    )
    if reuse:
        try:
            _require_matching_generation(staging, generation)
        finally:
            # _stage_entries succeeded, so only this invocation owns this stage.
            _remove_generation(config, staging)
        _make_directories_traversable(generation)
    else:
        os.replace(staging, generation)
        _fsync_directory(config.generations_root)
    result = OverlayApplyResult(generation, previous, plan.plan_hash, changed=True)
    try:
        _replace_pointer(config, generation)
        if after_swap is not None:
            after_swap(result)
    except Exception:
        # Replacement may have succeeded before its directory fsync raised.
        if previous is None:
            config.current_pointer.unlink(missing_ok=True)
            _fsync_directory(config.overlay_root)
        else:
            _replace_pointer(config, previous)
        if not reuse and generation.exists():
            _remove_generation(config, generation)
        raise

    keep = {generation}
    if previous is not None:
        keep.add(previous)
    for candidate in config.generations_root.iterdir():
        if candidate.name.startswith("generation-") and candidate not in keep:
            _remove_generation(config, candidate)
    return result


def stage_manifest_overlays(
    manifest: dict,
    *,
    source_root: Path,
    staging_root: Path,
) -> StagedOverlay:
    """Compatibility helper for the backend vertical fixture: stage without swapping."""
    config = CuratorConfig(source_root=source_root, overlay_root=staging_root.parent)
    plan = build_overlay_plan(config, manifest)
    links = _stage_entries(config, plan, staging_root)
    return StagedOverlay(root=staging_root, links=links, plan_hash=plan.plan_hash)
