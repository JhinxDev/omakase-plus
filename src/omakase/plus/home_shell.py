"""Profile-scoped Home projection shared by web Home and the Apple TV client.

One server-side read model answers three questions for a signed-in profile:

* which single title leads the banner (most recently watched, playable),
* which shelves render, in what order, and which are hidden,
* which cards belong in each Continue Watching shelf.

Nothing here writes watch history. Watch timestamps come only from playback and
watch evidence: accepted watch events, saved playheads and explicit history
rows. Release dates, airing schedules and library sync receipts never enter the
sort key, so a sync cannot promote or demote a shelf card.

Row layout is stored per profile so web and Apple TV read one ordering. The
projection is JSON-serialisable on purpose: the native client consumes the same
dict the web template renders.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

MAX_ROWS = 12
MAX_CARD_TITLE = 200

#: Canonical shelf catalogue. ``default`` marks the three Continue Watching
#: shelves; everything else is opt-in from Edit Home.
HOME_ROW_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "id": "continue-anime",
        "label": "Continue Watching Anime",
        "kind": "continue",
        "domain": "anime",
        "default": True,
        "description": "Resume anime you are part way through.",
    },
    {
        "id": "continue-movies",
        "label": "Continue Watching Movies",
        "kind": "continue",
        "domain": "movie",
        "default": True,
        "description": "Movies with a saved playhead.",
    },
    {
        "id": "continue-tv",
        "label": "Continue Watching TV Shows",
        "kind": "continue",
        "domain": "tv",
        "default": True,
        "description": "Resume an episode or play the next one.",
    },
    {
        "id": "watchlist",
        "label": "Watchlist",
        "kind": "watchlist",
        "domain": "anime",
        "default": False,
        "description": "Saved titles waiting to be added.",
    },
    {
        "id": "ready-to-watch",
        "label": "Ready to Watch",
        "kind": "ready",
        "domain": "anime",
        "default": False,
        "description": "Titles verified playable but not started.",
    },
    {
        "id": "recently-watched",
        "label": "Recently Watched",
        "kind": "recent",
        "domain": "all",
        "default": False,
        "description": "The latest accepted watch activity.",
    },
)

ROWS_BY_ID = {row["id"]: row for row in HOME_ROW_CATALOG}
DEFAULT_LAYOUT_IDS: tuple[str, ...] = tuple(row["id"] for row in HOME_ROW_CATALOG if row["default"])
_REMOVED_ROW_IDS = frozenset({"new-episodes", "continue-watching", "for-you"})
_RESUME_FIELD_LIMIT = 4096
_ISO_HINT = re.compile(r"^\d{4}-\d{2}-\d{2}")


class HomeLayoutError(ValueError):
    """A layout submission named an unknown shelf or an impossible order."""


# ---------------------------------------------------------------------------
# Layout persistence
# ---------------------------------------------------------------------------


def default_layout() -> list[dict[str, Any]]:
    return [{"id": row_id, "hidden": False} for row_id in DEFAULT_LAYOUT_IDS]


def normalize_layout(raw: Any) -> list[dict[str, Any]]:
    """Return a complete, deduplicated layout in submission order.

    Unknown and retired shelf ids are dropped rather than rejected so a stored
    layout survives a catalogue change. Shelves the profile has never placed are
    appended once (defaults first) so new defaults still appear.
    """
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in _layout_entries(raw):
        row_id = entry.get("id")
        if not isinstance(row_id, str) or row_id in seen or row_id not in ROWS_BY_ID:
            continue
        seen.add(row_id)
        ordered.append({"id": row_id, "hidden": bool(entry.get("hidden"))})
    for row_id in DEFAULT_LAYOUT_IDS:
        if row_id not in seen:
            seen.add(row_id)
            ordered.append({"id": row_id, "hidden": False})
    for row in HOME_ROW_CATALOG:
        if row["id"] not in seen:
            ordered.append({"id": row["id"], "hidden": True})
    return ordered[:MAX_ROWS]


def _layout_entries(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, str):
        if len(raw) > _RESUME_FIELD_LIMIT:
            return []
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return []
    if not isinstance(raw, list):
        return []
    return [entry for entry in raw if isinstance(entry, dict)]


def load_layout(db, user_id: int) -> dict[str, Any]:
    """Read the stored layout, or the shipped default when none is saved."""
    row = db.execute(
        "SELECT rows_json, revision, updated_at FROM viewer_home_layout WHERE user_id=?",
        (user_id,),
    ).fetchone()
    if row is None:
        return {"rows": default_layout(), "revision": 0, "updated_at": "", "stored": False}
    return {
        "rows": normalize_layout(row["rows_json"]),
        "revision": int(row["revision"]),
        "updated_at": str(row["updated_at"] or ""),
        "stored": True,
    }


def save_layout(db, user_id: int, rows: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Persist one profile's shelf order. Only this profile's row is touched."""
    normalized = normalize_layout(rows)
    if not normalized:
        raise HomeLayoutError("Choose at least one Home shelf")
    if all(entry["hidden"] for entry in normalized):
        raise HomeLayoutError("Keep at least one Home shelf visible")
    stamp = _iso(now or datetime.now(UTC))
    db.execute(
        """INSERT INTO viewer_home_layout(user_id, rows_json, revision, updated_at)
           VALUES(?,?,1,?)
           ON CONFLICT(user_id) DO UPDATE SET
             rows_json=excluded.rows_json,
             revision=viewer_home_layout.revision+1,
             updated_at=excluded.updated_at""",
        (user_id, json.dumps(normalized, separators=(",", ":")), stamp),
    )
    db.commit()
    return load_layout(db, user_id)


def apply_layout_action(
    current: list[dict[str, Any]], action: str, target: str
) -> list[dict[str, Any]]:
    """Apply one Edit Home control. Used when JavaScript is unavailable."""
    rows = normalize_layout(current)
    if action == "reset":
        return default_layout()
    index = next((number for number, row in enumerate(rows) if row["id"] == target), None)
    if action == "add":
        if target in ROWS_BY_ID and index is not None:
            rows[index]["hidden"] = False
        return rows
    if index is None:
        raise HomeLayoutError("That Home shelf is not on this profile")
    if action == "remove":
        rows[index]["hidden"] = True
    elif action == "move-up":
        if index == 0:
            raise HomeLayoutError("That shelf is already first")
        rows[index - 1], rows[index] = rows[index], rows[index - 1]
    elif action == "move-down":
        if index == len(rows) - 1:
            raise HomeLayoutError("That shelf is already last")
        rows[index + 1], rows[index] = rows[index], rows[index + 1]
    else:
        raise HomeLayoutError("That Home edit is not supported")
    return rows


def edit_rows(layout: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Describe the layout for the Edit Home dialog, catalogue order preserved."""
    by_id = {row["id"]: row for row in layout}
    view = []
    for index, entry in enumerate(layout):
        spec = ROWS_BY_ID[entry["id"]]
        view.append(
            {
                "id": entry["id"],
                "label": spec["label"],
                "description": spec["description"],
                "kind": spec["kind"],
                "hidden": entry["hidden"],
                "position": index + 1,
                "first": index == 0,
                "last": index == len(layout) - 1,
            }
        )
    del by_id
    return view


def missing_rows(layout: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Shelves hidden from this profile, offered as Add back."""
    present = {entry["id"] for entry in layout if not entry["hidden"]}
    return [
        {"id": row["id"], "label": row["label"], "description": row["description"]}
        for row in HOME_ROW_CATALOG
        if row["id"] not in present
    ]


# ---------------------------------------------------------------------------
# Continue Watching fold
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContinueSignal:
    """One watch or resume observation, already scoped to the viewer.

    ``last_watch_at`` may only be set from watch evidence. Callers must not pass
    an air date, a library sync stamp or a catalogue update time.
    """

    domain: str
    key: str
    title: str
    last_watch_at: str | None = None
    title_id: str = ""
    season: int | None = None
    episode: int | None = None
    next_episode: int | None = None
    next_playable: bool = False
    caught_up: bool = False
    completed: bool = False
    finished_title: bool = False
    position_seconds: float | None = None
    duration_seconds: float | None = None
    resume_percent: int | None = None
    resume_minutes_left: int | None = None
    play_url: str | None = None
    detail_url: str = ""
    landscape: str | None = None
    poster: str | None = None
    subtitle: str = ""
    meta: dict[str, Any] = field(default_factory=dict, compare=False)


@dataclass
class _Fold:
    """Accumulates every observation about one show before one card is built."""

    domain: str
    key: str
    signals: list[ContinueSignal] = field(default_factory=list)

    def absorb(self, signal: ContinueSignal) -> None:
        self.signals.append(signal)


def fold_continue_watching(
    signals: list[ContinueSignal], *, limit: int = 20
) -> dict[str, list[dict[str, Any]]]:
    """Fold many observations into one card per show, newest watch first.

    A catch-up hides the show until the next unwatched episode is playable. When
    the episode arrives the show returns ranked by its own last watch time, so a
    release date or a sync changes nothing about its order.
    """
    folds: dict[tuple[str, str], _Fold] = {}
    for signal in signals:
        if not isinstance(signal, ContinueSignal):
            continue
        if signal.domain not in {"anime", "movie", "tv"}:
            continue
        folds.setdefault((signal.domain, signal.key), _Fold(signal.domain, signal.key)).absorb(
            signal
        )
    cards: list[dict[str, Any]] = []
    hidden: list[dict[str, Any]] = []
    for folded in folds.values():
        card = _card_from_fold(folded)
        if card is None:
            continue
        if card["hidden"]:
            hidden.append(card)
        else:
            cards.append(card)
    cards.sort(key=lambda card: (card["last_watch_at"] or "", card["sort_key"]), reverse=True)
    hidden.sort(key=lambda card: (card["last_watch_at"] or "", card["sort_key"]), reverse=True)
    return {"cards": cards[:limit], "hidden": hidden[:limit]}


def _card_from_fold(folded: _Fold) -> dict[str, Any] | None:
    domain = folded.domain
    usable = [signal for signal in folded.signals if _signal_allowed(signal)]
    if not usable:
        return None
    # Keep the target, metadata, URL and progress bound to one observation.
    lead = max(
        usable,
        key=lambda signal: (
            signal.last_watch_at or "",
            bool(signal.play_url),
            bool(_resume_block(signal)),
        ),
    )
    episode = lead.episode
    season = lead.season
    next_episode = lead.next_episode
    playable_next = lead.next_playable and bool(lead.play_url)
    completed = lead.completed
    resume = _resume_block(lead)
    last_watch = lead.last_watch_at or ""

    if domain == "movie" and lead.finished_title:
        return None
    if lead.caught_up and not playable_next and resume is None:
        return _hidden_card(folded, lead, last_watch, reason="awaiting-next-episode")
    play_url = lead.play_url
    label, action = _play_label(
        domain=domain,
        episode=episode,
        next_episode=next_episode,
        playable_next=playable_next,
        completed=completed,
        resume=resume,
    )
    if play_url is None:
        # Availability without a bound play source stays visible as an overview
        # entry. It never pretends a play control exists.
        label, action = "View episodes", "detail"
    subtitle = _subtitle(
        domain=domain,
        season=season,
        episode=episode,
        next_episode=next_episode,
        playable_next=playable_next,
        lead=lead,
    )
    return {
        "id": f"{domain}-{folded.key}",
        "sort_key": f"{domain}:{folded.key}",
        "domain": domain,
        "key": folded.key,
        "title": _clean_title(lead.title),
        "title_id": lead.title_id or folded.key,
        "subtitle": subtitle,
        "detail_label": _detail_label(resume, completed, playable_next, domain),
        "progress": _progress_block(resume),
        "episode": {
            "season": season,
            "episode": episode,
            "next_episode": next_episode,
            "next_playable": playable_next,
        },
        "play_url": play_url,
        "play_label": label,
        "play_action": action,
        "detail_url": lead.detail_url,
        "artwork": {"landscape": lead.landscape, "poster": lead.poster},
        "last_watch_at": last_watch,
        "hidden": False,
        # Anime keeps the readiness fields the shared card needs for its
        # series progress label; no other domain duplicates them.
        "anime": lead.meta.get("item") if domain == "anime" else None,
    }


def _hidden_card(
    folded: _Fold, lead: ContinueSignal, last_watch: str, *, reason: str
) -> dict[str, Any]:
    return {
        "id": f"{folded.domain}-{folded.key}",
        "sort_key": f"{folded.domain}:{folded.key}",
        "domain": folded.domain,
        "key": folded.key,
        "title": _clean_title(lead.title),
        "last_watch_at": last_watch,
        "hidden": True,
        "hidden_reason": reason,
    }


def _signal_allowed(signal: ContinueSignal) -> bool:
    """Keep a signal that says anything about watch state.

    A missing watch time is not a reason to drop a title: it simply ranks after
    dated activity. Only a signal with no watch evidence at all is ignored.
    """
    if signal.last_watch_at and not _ISO_HINT.match(signal.last_watch_at):
        return False
    return bool(
        signal.last_watch_at
        or signal.play_url
        or signal.episode
        or signal.position_seconds
        or signal.caught_up
        or signal.completed
    )


def _resume_block(signal: ContinueSignal) -> dict[str, Any] | None:
    position = signal.position_seconds
    duration = signal.duration_seconds
    if position is not None and duration and 0 < position < duration:
        percent = min(99, max(1, round(100 * position / duration)))
        minutes_left = max(0, math.ceil((duration - position) / 60))
        return {
            "position_seconds": round(float(position), 3),
            "duration_seconds": round(float(duration), 3),
            "percent": percent,
            "minutes_left": minutes_left,
            "remaining_label": f"{minutes_left} min left",
            "updated_sort": signal.last_watch_at or "",
        }
    if signal.resume_percent and signal.resume_minutes_left is not None:
        # Library readiness reports a playhead as percent plus time left; that
        # is still saved playback evidence, so it can label the resume.
        return {
            "position_seconds": None,
            "duration_seconds": None,
            "percent": int(signal.resume_percent),
            "minutes_left": int(signal.resume_minutes_left),
            "remaining_label": f"{int(signal.resume_minutes_left)} min left",
            "updated_sort": signal.last_watch_at or "",
        }
    return None


def _progress_block(resume: dict[str, Any] | None) -> dict[str, Any]:
    if resume is None:
        return {
            "percent": None,
            "position_seconds": None,
            "duration_seconds": None,
            "minutes_left": None,
            "remaining_label": "",
        }
    return {
        "percent": resume["percent"],
        "position_seconds": resume["position_seconds"],
        "duration_seconds": resume["duration_seconds"],
        "minutes_left": resume["minutes_left"],
        "remaining_label": resume["remaining_label"],
    }


def _play_label(
    *,
    domain: str,
    episode: int | None,
    next_episode: int | None,
    playable_next: bool,
    completed: bool,
    resume: dict[str, Any] | None,
) -> tuple[str, str]:
    """Return (button label, machine action)."""
    if domain == "movie":
        return ("Resume" if resume else "Play"), ("resume" if resume else "play")
    if resume:
        return f"Resume episode {episode}", "resume"
    if completed and playable_next and next_episode:
        return f"Play episode {next_episode}", "play-next"
    if playable_next and next_episode and not completed:
        return f"Play episode {next_episode}", "play-next"
    if episode:
        return f"Play episode {episode}", "play"
    return "Play", "play"


def _subtitle(
    *,
    domain: str,
    season: int | None,
    episode: int | None,
    next_episode: int | None,
    playable_next: bool,
    lead: ContinueSignal,
) -> str:
    if lead.subtitle:
        return lead.subtitle
    if domain == "movie":
        return "Movie"
    if episode and season:
        return f"S{season} E{episode}"
    if playable_next and next_episode and season:
        return f"S{season} E{next_episode} up next"
    if episode:
        return f"Episode {episode}"
    return ""


def _detail_label(
    resume: dict[str, Any] | None, completed: bool, playable_next: bool, domain: str
) -> str:
    if resume:
        return resume["remaining_label"]
    if domain == "movie":
        return ""
    if completed and playable_next:
        return "Next episode ready"
    return "Up next"


def _clean_title(value: Any) -> str:
    text = re.sub(r"<[^>]*>", "", str(value or "")).strip()
    text = re.sub(r"\s+", " ", text)
    return text[:MAX_CARD_TITLE] or "Untitled"


# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------


def _banner_rank(card: dict[str, Any]) -> tuple[str, str]:
    return card["last_watch_at"], card.get("sort_key", "")


def select_banner_choices(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One newest playable title for each available media type."""
    newest: dict[str, dict[str, Any]] = {}
    for row in rows:
        for card in row.get("cards") or []:
            domain = card.get("domain")
            if domain not in {"anime", "movie", "tv"}:
                continue
            if not card.get("play_url") or not card.get("last_watch_at"):
                continue
            current = newest.get(domain)
            if current is None or _banner_rank(card) > _banner_rank(current):
                newest[domain] = card
    return [
        _banner_from_card(card) for card in sorted(newest.values(), key=_banner_rank, reverse=True)
    ]


def select_banner(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Use the newest playable title, then fall back to a title detail."""
    playable = select_banner_choices(rows)
    if playable:
        return playable[0]
    fallback = max(
        (
            card
            for row in rows
            for card in row.get("cards") or []
            if card.get("detail_url") and card.get("last_watch_at") and card.get("episode")
        ),
        key=_banner_rank,
        default=None,
    )
    return _banner_from_card(fallback) if fallback else None


def _banner_from_card(lead: dict[str, Any]) -> dict[str, Any]:
    playable = bool(lead["play_url"])
    view_episodes = lead["detail_url"]
    anime = lead.get("anime") or {}
    if not playable and anime.get("resurfaced") and anime.get("next_episode"):
        view_episodes = f"{view_episodes.split('#', 1)[0]}#episode-{int(anime['next_episode'])}"
    return {
        "domain": lead["domain"],
        "title": lead["title"],
        "title_id": lead["title_id"],
        "key": lead["key"],
        "eyebrow": _banner_eyebrow(lead),
        "media_detail": lead["subtitle"],
        "progress": lead["progress"],
        "play_url": lead["play_url"] if playable else view_episodes,
        "play_label": lead["play_label"] if playable else "View episodes",
        "play_action": lead["play_action"] if playable else "detail",
        "detail_url": lead["detail_url"],
        "artwork": lead["artwork"],
        "last_watch_at": lead["last_watch_at"],
        "neutral": False,
        "series": _series_progress(lead),
    }


def _series_progress(card: dict[str, Any]) -> dict[str, Any] | None:
    """Episode counts for the banner, from the same readiness card as the rail."""
    anime = card.get("anime")
    if not isinstance(anime, dict):
        return None
    total = anime.get("episode_total")
    watched = anime.get("progress_episodes")
    if watched is None or total is None:
        return None
    return {"watched": int(watched), "total": int(total)}


def neutral_banner() -> dict[str, Any]:
    """No active title: a neutral frame that still offers a way forward."""
    return {
        "domain": None,
        "title": "A good story starts here.",
        "title_id": "",
        "key": "",
        "eyebrow": "MADE FOR YOUR NEXT WATCH",
        "media_detail": "",
        "progress": _progress_block(None),
        "play_url": None,
        "play_label": "Browse your library",
        "play_action": "browse",
        "detail_url": "/plus/library",
        "artwork": {"landscape": None, "poster": None},
        "last_watch_at": "",
        "neutral": True,
        "series": None,
    }


def _banner_eyebrow(card: dict[str, Any]) -> str:
    domain = {"anime": "Anime", "movie": "Movies", "tv": "TV Shows"}[card["domain"]]
    episode = card["episode"]
    if card["domain"] == "movie":
        return domain
    if (
        card["play_action"] == "play-next"
        and episode.get("next_playable")
        and episode.get("next_episode")
    ):
        return f"{domain} · Episode {episode['next_episode']} up next"
    if episode.get("episode"):
        return f"{domain} · Episode {episode['episode']}"
    return domain


# ---------------------------------------------------------------------------
# Signals from existing records
# ---------------------------------------------------------------------------


def anime_signals(
    db, *, user_id: int, titles: list[dict[str, Any]], recency=None
) -> list[ContinueSignal]:
    """Build anime signals from library readiness plus accepted watch recency."""
    from omakase.plus.viewing_history import anime_viewing_recency

    recency = (
        recency
        if recency is not None
        else anime_viewing_recency(db, user_id, include_completed=True)
    )
    signals: list[ContinueSignal] = []
    for item in titles:
        progress = int(item.get("progress_episodes") or 0)
        resurfaced = bool(item.get("resurfaced"))
        available = bool(item.get("new_episode_available"))
        caught_up = bool(item.get("caught_up"))
        partial = bool(item.get("partial_playback") or item.get("native_resume_at"))
        if not (progress > 0 or resurfaced or partial or (caught_up and available)):
            continue
        identity = (int(item["anilist_id"]), int(item.get("season_number") or 0))
        last_watch = max(
            recency.get(identity, "") or "",
            str(item.get("native_resume_at") or ""),
        )
        resume = item.get("episode_resume") or {}
        play_episode = item.get("play_episode") or item.get("next_episode")
        play_url = item.get("play_url")
        starting_next = bool(
            available and play_url and not resume and play_episode == item.get("next_episode")
        )
        title_id = str(item["id"])
        signals.append(
            ContinueSignal(
                domain="anime",
                key=title_id,
                title=str(item.get("title") or ""),
                title_id=title_id,
                last_watch_at=last_watch or None,
                season=int(item.get("season_number") or 0) or None,
                episode=int(play_episode) if play_episode else None,
                next_episode=int(item["next_episode"]) if item.get("next_episode") else None,
                next_playable=starting_next,
                caught_up=caught_up,
                completed=str(item.get("watch_status") or "") == "completed",
                position_seconds=_float_or_none(resume.get("position_seconds")),
                duration_seconds=_float_or_none(resume.get("duration_seconds")),
                resume_percent=resume.get("percent"),
                resume_minutes_left=resume.get("minutes_left"),
                play_url=play_url,
                detail_url=str(item.get("detail_url") or f"/plus/library/{title_id}#overview"),
                landscape=f"/plus/media/{int(item['anilist_id'])}/banner?display=hero-v3",
                poster=f"/plus/media/{int(item['anilist_id'])}/poster",
                meta={"lifecycle": item.get("lifecycle"), "resurface": resurfaced, "item": item},
            )
        )
    return signals


def movie_signals(db, *, user_id: int, library_id: str | None = None) -> list[ContinueSignal]:
    """Movies with a saved playhead, plus the viewer's own last watch time."""
    rows = db.execute(
        """SELECT r.canonical_id, r.position_seconds, r.duration_seconds, r.updated_at,
                  c.title, c.year, c.poster_url, c.backdrop_url,
                  h.status, h.last_viewed_at
           FROM web_playback_resumes AS r
           LEFT JOIN movie_catalog AS c
             ON c.user_id = r.user_id AND c.imdb_id = r.canonical_id
           LEFT JOIN movie_history AS h
             ON h.user_id = r.user_id AND h.imdb_id = r.canonical_id
           WHERE r.user_id = ? AND (? IS NULL OR r.library_id = ?) AND r.domain = 'movie'
             AND c.title IS NOT NULL
           ORDER BY r.updated_at DESC
           LIMIT 200""",
        (user_id, library_id, library_id),
    ).fetchall()
    signals: list[ContinueSignal] = []
    for row in rows:
        imdb_id = str(row["canonical_id"] or "")
        title = str(row["title"] or "").strip()
        if not imdb_id.startswith("tt") or not imdb_id[2:].isdigit() or not title:
            continue
        completed = str(row["status"] or "") == "completed"
        finished = bool(
            completed and not _resumable(row["position_seconds"], row["duration_seconds"])
        )
        last_watch = (
            max(_stamp(row["last_viewed_at"]) or "", _stamp_number(row["updated_at"]) or "") or None
        )
        signals.append(
            ContinueSignal(
                domain="movie",
                key=imdb_id,
                title=title,
                title_id=imdb_id,
                last_watch_at=last_watch,
                finished_title=finished,
                completed=completed,
                position_seconds=row["position_seconds"],
                duration_seconds=row["duration_seconds"],
                play_url=None if finished else f"/plus/watch/title/movie/{imdb_id}?play=1",
                detail_url=f"/plus/movies/{imdb_id}",
                landscape=f"/plus/movies/{imdb_id}/artwork?variant=backdrop"
                if row["backdrop_url"]
                else None,
                poster=f"/plus/movies/{imdb_id}/artwork?variant=poster"
                if row["poster_url"]
                else None,
                subtitle=f"Movie · {int(row['year'])}" if row["year"] else "Movie",
                meta={"year": row["year"]},
            )
        )
    return signals


def tv_signals(
    db,
    *,
    user_id: int,
    now: datetime | None = None,
    library_id: str | None = None,
    personal: bool = True,
) -> list[ContinueSignal]:
    """TV signals from saved playheads and explicit watched episodes."""
    moment = (now or datetime.now(UTC)).date().isoformat()
    rows = db.execute(
        """SELECT r.canonical_id, r.season_number, r.episode_number,
                  r.position_seconds, r.duration_seconds, r.updated_at,
                  c.metadata_json, h.status
           FROM web_playback_resumes AS r
           LEFT JOIN tv_catalog AS c
             ON c.user_id = r.user_id AND c.tmdb_id = CAST(r.canonical_id AS INTEGER)
           LEFT JOIN tv_history AS h
             ON h.user_id = r.user_id AND h.tmdb_id = CAST(r.canonical_id AS INTEGER)
           WHERE r.user_id = ? AND (? IS NULL OR r.library_id = ?) AND r.domain = 'tv'
           ORDER BY r.updated_at DESC
           LIMIT 200""",
        (user_id, library_id, library_id),
    ).fetchall()
    signals: list[ContinueSignal] = []
    for row in rows:
        tmdb_id = str(row["canonical_id"] or "")
        if not tmdb_id.isdigit():
            continue
        metadata = _json_dict(row["metadata_json"])
        watched, lineup = _tv_episode_state(db, user_id=user_id, tmdb_id=int(tmdb_id))
        season, episode = int(row["season_number"]), int(row["episode_number"])
        resume = _resumable(row["position_seconds"], row["duration_seconds"])
        completed = not resume and bool(watched.get((season, episode)))
        next_episode = _next_episode(lineup, season, episode, moment=moment) if completed else None
        play_season, play_episode = next_episode or (season, episode)
        signals.append(
            ContinueSignal(
                domain="tv",
                key=tmdb_id,
                title=str(metadata.get("title") or f"TV {tmdb_id}"),
                title_id=tmdb_id,
                last_watch_at=_stamp_number(row["updated_at"]),
                season=play_season,
                episode=episode,
                next_episode=next_episode[1] if next_episode else None,
                next_playable=next_episode is not None,
                caught_up=completed and next_episode is None,
                completed=completed and not resume,
                position_seconds=row["position_seconds"] if resume else None,
                duration_seconds=row["duration_seconds"] if resume else None,
                play_url=f"/plus/watch/title/tv/{tmdb_id}/{play_season}/{play_episode}?play=1"
                if not completed or next_episode
                else None,
                detail_url=f"/plus/tv/{tmdb_id}?season={play_season}",
                landscape=f"/plus/tv/{tmdb_id}/artwork?variant=backdrop"
                if metadata.get("backdrop_url")
                else None,
                poster=f"/plus/tv/{tmdb_id}/artwork?variant=poster"
                if metadata.get("poster_url")
                else None,
                subtitle=f"S{play_season} E{play_episode}" + (" up next" if next_episode else ""),
                meta={"status": row["status"]},
            )
        )
    if personal:
        signals.extend(_tv_watched_signals(db, user_id=user_id, moment=moment))
    return signals


def _tv_watched_signals(db, *, user_id: int, moment: str) -> list[ContinueSignal]:
    """Explicitly watched episodes that still have a playable next episode."""
    rows = db.execute(
        """SELECT e.tmdb_id, MAX(e.season_number * 10000 + e.episode_number) AS high_water,
                  MAX(e.updated_at) AS last_change, c.metadata_json, h.status
           FROM tv_episodes AS e
           LEFT JOIN tv_catalog AS c ON c.user_id = e.user_id AND c.tmdb_id = e.tmdb_id
           LEFT JOIN tv_history AS h ON h.user_id = e.user_id AND h.tmdb_id = e.tmdb_id
           WHERE e.user_id = ? AND e.watched = 1 AND e.watch_source IN ('manual','plex')
           GROUP BY e.tmdb_id
           ORDER BY last_change DESC
           LIMIT 100""",
        (user_id,),
    ).fetchall()
    signals: list[ContinueSignal] = []
    for row in rows:
        tmdb_id = int(row["tmdb_id"])
        high = int(row["high_water"] or 0)
        season, episode = divmod(high, 10000)
        if episode < 1 or season < 1:
            continue
        watched, lineup = _tv_episode_state(db, user_id=user_id, tmdb_id=tmdb_id)
        next_episode = _next_episode(lineup, season, episode, moment=moment)
        if next_episode is None:
            # Caught up: the show is hidden until the next episode is playable.
            signals.append(
                ContinueSignal(
                    domain="tv",
                    key=str(tmdb_id),
                    title=str(_json_dict(row["metadata_json"]).get("title") or f"TV {tmdb_id}"),
                    title_id=str(tmdb_id),
                    last_watch_at=_stamp(row["last_change"]),
                    season=season,
                    episode=episode,
                    caught_up=True,
                    completed=True,
                )
            )
            continue
        next_season, next_number = next_episode
        metadata = _json_dict(row["metadata_json"])
        signals.append(
            ContinueSignal(
                domain="tv",
                key=str(tmdb_id),
                title=str(metadata.get("title") or f"TV {tmdb_id}"),
                title_id=str(tmdb_id),
                last_watch_at=_stamp(row["last_change"]),
                season=next_season,
                episode=episode,
                next_episode=next_number,
                next_playable=True,
                completed=True,
                play_url=f"/plus/watch/title/tv/{tmdb_id}/{next_season}/{next_number}?play=1",
                detail_url=f"/plus/tv/{tmdb_id}?season={next_season}",
                landscape=f"/plus/tv/{tmdb_id}/artwork?variant=backdrop"
                if metadata.get("backdrop_url")
                else None,
                poster=f"/plus/tv/{tmdb_id}/artwork?variant=poster"
                if metadata.get("poster_url")
                else None,
                subtitle=f"S{next_season} E{next_number} up next",
                meta={"watched": watched.get((season, episode), False)},
            )
        )
    return signals


def _tv_episode_state(db, *, user_id: int, tmdb_id: int):
    """Watched flags plus the playable lineup, ordered by season and episode."""
    rows = db.execute(
        """SELECT season_number, episode_number, watched, plex_present, metadata_json
           FROM tv_episodes WHERE user_id = ? AND tmdb_id = ?
           ORDER BY season_number, episode_number""",
        (user_id, tmdb_id),
    ).fetchall()
    watched: dict[tuple[int, int], bool] = {}
    lineup: list[tuple[int, int, str]] = []
    for row in rows:
        key = (int(row["season_number"]), int(row["episode_number"]))
        watched[key] = bool(row["watched"])
        metadata = _json_dict(row["metadata_json"])
        air_date = str(metadata.get("air_date") or "")
        lineup.append(
            (key[0], key[1], air_date if bool(row["plex_present"]) and not watched[key] else "")
        )
    return watched, lineup


def _next_episode(
    lineup: list[tuple[int, int, str]], season: int, episode: int, *, moment: str
) -> tuple[int, int] | None:
    """First unwatched, released, present episode after the current position."""
    for number_season, number_episode, air_date in lineup:
        if (number_season, number_episode) <= (season, episode):
            continue
        if not air_date:
            continue
        if air_date > moment:
            continue
        return number_season, number_episode
    return None


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


def projection(
    db,
    *,
    user_id: int,
    library_scope=None,
    anime_titles: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the complete Home read model for one profile."""
    layout_record = load_layout(db, user_id)
    layout = layout_record["rows"]
    if anime_titles is None:
        from omakase.plus.product_views import library_titles

        anime_titles = library_titles(db, user_id=user_id, library_scope=library_scope)
    anime = fold_continue_watching(anime_signals(db, user_id=user_id, titles=anime_titles))
    scope_id = library_scope.library_id if library_scope is not None else None
    if scope_id is None:
        personal = db.execute(
            "SELECT library_id FROM personal_libraries WHERE user_id=?", (user_id,)
        ).fetchone()
        scope_id = str(personal[0]) if personal else None
    movies = fold_continue_watching(movie_signals(db, user_id=user_id, library_id=scope_id))
    tv = fold_continue_watching(
        tv_signals(
            db,
            user_id=user_id,
            now=now,
            library_id=scope_id,
            personal=library_scope is None or library_scope.kind == "personal",
        )
    )
    by_row = {
        "continue-anime": anime["cards"],
        "continue-movies": movies["cards"],
        "continue-tv": tv["cards"],
        "watchlist": _watchlist_cards(db, user_id=user_id),
        "ready-to-watch": _ready_cards(anime_titles),
        "recently-watched": _recent_cards(db, user_id=user_id),
    }
    rows = []
    for entry in layout:
        if entry["hidden"]:
            continue
        spec = ROWS_BY_ID[entry["id"]]
        items = by_row.get(entry["id"], [])
        rows.append(
            {
                "id": entry["id"],
                "label": spec["label"],
                "kind": spec["kind"],
                "domain": spec["domain"],
                "count": len(items),
                "cards": items,
            }
        )
    banner_choices = select_banner_choices(rows)
    banner = banner_choices[0] if banner_choices else select_banner(rows) or neutral_banner()
    return {
        "profile": {"user_id": user_id},
        "banner": banner,
        "banner_choices": banner_choices,
        "rows": rows,
        "layout": {"rows": edit_rows(layout), "updated_at": layout_record["updated_at"]},
        "addable": missing_rows(layout),
        "hidden": {
            "continue-anime": anime["hidden"],
            "continue-movies": movies["hidden"],
            "continue-tv": tv["hidden"],
        },
        "empty": not any(row["count"] for row in rows),
        "updated_at": layout_record["updated_at"],
    }


def _watchlist_cards(db, *, user_id: int) -> list[dict[str, Any]]:
    from omakase.plus.product_views import saved_watchlist

    cards = []
    for item in saved_watchlist(db, user_id=user_id)[:20]:
        anilist_id = int(item["anilist_id"])
        cards.append(
            {
                "id": f"watchlist-{anilist_id}",
                "sort_key": f"watchlist:{anilist_id}",
                "domain": "anime",
                "key": str(anilist_id),
                "title": _clean_title(item["title"]),
                "title_id": str(anilist_id),
                "subtitle": "On your watchlist",
                "detail_label": "Not added yet",
                "progress": _progress_block(None),
                "episode": {
                    "season": None,
                    "episode": None,
                    "next_episode": None,
                    "next_playable": False,
                },
                "play_url": None,
                "play_label": "Add to library",
                "play_action": "detail",
                "detail_url": f"/plus/catalog/anime/{anilist_id}",
                "artwork": {
                    "landscape": f"/plus/media/{anilist_id}/banner?display=hero-v3",
                    "poster": f"/plus/media/{anilist_id}/poster",
                },
                "last_watch_at": "",
                "hidden": False,
            }
        )
    return cards


def _ready_cards(titles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cards = []
    for item in titles:
        if item.get("lifecycle") != "ready_to_start":
            continue
        anilist_id = int(item["anilist_id"])
        cards.append(
            {
                "id": f"ready-{item['id']}",
                "sort_key": f"ready:{item['id']}",
                "domain": "anime",
                "key": str(item["id"]),
                "title": _clean_title(item.get("title")),
                "title_id": str(item["id"]),
                "subtitle": str(item.get("lifecycle_label") or "Ready to start"),
                "detail_label": str(item.get("status_label") or ""),
                "progress": _progress_block(None),
                "episode": {
                    "season": int(item.get("season_number") or 0) or None,
                    "episode": item.get("next_episode"),
                    "next_episode": item.get("next_episode"),
                    "next_playable": bool(item.get("play_url")),
                },
                "play_url": item.get("play_url"),
                "play_label": "Play" if item.get("play_url") else "View title",
                "play_action": "play" if item.get("play_url") else "detail",
                "detail_url": f"/plus/library/{int(item['id'])}#overview",
                "artwork": {
                    "landscape": f"/plus/media/{anilist_id}/banner?display=hero-v3",
                    "poster": f"/plus/media/{anilist_id}/poster",
                },
                "last_watch_at": "",
                "hidden": False,
            }
        )
        if len(cards) >= 20:
            break
    return cards


def _recent_cards(db, *, user_id: int) -> list[dict[str, Any]]:
    from omakase.plus.viewing_history import viewing_history

    cards = []
    for item in viewing_history(db, user_id=user_id, limit=12)["items"]:
        artwork = item.get("artwork_url") or item.get("series_artwork_url")
        cards.append(
            {
                "id": f"recent-{item['id']}",
                "sort_key": f"recent:{item['id']}",
                "domain": "anime"
                if item.get("media_type") == "anime"
                else ("movie" if item.get("media_type") == "movie" else "tv"),
                "key": str(item["id"]),
                "title": _clean_title(item.get("title")),
                "title_id": str(item.get("id")),
                "subtitle": str(item.get("status_label") or ""),
                "detail_label": str(item.get("source_label") or ""),
                "progress": _progress_block(None),
                "episode": {
                    "season": item.get("season_number"),
                    "episode": item.get("episode"),
                    "next_episode": None,
                    "next_playable": False,
                },
                "play_url": None,
                "play_label": "View history",
                "play_action": "detail",
                "detail_url": str(item.get("detail_url") or "/plus/activity?view=watch"),
                "artwork": {"landscape": artwork, "poster": item.get("series_artwork_url")},
                "last_watch_at": str(item.get("watched_at") or item.get("recorded_at") or ""),
                "hidden": False,
            }
        )
    return cards


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def _resumable(position: Any, duration: Any) -> bool:
    try:
        start = float(position)
        total = float(duration)
    except (TypeError, ValueError):
        return False
    return 0 < start < total


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _stamp(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return _iso(parsed)


def _stamp_number(value: Any) -> str | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if not 0 < seconds < 4102444800:
        return None
    return _iso(datetime.fromtimestamp(seconds, UTC))


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or len(value) > 2_000_000:
        return {}
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError, RecursionError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
