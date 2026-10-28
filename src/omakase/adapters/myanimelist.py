"""MyAnimeList adapter — fetches anime watch history and candidate pool.

Two paths into the user's list:
  1. Live API path: MAL API v2 (requires MAL_CLIENT_ID) — works only if the
     user's list is set to Public.
  2. Export-upload path: parse a MAL XML export (`animelist_*.xml` or its
     gzipped form). Lets users with Private lists run the demo by dropping
     their exported file in. No Client ID required on this path.

Candidate pool comes from Jikan v4 REST API on either path (no auth).

To get a MAL Client ID (only for the live-API path):
  1. Go to https://myanimelist.net/apiconfig
  2. Register a new "Web" application (any name/URL works)
  3. Copy the Client ID
  4. Set it:  $env:MAL_CLIENT_ID="your_client_id"
"""

from __future__ import annotations

import gzip
import json
import os
import time
import xml.etree.ElementTree as ET
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from omakase.adapters.base import SourceAdapter, register
from omakase.types import MediaItem, SourceData

USER_AGENT = "Omakase/0.1 (homelab; +https://github.com/HeyiTzSenpai/omakase)"

MAL_API = "https://api.myanimelist.net/v2"
JIKAN_API = "https://api.jikan.moe/v4"


def _get_mal_client_id(client_id: str | None = None) -> str:
    cid = client_id or os.environ.get("MAL_CLIENT_ID", "")
    if not cid:
        raise ValueError(
            "MAL_CLIENT_ID env var is not set. "
            "Get one at https://myanimelist.net/apiconfig, then:\n"
            "  $env:MAL_CLIENT_ID='your_client_id'"
        )
    return cid


def _mal_headers(client_id: str | None = None) -> dict[str, str]:
    return {
        "X-MAL-Client-ID": _get_mal_client_id(client_id),
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }


def _jikan_fetch(endpoint: str) -> dict:
    req = Request(f"{JIKAN_API}{endpoint}", headers={"User-Agent": USER_AGENT})
    with urlopen(req) as resp:
        return json.loads(resp.read().decode())


def _mal_fetch(endpoint: str, client_id: str | None = None) -> dict:
    req = Request(f"{MAL_API}{endpoint}", headers=_mal_headers(client_id))
    with urlopen(req) as resp:
        return json.loads(resp.read().decode())


def _parse_media(m: dict, source: str = "jikan") -> MediaItem:
    """Parse an anime dict from either Jikan or MAL API into a MediaItem."""
    if source == "mal":
        mid = m.get("id", 0)
        title = m.get("title", "")
        title_en = m.get("alternative_titles", {}).get("en", "") or title
        genres = [g.get("name", "") for g in m.get("genres", []) if g.get("name")]
        studios_list = m.get("studios", [])
        studio = studios_list[0].get("name", "") if studios_list else None
        desc = m.get("synopsis", "") or ""
        fmt = m.get("media_type", "")
        episodes = m.get("num_episodes")
        mean_score = m.get("mean")
    else:
        mid = m.get("mal_id", 0)
        title = m.get("title", "")
        title_en = m.get("title_english") or title
        genres = [g.get("name", "") for g in m.get("genres", []) if g.get("name")]
        studios_list = m.get("studios", [])
        studio = studios_list[0].get("name", "") if studios_list else None
        desc = m.get("synopsis", "") or ""
        fmt = m.get("type", "")
        episodes = m.get("episodes")
        mean_score = m.get("score")

    if desc and len(desc) > 200:
        desc = desc[:200] + "..."
    return MediaItem(
        id=mid,
        title_romaji=title,
        title_english=title_en,
        genres=genres,
        tags=[],
        format=fmt,
        episodes=episodes,
        studio=studio,
        description=desc,
        mean_score=float(mean_score) if mean_score else None,
    )


_MAL_STATUS_MAP = {
    "Watching": "CURRENT",
    "Completed": "COMPLETED",
    "On-Hold": "PAUSED",
    "On Hold": "PAUSED",
    "Dropped": "DROPPED",
    "Plan to Watch": "PLANNING",
    "Plan To Watch": "PLANNING",
    "Plan_to_Watch": "PLANNING",
    "Plan_To_Watch": "PLANNING",
}


class MALExportError(ValueError):
    """The uploaded file is not a recognizable MAL anime export."""


def parse_mal_export(data: bytes) -> tuple[str, list[MediaItem]]:
    """Parse a MAL anime-list export (`.xml` or `.xml.gz`) into history items.

    Returns (username_from_export, history_items). Username may be empty
    when MAL omits the <user_name> tag — callers should fall back to the
    form-supplied username in that case.
    """
    if not data:
        raise MALExportError("Uploaded file is empty.")
    # MAL hands you a gzip; users sometimes pre-decompress, so accept both.
    raw = gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise MALExportError(f"Could not parse export XML: {e}") from e

    if root.tag != "myanimelist":
        raise MALExportError(
            "Not a MAL anime-list export. The root element should be <myanimelist>; "
            f"got <{root.tag}>. Make sure you exported the Anime list, not Manga."
        )

    myinfo = root.find("myinfo")
    username = ""
    if myinfo is not None:
        un = myinfo.findtext("user_name")
        if un:
            username = un.strip()

    items: list[MediaItem] = []
    seen_ids: set[int] = set()
    for anime in root.findall("anime"):
        try:
            mid = int((anime.findtext("series_animedb_id") or "0").strip())
        except ValueError:
            continue
        if not mid or mid in seen_ids:
            continue
        seen_ids.add(mid)

        title = (anime.findtext("series_title") or "").strip()
        if not title:
            continue
        fmt = (anime.findtext("series_type") or "").strip()
        episodes_raw = (anime.findtext("series_episodes") or "").strip()
        try:
            episodes = int(episodes_raw) if episodes_raw else None
        except ValueError:
            episodes = None

        score_raw = (anime.findtext("my_score") or "").strip()
        try:
            score = float(score_raw) if score_raw else None
        except ValueError:
            score = None
        if score == 0:
            score = None  # MAL writes "0" for unscored entries.

        status_raw = (anime.findtext("my_status") or "").strip()
        status = _MAL_STATUS_MAP.get(status_raw, status_raw.upper().replace(" ", "_") or None)

        items.append(
            MediaItem(
                id=mid,
                title_romaji=title,
                title_english=title,  # MAL export doesn't carry English titles separately.
                format=fmt,
                episodes=episodes,
                score=score,
                status=status,
            )
        )

    if not items:
        raise MALExportError(
            "No <anime> entries found in the export. Did you export the Anime list?"
        )
    return username, items


@register("myanimelist")
class MALAdapter(SourceAdapter):
    name = "myanimelist"

    def _fetch_history(self, username: str, client_id: str | None = None) -> list[MediaItem]:
        """Fetch all user anime list entries from MAL API v2."""
        fetch = (lambda endpoint: _mal_fetch(endpoint, client_id)) if client_id else _mal_fetch
        statuses = ["watching", "completed", "on_hold", "dropped", "plan_to_watch"]
        items: list[MediaItem] = []
        seen_ids: set[int] = set()
        # Request available fields; some may be omitted if not authorized
        fields = "genres,studios,synopsis,media_type,num_episodes,mean"

        for status in statuses:
            offset = 0
            limit = 100
            while True:
                try:
                    data = fetch(
                        f"/users/{username}/animelist?"
                        f"fields={fields}&limit={limit}&offset={offset}"
                        f"&status={status}"
                    )
                except HTTPError as e:
                    if e.code == 404:
                        return items  # user not found or no list
                    raise

                entries = data.get("data", [])
                if not entries:
                    break

                for entry in entries:
                    node = entry.get("node", {})
                    mid = node.get("id")
                    if not mid or mid in seen_ids:
                        continue
                    seen_ids.add(mid)

                    list_status = entry.get("list_status", {})
                    user_score = list_status.get("score")
                    user_status = {
                        "watching": "CURRENT",
                        "completed": "COMPLETED",
                        "on_hold": "PAUSED",
                        "dropped": "DROPPED",
                        "plan_to_watch": "PLANNING",
                    }.get(status, status.upper())

                    mi = _parse_media(node, source="mal")
                    mi.score = float(user_score) if user_score else None
                    mi.status = user_status
                    items.append(mi)

                paging = data.get("paging", {})
                if not paging.get("next"):
                    break
                offset += limit
                time.sleep(0.35)

        return items

    def _fetch_candidates_jikan(self, exclude_ids: list[int], pool_size: int) -> list[MediaItem]:
        """Fetch top anime from Jikan, excluding watched IDs."""
        items: list[MediaItem] = []
        page = 1
        while len(items) < pool_size:
            try:
                data = _jikan_fetch(f"/top/anime?page={page}&limit=25")
            except HTTPError:
                break
            media_list = data.get("data", [])
            if not media_list:
                break
            for entry in media_list:
                mid = entry.get("mal_id")
                if mid and mid in exclude_ids:
                    continue
                mi = _parse_media(entry, source="jikan")
                items.append(mi)
            page += 1
            time.sleep(0.5)
        return items[:pool_size]

    def _fetch_planning(self, username: str, client_id: str | None = None) -> list[MediaItem]:
        """Fetch user's plan_to_watch list from MAL API."""
        fetch = (lambda endpoint: _mal_fetch(endpoint, client_id)) if client_id else _mal_fetch
        items: list[MediaItem] = []
        offset = 0
        limit = 100
        fields = "genres,studios,synopsis,media_type,num_episodes,mean"
        while True:
            try:
                data = fetch(
                    f"/users/{username}/animelist?"
                    f"fields={fields}&limit={limit}&offset={offset}"
                    f"&status=plan_to_watch"
                )
            except HTTPError as e:
                if e.code == 404:
                    return items
                raise
            entries = data.get("data", [])
            if not entries:
                break
            for entry in entries:
                node = entry.get("node", {})
                mi = _parse_media(node, source="mal")
                items.append(mi)
            paging = data.get("paging", {})
            if not paging.get("next"):
                break
            offset += limit
            time.sleep(0.35)
        return items

    def fetch(self, username: str, pool_size: int = 100, **kwargs) -> SourceData:
        export_data: bytes | None = kwargs.get("export_data")
        use_planning = kwargs.get("use_planning", False)
        client_id: str | None = kwargs.get("mal_client_id")

        if export_data is not None:
            export_username, history = parse_mal_export(export_data)
            # Form-supplied username wins when present; the one in the XML is
            # a fallback so the rest of the pipeline (logs, headers) has
            # something to display.
            username = (username or export_username or "").strip()
        else:
            history = self._fetch_history(username, client_id)

        exclude_ids = [m.id for m in history if m.id]

        if use_planning:
            if export_data is not None:
                # The plan-to-watch list lives in the same XML; reuse it.
                candidates = [m for m in history if m.status == "PLANNING"]
            else:
                candidates = self._fetch_planning(username, client_id)
            # In planning mode the candidates ARE the planning list, so they
            # legitimately overlap with history. Only drop actively watched ones.
            watched_ids = {
                m.id for m in history if m.status in {"CURRENT", "COMPLETED", "DROPPED", "PAUSED"}
            }
            candidates = [c for c in candidates if c.id not in watched_ids]
        else:
            candidates = self._fetch_candidates_jikan(exclude_ids, pool_size)
            seen = set(exclude_ids)
            candidates = [c for c in candidates if c.id not in seen]
        return SourceData(
            username=username,
            history=history,
            candidates=candidates,
            source_name="myanimelist",
        )
