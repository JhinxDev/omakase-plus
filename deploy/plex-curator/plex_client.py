"""Narrow, dry-run-first Plex API and immutable topology contracts."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

import httpx

ACTION_COLLECTION = "Action & Adventure Anime"
COMEDY_COLLECTION = "Comedy & Couch Anime"
DRAMA_COLLECTION = "Drama & Suspense Anime"
FANTASY_COLLECTION = "Fantasy Anime"
SCIFI_COLLECTION = "Sci-Fi & Mecha Anime"
READY_COLLECTION = "Ready"
WATCHING_COLLECTION = "Watching"
GENRE_COLLECTIONS = frozenset(
    {ACTION_COLLECTION, COMEDY_COLLECTION, DRAMA_COLLECTION, FANTASY_COLLECTION, SCIFI_COLLECTION}
)
SHELF_COLLECTIONS = frozenset({READY_COLLECTION, WATCHING_COLLECTION})
MANAGED_COLLECTIONS = GENRE_COLLECTIONS | SHELF_COLLECTIONS
_MANAGED_TAG_ORDER = (
    SCIFI_COLLECTION,
    FANTASY_COLLECTION,
    ACTION_COLLECTION,
    DRAMA_COLLECTION,
    COMEDY_COLLECTION,
    READY_COLLECTION,
    WATCHING_COLLECTION,
)
_SECTION_NAMES = ("Anime", "Anime Movies", "TV Shows", "Movies")
_TOPOLOGY_PLAN_SCHEMA = "plex-topology-plan-v1"
_BACKUP_KINDS = frozenset(
    {
        "curator-config",
        "overlay-state",
        "plex-database",
        "plex-preferences",
        "section-inventory",
        "systemd-units",
    }
)


class TopologyPlanError(ValueError):
    """Observed Plex state does not satisfy a fail-closed mutation gate."""


@dataclass(frozen=True, slots=True)
class PlexLibraryNames:
    """User-facing Plex labels for Omakase's four logical library roles."""

    anime: str = "Anime"
    anime_movies: str = "Anime Movies"
    tv: str = "TV Shows"
    movies: str = "Movies"

    def __post_init__(self) -> None:
        values = tuple(value.strip() for value in self.actual_titles())
        if any(
            not value or len(value) > 80 or any(ord(char) < 32 for char in value)
            for value in values
        ):
            raise TopologyPlanError("configured Plex library name is invalid")
        if len({value.casefold() for value in values}) != len(values):
            raise TopologyPlanError("configured Plex library names must be unique")
        for field, value in zip(("anime", "anime_movies", "tv", "movies"), values, strict=True):
            object.__setattr__(self, field, value)

    def actual_titles(self) -> tuple[str, str, str, str]:
        return (self.anime, self.anime_movies, self.tv, self.movies)

    def logical_to_actual(self) -> dict[str, str]:
        return dict(zip(_SECTION_NAMES, self.actual_titles(), strict=True))


@dataclass(frozen=True, slots=True)
class PlexSection:
    key: int
    title: str
    paths: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.key < 1 or not self.title:
            raise TopologyPlanError("invalid Plex section identity")
        object.__setattr__(self, "paths", tuple(self.paths))


@dataclass(frozen=True, slots=True)
class PlexAction:
    kind: str
    key: int | None
    current: str
    proposed: str


@dataclass(frozen=True, slots=True)
class TopologyPlan:
    path_update_items: tuple[tuple[int, str], ...]
    rename_items: tuple[tuple[int, str], ...]
    create_section_items: tuple[tuple[str, str], ...]
    deferred_removals: tuple[int, ...]
    intended_paths: tuple[str, ...]
    actions: tuple[PlexAction, ...]
    plan_hash: str

    @property
    def path_updates(self) -> dict[int, str]:
        return dict(self.path_update_items)

    @property
    def renames(self) -> dict[int, str]:
        return dict(self.rename_items)

    @property
    def create_sections(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self.create_section_items)

    def _unsigned(self) -> dict:
        return {
            "schema_version": _TOPOLOGY_PLAN_SCHEMA,
            "path_updates": [list(item) for item in self.path_update_items],
            "renames": [list(item) for item in self.rename_items],
            "create_sections": [list(item) for item in self.create_section_items],
            "deferred_removals": list(self.deferred_removals),
            "intended_paths": list(self.intended_paths),
            "actions": [asdict(action) for action in self.actions],
        }

    def to_dict(self) -> dict:
        return {**self._unsigned(), "plan_hash": self.plan_hash}

    @classmethod
    def from_dict(cls, payload: dict) -> TopologyPlan:
        expected = {
            "schema_version",
            "path_updates",
            "renames",
            "create_sections",
            "deferred_removals",
            "intended_paths",
            "actions",
            "plan_hash",
        }
        if not isinstance(payload, dict) or set(payload) != expected:
            raise TopologyPlanError("topology plan has an unknown shape")
        if payload["schema_version"] != _TOPOLOGY_PLAN_SCHEMA:
            raise TopologyPlanError("topology plan schema is unsupported")

        def pairs(field: str) -> tuple[tuple, ...]:
            value = payload[field]
            if not isinstance(value, list) or any(
                not isinstance(item, list) or len(item) != 2 for item in value
            ):
                raise TopologyPlanError(f"topology {field} is invalid")
            return tuple(tuple(item) for item in value)

        actions_raw = payload["actions"]
        if not isinstance(actions_raw, list):
            raise TopologyPlanError("topology actions are invalid")
        actions: list[PlexAction] = []
        for raw in actions_raw:
            if not isinstance(raw, dict) or set(raw) != {"kind", "key", "current", "proposed"}:
                raise TopologyPlanError("topology action is invalid")
            actions.append(PlexAction(**raw))
        deferred = payload["deferred_removals"]
        intended = payload["intended_paths"]
        if not isinstance(deferred, list) or any(
            isinstance(key, bool) or not isinstance(key, int) or key < 1 for key in deferred
        ):
            raise TopologyPlanError("topology deferred removals are invalid")
        if not isinstance(intended, list):
            raise TopologyPlanError("topology intended paths are invalid")
        intended_paths = tuple(_absolute_plex_path(path, "intended path") for path in intended)
        plan_hash = payload["plan_hash"]
        if (
            not isinstance(plan_hash, str)
            or len(plan_hash) != 64
            or any(character not in "0123456789abcdef" for character in plan_hash)
        ):
            raise TopologyPlanError("topology plan hash is invalid")
        plan = cls(
            path_update_items=pairs("path_updates"),
            rename_items=pairs("renames"),
            create_section_items=pairs("create_sections"),
            deferred_removals=tuple(deferred),
            intended_paths=intended_paths,
            actions=tuple(actions),
            plan_hash=plan_hash,
        )
        expected_hash = hashlib.sha256(
            json.dumps(plan._unsigned(), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if expected_hash != plan.plan_hash:
            raise TopologyPlanError("topology plan hash does not match its content")
        return plan


@dataclass(frozen=True, slots=True)
class TopologyRemovalProof:
    plan_hash: str
    scans_succeeded: bool
    first_observed_at: datetime
    first_counts: dict[str, int]
    second_observed_at: datetime
    second_counts: dict[str, int]
    playback_http_status: int
    playback_bytes: int
    unmatched_required_media: int
    invincible_tv_seasons: tuple[int, ...]
    invincible_anime_matches: int

    def to_dict(self) -> dict:
        return asdict(self)

    def validate(self, *, expected_plan_hash: str | None = None) -> None:
        if (
            len(self.plan_hash) != 64
            or any(character not in "0123456789abcdef" for character in self.plan_hash)
            or (expected_plan_hash is not None and self.plan_hash != expected_plan_hash)
        ):
            raise TopologyPlanError("removal proof does not match the immutable plan hash")
        if not self.scans_succeeded:
            raise TopologyPlanError("replacement scans did not succeed")
        if self.first_observed_at.tzinfo is None or self.second_observed_at.tzinfo is None:
            raise TopologyPlanError("count observations must be timezone-aware")
        if self.second_observed_at - self.first_observed_at < timedelta(minutes=5):
            raise TopologyPlanError("stable count observations must be at least five minutes apart")
        if self.first_counts != self.second_counts:
            raise TopologyPlanError("Plex section counts are not stable")
        if set(self.first_counts) != set(_SECTION_NAMES) or any(
            isinstance(count, bool) or not isinstance(count, int) or count < 0
            for count in self.first_counts.values()
        ):
            raise TopologyPlanError("stable counts do not describe the exact four sections")
        if self.playback_http_status != 206 or self.playback_bytes < 1:
            raise TopologyPlanError("representative playback did not return bytes with HTTP 206")
        if self.unmatched_required_media != 0:
            raise TopologyPlanError("requested-title matrix still has unmatched required media")
        if self.invincible_tv_seasons != (1, 2, 3, 4) or self.invincible_anime_matches != 0:
            raise TopologyPlanError("Invincible placement is not proven safe")


@dataclass(frozen=True, slots=True)
class PlexMutation:
    kind: str
    key: int | None
    proposed: str
    dry_run: bool


@dataclass(frozen=True, slots=True)
class PlexAnimeItem:
    rating_key: str
    title: str
    genres: tuple[str, ...]
    collections: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PlexLibraryScan:
    title: str
    subtitle: str
    progress: str


def read_plex_token(preferences_xml: Path) -> str:
    """Read PlexOnlineToken without ever including the value in a URL or repr."""
    try:
        root = ElementTree.parse(preferences_xml).getroot()
    except (OSError, ElementTree.ParseError) as exc:
        raise TopologyPlanError("Plex Preferences.xml is unavailable or invalid") from exc
    token = root.attrib.get("PlexOnlineToken", "")
    if not token or len(token) > 512 or any(character.isspace() for character in token):
        raise TopologyPlanError("Plex token is missing or invalid")
    return token


def validate_backup_receipt(path: Path) -> dict:
    """Require hashed, readable CT111 rollback artifacts before Plex mutation."""
    try:
        receipt = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TopologyPlanError("Plex backup receipt is unavailable or invalid") from exc
    if not isinstance(receipt, dict) or set(receipt) != {
        "schema_version",
        "created_at",
        "artifacts",
    }:
        raise TopologyPlanError("Plex backup receipt has an unknown shape")
    if receipt["schema_version"] != "plex-backup-receipt-v1":
        raise TopologyPlanError("Plex backup receipt schema is unsupported")
    created_at = receipt["created_at"]
    if not isinstance(created_at, str):
        raise TopologyPlanError("Plex backup timestamp is invalid")
    try:
        datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise TopologyPlanError("Plex backup timestamp is invalid") from exc
    artifacts = receipt["artifacts"]
    if not isinstance(artifacts, dict) or set(artifacts) != _BACKUP_KINDS:
        raise TopologyPlanError("Plex backup receipt is incomplete")
    for artifact in artifacts.values():
        if not isinstance(artifact, dict) or set(artifact) != {"path", "sha256"}:
            raise TopologyPlanError("Plex backup artifact is invalid")
        artifact_path = Path(artifact["path"])
        digest = artifact["sha256"]
        if (
            not artifact_path.is_absolute()
            or not artifact_path.is_file()
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise TopologyPlanError("Plex backup artifact is unavailable or invalid")
        calculated = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        if calculated != digest:
            raise TopologyPlanError("Plex backup artifact hash does not match")
    return receipt


def anime_bucket(
    genres: Iterable[str],
    *,
    identity: str = "",
    overrides: dict[str, str] | None = None,
) -> str:
    """Select one genre bucket; specific settings precede broad action overlap."""
    override = (overrides or {}).get(identity)
    if override is not None:
        if override not in GENRE_COLLECTIONS:
            raise TopologyPlanError("collection override is not a managed bucket")
        return override
    normalized = {str(genre).strip().casefold() for genre in genres if str(genre).strip()}
    for signals, collection in (
        ({"sci-fi", "sci fi", "science fiction", "mecha"}, SCIFI_COLLECTION),
        ({"fantasy"}, FANTASY_COLLECTION),
        ({"action", "adventure"}, ACTION_COLLECTION),
        ({"drama", "mystery", "psychological", "thriller"}, DRAMA_COLLECTION),
        ({"comedy", "slice of life"}, COMEDY_COLLECTION),
    ):
        if normalized & signals:
            return collection
    raise TopologyPlanError("anime collection identity is unresolved")


def managed_collection_tags(
    existing: Iterable[str], desired: str | Iterable[str]
) -> tuple[str, ...]:
    """Converge managed tags while preserving every unrelated user collection.

    A legacy single string updates its own genre/shelf axis only. An iterable
    describes the complete managed state, including an explicitly empty shelf.
    """
    existing_tags = tuple(dict.fromkeys(existing))
    if isinstance(desired, str):
        if desired not in MANAGED_COLLECTIONS:
            raise TopologyPlanError("desired collection is not managed")
        other_axis = SHELF_COLLECTIONS if desired in GENRE_COLLECTIONS else GENRE_COLLECTIONS
        desired_tags = {desired} | (set(existing_tags) & other_axis)
    else:
        desired_tags = set(desired)
    if not desired_tags <= MANAGED_COLLECTIONS:
        raise TopologyPlanError("desired collection is not managed")
    if len(desired_tags & GENRE_COLLECTIONS) > 1:
        raise TopologyPlanError("multiple genre buckets are ambiguous")
    preserved = tuple(tag for tag in existing_tags if tag not in MANAGED_COLLECTIONS)
    managed = tuple(tag for tag in _MANAGED_TAG_ORDER if tag in desired_tags)
    return preserved + managed


def _absolute_plex_path(path: str, field: str) -> str:
    if not isinstance(path, str) or not path.startswith("/") or path.endswith("/"):
        raise TopologyPlanError(f"{field} must be one canonical absolute Plex path")
    if "/../" in f"{path}/" or "/./" in f"{path}/":
        raise TopologyPlanError(f"{field} must be one canonical absolute Plex path")
    return path


def plan_library_topology(
    sections: tuple[PlexSection, ...],
    *,
    current_overlay: str,
    riven_movies: str = "/library/symlinks/movies",
    library_names: PlexLibraryNames = PlexLibraryNames(),
) -> TopologyPlan:
    """Discover configured libraries by name and produce a key-independent proposal."""
    current_overlay = _absolute_plex_path(current_overlay, "overlay pointer")
    riven_movies = _absolute_plex_path(riven_movies, "Riven movies path")
    by_key = {section.key: section for section in sections}
    if len(by_key) != len(sections):
        raise TopologyPlanError("duplicate Plex section key")
    by_title = {section.title.casefold(): section for section in sections}
    if len(by_title) != len(sections):
        raise TopologyPlanError("duplicate Plex section title")

    def configured(title: str, role: str) -> PlexSection:
        section = by_title.get(title.casefold())
        if section is None:
            raise TopologyPlanError(f"configured {role} library is absent")
        return section

    anime = configured(library_names.anime, "anime")
    anime_movies = configured(library_names.anime_movies, "anime movies")
    tv = by_title.get(library_names.tv.casefold())
    if tv is None and library_names.tv == "TV Shows":
        tv = by_title.get("riven tv")
    if tv is None:
        raise TopologyPlanError("configured TV library is absent")
    if len(tv.paths) != 1:
        raise TopologyPlanError("configured TV library must expose one source path")
    tv_path = _absolute_plex_path(tv.paths[0], "Riven shows path")

    anime_path = f"{current_overlay}/Anime"
    anime_movies_path = f"{current_overlay}/Anime Movies"
    updates: list[tuple[int, str]] = []
    for section, proposed in ((anime, anime_path), (anime_movies, anime_movies_path)):
        if section.paths != (proposed,):
            updates.append((section.key, proposed))
    movies = by_title.get(library_names.movies.casefold())
    creates: tuple[tuple[str, str], ...]
    if movies is not None:
        if movies.paths != (riven_movies,):
            updates.append((movies.key, riven_movies))
        creates = ()
    else:
        creates = ((library_names.movies, riven_movies),)

    renames = ((tv.key, library_names.tv),) if tv.title != library_names.tv else ()
    legacy_debrid = by_title.get("real-debrid")
    deferred = (legacy_debrid.key,) if legacy_debrid is not None else ()
    actions = tuple(
        [
            PlexAction(
                "update_path",
                key,
                by_key[key].paths[0] if by_key[key].paths else "",
                path,
            )
            for key, path in updates
        ]
        + [PlexAction("rename_section", key, by_key[key].title, title) for key, title in renames]
        + [PlexAction("create_section", None, "", path) for _, path in creates]
        + [PlexAction("defer_remove_section", key, by_key[key].title, "") for key in deferred]
    )
    intended_paths = (
        anime_path,
        anime_movies_path,
        tv_path,
        riven_movies,
    )
    provisional = TopologyPlan(
        path_update_items=tuple(updates),
        rename_items=renames,
        create_section_items=creates,
        deferred_removals=deferred,
        intended_paths=intended_paths,
        actions=actions,
        plan_hash="",
    )
    plan_hash = hashlib.sha256(
        json.dumps(provisional._unsigned(), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return TopologyPlan.from_dict({**provisional._unsigned(), "plan_hash": plan_hash})


def managed_path_reference(path: str, overlay_path: str) -> tuple[dict | None, bool]:
    """Keep managed presence evidence even when native metadata is ambiguous."""
    if (
        not path.startswith("/")
        or path != PurePosixPath(path).as_posix()
        or any(part in {".", ".."} for part in path.split("/"))
        or any(ord(char) < 32 for char in path)
    ):
        return None, False
    try:
        parts = PurePosixPath(path).relative_to(PurePosixPath(overlay_path, "current")).parts
    except ValueError:
        return None, True
    if len(parts) == 3 and parts[0] == "Anime Movies" and parts[2].startswith(parts[1] + " - "):
        return {"canonical_title": parts[1], "season_number": 0}, True
    if len(parts) != 4 or parts[0] != "Anime":
        return None, False
    season = re.fullmatch(r"Season (\d{2})", parts[2])
    if not season:
        return None, False
    number = int(season[1])
    file = re.fullmatch(
        re.escape(parts[1])
        + rf" - S{number:02d}E(\d{{2,4}})(?:-E(\d{{2,4}}))?\.[a-zA-Z0-9]{{1,8}}",
        parts[3],
    )
    if not file or not 1 <= int(file[1]) <= int(file[2] or file[1]) <= 5000:
        return None, False
    return {"canonical_title": parts[1], "season_number": number}, True


def managed_media_title(
    path: str, *, overlay_path: str, section: str, season: int, episode: int
) -> str | None:
    """Bind a native item to the exact normalized managed folder and season."""
    if not path.startswith("/") or any(part in {".", ".."} for part in path.split("/")):
        return None
    try:
        parts = (
            PurePosixPath(path).relative_to(PurePosixPath(overlay_path, "current", section)).parts
        )
    except ValueError:
        return None
    if section == "Anime Movies":
        if (
            season == 0
            and episode == 1
            and len(parts) == 2
            and parts[1].startswith(parts[0] + " - ")
        ):
            return parts[0]
        return None
    if section != "Anime" or len(parts) != 3 or parts[1] != f"Season {season:02d}":
        return None
    match = re.fullmatch(
        re.escape(parts[0]) + rf" - S{season:02d}E(?P<start>\d{{2,4}})"
        r"(?:-E(?P<end>\d{2,4}))?\.[a-zA-Z0-9]{1,8}",
        parts[2],
    )
    if match and int(match["start"]) <= episode <= int(match["end"] or match["start"]):
        return parts[0]
    return None


def _require_complete_inventory(root: ElementTree.Element) -> None:
    count = len(root.findall("Video")) + len(root.findall("Directory"))
    for key in ("size", "totalSize"):
        value = root.attrib.get(key)
        if value is not None and (not value.isdigit() or int(value) != count):
            raise TopologyPlanError("Plex inventory response is incomplete")
    if root.attrib.get("offset", "0") != "0":
        raise TopologyPlanError("Plex inventory response is incomplete")


class PlexClient:
    """Small Plex API surface with no media-deletion operation."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        if not base_url.startswith(("http://", "https://")) or not token:
            raise TopologyPlanError("Plex base URL or token is invalid")
        self._base_url = base_url.rstrip("/")
        self._episode_paths: dict[str, tuple[tuple[int, int, str], ...]] = {}
        self._client = httpx.Client(
            base_url=self._base_url,
            headers={"Accept": "application/xml", "X-Plex-Token": token},
            timeout=timeout,
            transport=transport,
        )

    def __repr__(self) -> str:
        return f"PlexClient(base_url={self._base_url!r}, token=<redacted>)"

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        response = self._client.request(method, path, **kwargs)
        response.raise_for_status()
        return response

    def inventory_sections(self) -> tuple[PlexSection, ...]:
        root = ElementTree.fromstring(self._request("GET", "/library/sections").text)
        sections: list[PlexSection] = []
        for directory in root.findall("Directory"):
            locations = tuple(
                item.attrib["path"]
                for item in directory.findall("Location")
                if item.attrib.get("path")
            )
            sections.append(
                PlexSection(
                    key=int(directory.attrib["key"]),
                    title=directory.attrib["title"],
                    paths=locations,
                )
            )
        return tuple(sections)

    def inventory_anime(self, section_key: int) -> tuple[PlexAnimeItem, ...]:
        root = ElementTree.fromstring(
            self._request("GET", f"/library/sections/{section_key}/all").text
        )
        items: list[PlexAnimeItem] = []
        for element in (*root.findall("Directory"), *root.findall("Video")):
            rating_key = element.attrib.get("ratingKey", "")
            title = element.attrib.get("title", "")
            if not rating_key.isdigit() or not title:
                raise TopologyPlanError("Plex anime metadata has no stable identity")
            items.append(
                PlexAnimeItem(
                    rating_key=rating_key,
                    title=title,
                    genres=tuple(
                        genre.attrib["tag"]
                        for genre in element.findall("Genre")
                        if genre.attrib.get("tag")
                    ),
                    collections=tuple(
                        collection.attrib["tag"]
                        for collection in element.findall("Collection")
                        if collection.attrib.get("tag")
                    ),
                )
            )
        return tuple(items)

    @staticmethod
    def _viewed_at(value: str) -> str | None:
        if not value:
            return None
        if not value.isdigit():
            raise TopologyPlanError("Plex last-viewed timestamp is invalid")
        return datetime.fromtimestamp(int(value), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def library_snapshot(
        self,
        *,
        library_names: PlexLibraryNames,
        generated_at: str,
        overlay_path: str | None = None,
    ) -> dict:
        """Read the complete anime catalog and owner progress without mutation."""
        try:
            datetime.strptime(generated_at, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError as exc:
            raise TopologyPlanError("snapshot timestamp is invalid") from exc
        sections = {section.title.casefold(): section for section in self.inventory_sections()}
        items: list[dict] = []
        for title, media_type in (
            (library_names.anime, "show"),
            (library_names.anime_movies, "movie"),
        ):
            section = sections.get(title.casefold())
            if section is None:
                raise TopologyPlanError(f"configured Plex {title} section is absent")
            root = ElementTree.fromstring(
                self._request("GET", f"/library/sections/{section.key}/all").text
            )
            if overlay_path is not None:
                _require_complete_inventory(root)
            if media_type == "movie":
                for video in root.findall("Video"):
                    rating_key = video.attrib.get("ratingKey", "")
                    item_title = video.attrib.get("title", "")
                    if not rating_key.isdigit() or not item_title:
                        raise TopologyPlanError("Plex movie metadata has no stable identity")
                    watched = 1 if int(video.attrib.get("viewCount", "0") or 0) > 0 else 0
                    item = {
                        "rating_key": rating_key,
                        "section": section.title,
                        "media_type": "movie",
                        "title": item_title,
                        "season_number": 0,
                        "episode_total": 1,
                        "watched_through": watched,
                        "last_viewed_at": self._viewed_at(video.attrib.get("lastViewedAt", "")),
                    }
                    if overlay_path is not None:
                        self._bind_managed_title(item, video.findall("./Media/Part"), overlay_path)
                    items.append(item)
                continue
            for directory in root.findall("Directory"):
                rating_key = directory.attrib.get("ratingKey", "")
                item_title = directory.attrib.get("title", "")
                if not rating_key.isdigit() or not item_title:
                    raise TopologyPlanError("Plex show metadata has no stable identity")
                leaves = ElementTree.fromstring(
                    self._request("GET", f"/library/metadata/{rating_key}/allLeaves").text
                )
                if overlay_path is not None:
                    _require_complete_inventory(leaves)
                seasons: dict[int, list[ElementTree.Element]] = {}
                for episode in leaves.findall("Video"):
                    season = episode.attrib.get("parentIndex", "")
                    index = episode.attrib.get("index", "")
                    if not season.isdigit() or not index.isdigit():
                        raise TopologyPlanError("Plex episode has no stable season identity")
                    season_number = int(season)
                    if season_number == 0:
                        if overlay_path is not None:
                            parts = episode.findall("./Media/Part")
                            if not parts:
                                raise TopologyPlanError("Plex special media paths are unavailable")
                            for part in parts:
                                ref, complete = managed_path_reference(
                                    part.attrib.get("file", ""), overlay_path
                                )
                                if not complete or (ref and ref["season_number"] != 0):
                                    raise TopologyPlanError(
                                        "Plex regular episode is ambiguously indexed as a special"
                                    )
                        continue
                    seasons.setdefault(season_number, []).append(episode)
                for season_number, episodes in seasons.items():
                    episode_total = max(int(episode.attrib["index"]) for episode in episodes)
                    viewed = [
                        episode
                        for episode in episodes
                        if int(episode.attrib.get("viewCount", "0") or 0) > 0
                    ]
                    watched_through = max(
                        (int(episode.attrib["index"]) for episode in viewed), default=0
                    )
                    viewed_at_values = [
                        value
                        for episode in viewed
                        if (value := self._viewed_at(episode.attrib.get("lastViewedAt", "")))
                    ]
                    item = {
                        "rating_key": rating_key,
                        "section": section.title,
                        "media_type": "show",
                        "title": item_title,
                        "season_number": season_number,
                        "episode_total": episode_total,
                        "watched_through": watched_through,
                        "last_viewed_at": max(viewed_at_values, default=None),
                    }
                    if overlay_path is not None:
                        identities: set[str | None] = set()
                        evidence = []
                        for episode in episodes:
                            parts = episode.findall("./Media/Part")
                            evidence.append(parts)
                            if not parts:
                                identities.add(None)
                            for part in parts:
                                identities.add(
                                    managed_media_title(
                                        part.attrib.get("file", ""),
                                        overlay_path=overlay_path,
                                        section="Anime",
                                        season=season_number,
                                        episode=int(episode.attrib["index"]),
                                    )
                                )
                        if len(identities) == 1 and None not in identities:
                            item["canonical_title"] = identities.pop()
                        self._bind_path_evidence(item, evidence, overlay_path)
                    items.append(item)
        items.sort(
            key=lambda item: (
                item["section"].casefold(),
                item["title"].casefold(),
                item["season_number"],
            )
        )
        return {
            "schema_version": "plex-library-snapshot-v1",
            "generated_at": generated_at,
            "items": items,
        }

    @staticmethod
    def _bind_managed_title(item: dict, parts: list, overlay_path: str) -> None:
        identities = {
            managed_media_title(
                part.attrib.get("file", ""),
                overlay_path=overlay_path,
                section="Anime Movies",
                season=0,
                episode=1,
            )
            for part in parts
        }
        if len(identities) == 1 and None not in identities:
            item["canonical_title"] = identities.pop()
        PlexClient._bind_path_evidence(item, [parts], overlay_path)

    @staticmethod
    def _bind_path_evidence(item: dict, groups: list, overlay_path: str) -> None:
        refs: set[tuple[str, int]] = set()
        complete = True
        for parts in groups:
            if not parts:
                complete = False
            for part in parts:
                reference, valid = managed_path_reference(part.attrib.get("file", ""), overlay_path)
                complete = complete and valid
                if reference:
                    refs.add((reference["canonical_title"], reference["season_number"]))
        item["managed_paths"] = [
            {"canonical_title": title, "season_number": season} for title, season in sorted(refs)
        ]
        item["path_identity_complete"] = complete
        if not complete:
            item.pop("canonical_title", None)

    def season_episode_paths(self, rating_key: str, season: int) -> tuple[tuple[int, str], ...]:
        """Read native episode indices and paths once per show for this client."""
        if not rating_key.isdigit() or type(season) is not int or not 1 <= season <= 99:
            raise TopologyPlanError("Plex season identity is invalid")
        if rating_key not in self._episode_paths:
            root = ElementTree.fromstring(
                self._request("GET", f"/library/metadata/{rating_key}/allLeaves").text
            )
            _require_complete_inventory(root)
            paths = set()
            for video in root.findall("Video"):
                parent = video.attrib.get("parentIndex", "")
                index = video.attrib.get("index", "")
                if not parent.isdigit() or not index.isdigit():
                    raise TopologyPlanError("Plex episode has no stable season identity")
                for part in video.findall("./Media/Part"):
                    path = part.attrib.get("file", "")
                    if path.startswith("/"):
                        paths.add((int(parent), int(index), path))
            self._episode_paths[rating_key] = tuple(sorted(paths))
        return tuple(
            (index, path)
            for parent, index, path in self._episode_paths[rating_key]
            if parent == season
        )

    def section_count(self, section_key: int) -> int:
        root = ElementTree.fromstring(
            self._request("GET", f"/library/sections/{section_key}/all").text
        )
        raw_size = root.attrib.get("size")
        if raw_size is not None:
            if not raw_size.isdigit():
                raise TopologyPlanError("Plex section count is invalid")
            return int(raw_size)
        return len(root.findall("Directory")) + len(root.findall("Video"))

    def active_library_scans(self) -> tuple[PlexLibraryScan, ...]:
        root = ElementTree.fromstring(self._request("GET", "/activities").text)
        scans: list[PlexLibraryScan] = []
        for activity in root:
            if activity.attrib.get("type") != "library.update.section":
                continue
            scans.append(
                PlexLibraryScan(
                    title=activity.attrib.get("title", "Plex library scan"),
                    subtitle=activity.attrib.get("subtitle", ""),
                    progress=activity.attrib.get("progress", ""),
                )
            )
        return tuple(scans)

    def episode_count(self, rating_key: str) -> int:
        if not rating_key.isdigit():
            raise TopologyPlanError("Plex rating key is invalid")
        root = ElementTree.fromstring(
            self._request("GET", f"/library/metadata/{rating_key}/allLeaves").text
        )
        raw_size = root.attrib.get("size")
        if raw_size is not None:
            if not raw_size.isdigit():
                raise TopologyPlanError("Plex episode count is invalid")
            return int(raw_size)
        return len(root.findall("Video"))

    def series_media_paths(self, rating_key: str) -> tuple[str, ...]:
        if not rating_key.isdigit():
            raise TopologyPlanError("Plex rating key is invalid")
        root = ElementTree.fromstring(
            self._request("GET", f"/library/metadata/{rating_key}/allLeaves").text
        )
        paths = {
            part.attrib["file"]
            for part in root.findall("./Video/Media/Part")
            if part.attrib.get("file")
        }
        if not paths or any(not path.startswith("/") for path in paths):
            raise TopologyPlanError("Plex series media paths are unavailable or invalid")
        return tuple(sorted(paths))

    def item_media_paths(self, rating_key: str) -> tuple[str, ...]:
        """Return the exact files Plex attached to one movie or other library item."""
        if not rating_key.isdigit():
            raise TopologyPlanError("Plex rating key is invalid")
        root = ElementTree.fromstring(self._request("GET", f"/library/metadata/{rating_key}").text)
        paths = {
            part.attrib["file"]
            for part in root.findall("./Video/Media/Part")
            if part.attrib.get("file")
        }
        if not paths or any(not path.startswith("/") for path in paths):
            raise TopologyPlanError("Plex item media paths are unavailable or invalid")
        return tuple(sorted(paths))

    def season_episode_count(self, rating_key: str, season: int) -> int:
        if (
            not rating_key.isdigit()
            or isinstance(season, bool)
            or not isinstance(season, int)
            or season < 0
        ):
            raise TopologyPlanError("Plex season identity is invalid")
        root = ElementTree.fromstring(
            self._request("GET", f"/library/metadata/{rating_key}/allLeaves").text
        )
        count = 0
        for episode in root.findall("Video"):
            parent_index = episode.attrib.get("parentIndex", "")
            if not parent_index.isdigit():
                raise TopologyPlanError("Plex episode has no stable season identity")
            if int(parent_index) == season:
                count += 1
        return count

    def converge_collections(
        self,
        section_key: int,
        *,
        rich_genres: dict[str, tuple[str, ...]] | None = None,
        overrides: dict[str, str] | None = None,
        shelves: dict[str, Iterable[str]] | None = None,
        dry_run: bool,
    ) -> tuple[PlexMutation, ...]:
        mutations: list[PlexMutation] = []
        for item in self.inventory_anime(section_key):
            identity_override = (overrides or {}).get(item.title)
            if identity_override is not None:
                genres: Iterable[str] = ()
            elif rich_genres is not None and item.title in rich_genres:
                genres = rich_genres[item.title]
            else:
                raise TopologyPlanError("anime collection identity is unresolved")
            desired = anime_bucket(genres, identity=item.title, overrides=overrides)
            selected: str | tuple[str, ...] = desired
            if shelves is not None and item.title in shelves:
                shelf_tags = tuple(shelves.get(item.title, ()))
                if not set(shelf_tags) <= SHELF_COLLECTIONS:
                    raise TopologyPlanError("desired shelf is not managed")
                selected = (desired, *shelf_tags)
            tags = managed_collection_tags(item.collections, selected)
            if set(tags) == set(item.collections):
                continue
            mutations.append(
                self.set_managed_collection(
                    section_key=section_key,
                    rating_key=item.rating_key,
                    existing=item.collections,
                    desired=selected,
                    dry_run=dry_run,
                )
            )
        return tuple(mutations)

    def set_managed_collection(
        self,
        *,
        section_key: int,
        rating_key: str,
        existing: Iterable[str],
        desired: str | Iterable[str],
        dry_run: bool = False,
    ) -> PlexMutation:
        existing_tags = tuple(dict.fromkeys(existing))
        desired_tags = managed_collection_tags(existing_tags, desired)
        removals = [
            tag for tag in existing_tags if tag in MANAGED_COLLECTIONS and tag not in desired_tags
        ]
        additions = [
            tag for tag in desired_tags if tag in MANAGED_COLLECTIONS and tag not in existing_tags
        ]
        proposed = (
            desired
            if isinstance(desired, str)
            else ", ".join(tag for tag in desired_tags if tag in MANAGED_COLLECTIONS)
        )
        mutation = PlexMutation("set_collection", int(rating_key), proposed, dry_run)
        if not dry_run and (removals or additions):
            params = [
                ("type", "2"),
                ("id", str(int(rating_key))),
                ("collection.locked", "1"),
            ]
            if removals:
                params.append(("collection[].tag.tag-", ",".join(removals)))
            for index, tag in enumerate(additions):
                params.append((f"collection[{index}].tag.tag", tag))
            self._request(
                "PUT",
                f"/library/sections/{int(section_key)}/all",
                params=params,
            )
        return mutation

    def _section_edit_params(
        self,
        key: int,
        *,
        name: str | None = None,
        locations: Iterable[str] | None = None,
    ) -> list[tuple[str, str]]:
        root = ElementTree.fromstring(self._request("GET", "/library/sections").text)
        section = next(
            (item for item in root.findall("Directory") if item.attrib.get("key") == str(key)),
            None,
        )
        if section is None:
            raise TopologyPlanError(f"Plex section {key} is missing")
        required = {
            field: section.attrib.get(field, "")
            for field in ("title", "agent", "scanner", "language")
        }
        if not all(required.values()):
            raise TopologyPlanError(f"Plex section {key} has incomplete edit metadata")
        desired_locations = tuple(
            locations
            if locations is not None
            else (
                item.attrib["path"]
                for item in section.findall("Location")
                if item.attrib.get("path")
            )
        )
        if not desired_locations:
            raise TopologyPlanError(f"Plex section {key} has no location")
        return [
            ("name", name or required["title"]),
            ("agent", required["agent"]),
            ("scanner", required["scanner"]),
            ("language", required["language"]),
            *(("location", location) for location in desired_locations),
        ]

    def update_section_path(self, key: int, path: str, *, dry_run: bool = False) -> PlexMutation:
        path = _absolute_plex_path(path, "section path")
        mutation = PlexMutation("update_path", key, path, dry_run)
        if not dry_run:
            self._request(
                "PUT",
                f"/library/sections/{key}",
                params=self._section_edit_params(key, locations=(path,)),
            )
        return mutation

    def rename_section(self, key: int, title: str, *, dry_run: bool = False) -> PlexMutation:
        if title not in _SECTION_NAMES:
            raise TopologyPlanError("section rename is not an intended library name")
        mutation = PlexMutation("rename_section", key, title, dry_run)
        if not dry_run:
            self._request(
                "PUT",
                f"/library/sections/{key}",
                params=self._section_edit_params(key, name=title),
            )
        return mutation

    def create_section(self, title: str, path: str, *, dry_run: bool = False) -> PlexMutation:
        if title != "Movies":
            raise TopologyPlanError("only the Movies section may be created")
        path = _absolute_plex_path(path, "Movies path")
        mutation = PlexMutation("create_section", None, path, dry_run)
        if not dry_run:
            self._request(
                "POST",
                "/library/sections",
                params=[
                    ("name", title),
                    ("type", "movie"),
                    ("agent", "tv.plex.agents.movie"),
                    ("scanner", "Plex Movie"),
                    ("language", "en-US"),
                    ("location", path),
                ],
            )
        return mutation

    def scan_section(
        self,
        key: int,
        *,
        path: str | None = None,
        dry_run: bool = False,
    ) -> PlexMutation:
        if path is not None:
            path = _absolute_plex_path(path, "partial scan path")
        mutation = PlexMutation("scan_section", key, path or "", dry_run)
        if not dry_run:
            self._request(
                "POST",
                f"/library/sections/{key}/refresh",
                params={
                    "force": "1",
                    **({"path": path} if path is not None else {}),
                },
            )
        return mutation

    def remove_section(self, key: int, *, dry_run: bool = False) -> PlexMutation:
        mutation = PlexMutation("remove_section", key, "", dry_run)
        if not dry_run:
            self._request("DELETE", f"/library/sections/{key}")
        return mutation

    def apply_topology(
        self,
        plan: TopologyPlan,
        *,
        backup_receipt: Path,
        readable_paths: Iterable[str],
        dry_run: bool,
        removal_proof: TopologyRemovalProof | None = None,
    ) -> tuple[PlexMutation, ...]:
        plan = TopologyPlan.from_dict(plan.to_dict())
        validate_backup_receipt(backup_receipt)
        readable = set(readable_paths)
        if not set(plan.intended_paths) <= readable:
            raise TopologyPlanError("all four replacement paths must be readable in Plex")
        mutations: list[PlexMutation] = []
        for key, path in plan.path_update_items:
            mutations.append(self.update_section_path(key, path, dry_run=dry_run))
        for key, title in plan.rename_items:
            mutations.append(self.rename_section(key, title, dry_run=dry_run))
        for title, path in plan.create_section_items:
            mutations.append(self.create_section(title, path, dry_run=dry_run))
        if removal_proof is not None:
            removal_proof.validate(expected_plan_hash=plan.plan_hash)
            for key in plan.deferred_removals:
                mutations.append(self.remove_section(key, dry_run=dry_run))
        return tuple(mutations)

    def remove_deferred_sections(
        self,
        plan: TopologyPlan,
        proof: TopologyRemovalProof,
        *,
        dry_run: bool,
    ) -> tuple[PlexMutation, ...]:
        plan = TopologyPlan.from_dict(plan.to_dict())
        proof.validate(expected_plan_hash=plan.plan_hash)
        return tuple(self.remove_section(key, dry_run=dry_run) for key in plan.deferred_removals)
