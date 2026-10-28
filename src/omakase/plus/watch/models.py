"""Immutable inputs and outputs for watch-progress transitions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

WatchSource = Literal["plex", "apple_tv", "anilist", "manual", "migration"]
WatchEventKind = Literal["progress", "completed", "correction", "reopened"]
WatchConfidence = Literal["exact", "inferred", "manual"]
WatchStatus = Literal["unwatched", "watching", "completed"]
WatchDisposition = Literal["accepted", "ignored", "confirmation_required"]
TrackerAction = Literal["progress", "complete"]

_WATCH_SOURCES = frozenset({"plex", "apple_tv", "anilist", "manual", "migration"})
_WATCH_KINDS = frozenset({"progress", "completed", "correction", "reopened"})
_WATCH_CONFIDENCE = frozenset({"exact", "inferred", "manual"})
_WATCH_STATUSES = frozenset({"unwatched", "watching", "completed"})


@dataclass(frozen=True)
class WatchEntry:
    """Current projection for one owner's AniList title and season."""

    anilist_id: int
    season_number: int
    status: WatchStatus = "unwatched"
    progress_episodes: int = 0
    episode_total: int | None = None
    last_source: WatchSource = "manual"
    confidence: WatchConfidence = "manual"
    last_observed_at: str | None = None
    completed_at: str | None = None
    revision: int = 0
    id: int | None = None
    user_id: int | None = None
    monitor_id: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.anilist_id, int) or self.anilist_id <= 0:
            raise ValueError("anilist_id must be a positive integer")
        if not isinstance(self.season_number, int) or self.season_number < 0:
            raise ValueError("season_number must be a non-negative integer")
        if self.status not in _WATCH_STATUSES:
            raise ValueError("status is invalid")
        if not isinstance(self.progress_episodes, int) or self.progress_episodes < 0:
            raise ValueError("progress_episodes must be a non-negative integer")
        if self.episode_total is not None:
            if not isinstance(self.episode_total, int) or self.episode_total <= 0:
                raise ValueError("episode_total must be a positive integer")
            if self.progress_episodes > self.episode_total:
                raise ValueError("progress_episodes cannot exceed episode_total")
        if self.last_source not in _WATCH_SOURCES:
            raise ValueError("last_source is invalid")
        if self.confidence not in _WATCH_CONFIDENCE:
            raise ValueError("confidence is invalid")
        if not isinstance(self.revision, int) or self.revision < 0:
            raise ValueError("revision must be a non-negative integer")
        if self.status == "completed" and self.completed_at is None:
            raise ValueError("completed entries require completed_at")
        if self.status != "completed" and self.completed_at is not None:
            raise ValueError("non-completed entries cannot have completed_at")

    @classmethod
    def new(
        cls,
        *,
        anilist_id: int,
        season_number: int,
        episode_total: int | None = None,
        progress_episodes: int = 0,
        last_observed_at: str | None = None,
        completed_at: str | None = None,
        id: int | None = None,
        user_id: int | None = None,
        monitor_id: int | None = None,
    ) -> WatchEntry:
        """Build a valid projection from persisted identity and progress fields."""
        status: WatchStatus
        if completed_at is not None:
            status = "completed"
        elif progress_episodes > 0:
            status = "watching"
        else:
            status = "unwatched"
        return cls(
            anilist_id=anilist_id,
            season_number=season_number,
            status=status,
            progress_episodes=progress_episodes,
            episode_total=episode_total,
            last_observed_at=last_observed_at,
            completed_at=completed_at,
            id=id,
            user_id=user_id,
            monitor_id=monitor_id,
        )


@dataclass(frozen=True)
class WatchEvent:
    """Normalized playback or owner action before persistence."""

    source: WatchSource
    kind: WatchEventKind
    event_key: str
    episode: int | None = None
    playback_fraction: float | None = None
    confidence: WatchConfidence = "exact"
    force: bool = False

    def __post_init__(self) -> None:
        if self.source not in _WATCH_SOURCES:
            raise ValueError("source is invalid")
        if self.kind not in _WATCH_KINDS:
            raise ValueError("kind is invalid")
        if not isinstance(self.event_key, str) or not 1 <= len(self.event_key) <= 240:
            raise ValueError("event_key must contain between 1 and 240 characters")
        if self.confidence not in _WATCH_CONFIDENCE:
            raise ValueError("confidence is invalid")
        if self.episode is not None:
            if not isinstance(self.episode, int) or self.episode < 0:
                raise ValueError("episode must be a non-negative integer")
        if self.kind == "progress" and self.episode is None and self.confidence != "inferred":
            raise ValueError("progress episode is required unless identity is inferred")
        if self.kind == "progress" and self.episode is not None and self.episode <= 0:
            raise ValueError("progress episode must be a positive integer")
        if self.kind == "correction" and self.episode is None:
            raise ValueError("correction episode is required")
        if self.playback_fraction is not None:
            if isinstance(self.playback_fraction, bool) or not isinstance(
                self.playback_fraction, (int, float)
            ):
                raise ValueError("playback_fraction must be numeric")
            if not 0.0 <= float(self.playback_fraction) <= 1.0:
                raise ValueError("playback_fraction must be between zero and one")
        if self.force and self.source != "manual":
            raise ValueError("force is only valid for manual events")
        if self.kind in {"correction", "reopened"} and not self.force:
            raise ValueError(f"{self.kind} events require force")

    @classmethod
    def progress(
        cls,
        *,
        source: WatchSource,
        episode: int | None,
        event_key: str,
        playback_fraction: float | None = None,
        confidence: WatchConfidence = "exact",
    ) -> WatchEvent:
        return cls(
            source=source,
            kind="progress",
            event_key=event_key,
            episode=episode,
            playback_fraction=playback_fraction,
            confidence=confidence,
        )

    @classmethod
    def complete(
        cls,
        *,
        source: WatchSource,
        event_key: str,
        episode: int | None = None,
        confidence: WatchConfidence | None = None,
        force: bool = False,
    ) -> WatchEvent:
        resolved_confidence: WatchConfidence = confidence or (
            "manual" if source == "manual" else "exact"
        )
        return cls(
            source=source,
            kind="completed",
            event_key=event_key,
            episode=episode,
            confidence=resolved_confidence,
            force=force,
        )

    @classmethod
    def correction(cls, *, episode: int, event_key: str, force: bool) -> WatchEvent:
        return cls(
            source="manual",
            kind="correction",
            event_key=event_key,
            episode=episode,
            confidence="manual",
            force=force,
        )

    @classmethod
    def reopen(cls, *, episode: int, event_key: str, force: bool) -> WatchEvent:
        return cls(
            source="manual",
            kind="reopened",
            event_key=event_key,
            episode=episode,
            confidence="manual",
            force=force,
        )


@dataclass(frozen=True)
class WatchDecision:
    """Pure transition result ready for an atomic persistence boundary."""

    entry: WatchEntry
    disposition: WatchDisposition
    reason: str
    enqueue_tracker_action: TrackerAction | None


@dataclass(frozen=True)
class TrackerOutboxItem:
    """Leased AniList synchronization work."""

    id: int
    watch_entry_id: int
    watch_event_id: int
    user_id: int
    tracker: str
    action: TrackerAction
    progress_episodes: int
    sync_state: str
    attempt_count: int
    lease_token: str
    score_ten: int | None = None
