"""Actual-app showcase with real cached media metadata/art and a fake account.

python scripts/showcase_preview.py --manifest PATH --art-dir DIRECTORY [--check]
The runner is read-only, loopback-only and never fetches remote artwork/catalogs.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import secrets
import socket
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[1]
STAMP = "2026-01-15T19:30:00Z"
BANNER = "Demo · Sample account and history · External actions disabled"
STATE_NOTICE = "Read-only presentation fixture. No action ran and no saved outcome is verified."
ENV_NAMES = {
    "OMAKASE_DATA_DIR",
    "OMAKASE_PLUS_PRIVATE",
    "OMAKASE_PLUS_MASTER_KEY",
    "OMAKASE_PLUS_INVITE",
    "OMAKASE_ALLOWED_HOSTS",
    "OMAKASE_PLUS_HOUSEHOLDS_ENABLED",
    "OMAKASE_PLUS_LANGUAGE_SETTINGS_ENABLED",
}
LIBRARY_STATES = ("personal", "multiple", "shared", "unavailable")
GUARD = {"package_env_suppressed": 0, "dotenv_calls": 0, "env_read_attempts": 0}


def load_manifest(manifest: Path, art_dir: Path):
    """Explicit caller-provided public catalog facts and cache, never owner stores."""
    art_dir = art_dir.resolve(strict=True)
    if not art_dir.is_dir():
        raise ValueError("Artwork cache must be an existing directory")
    payload = json.loads(manifest.read_text(encoding="utf-8-sig"))
    maps = {}
    for kind, key in (("anime", "anilist_id"), ("tv", "tmdb_id"), ("movies", "imdb_id")):
        rows = payload.get(kind)
        if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
            raise ValueError(f"Manifest requires 1..100 {kind} entries")
        maps[kind] = {}
        for row in rows:
            identifier = row.get(key)
            valid = (
                bool(re.fullmatch(r"tt[0-9]{7,10}", identifier))
                if kind == "movies" and isinstance(identifier, str)
                else type(identifier) is int and identifier > 0
                if kind != "movies"
                else False
            )
            if (
                not valid
                or identifier in maps[kind]
                or not isinstance(row.get("title"), str)
                or not row["title"].strip()
            ):
                raise ValueError(f"Invalid or duplicate {kind} identity/title")
            result = dict(row)
            for variant in ("poster", "banner"):
                relative = row.get(variant) or row.get("poster")
                if not isinstance(relative, str) or ":" in relative or Path(relative).is_absolute():
                    raise ValueError("Artwork must use relative cached file paths")
                target = (art_dir / relative).resolve(strict=True)
                if (
                    not target.is_relative_to(art_dir)
                    or not target.is_file()
                    or target.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}
                ):
                    raise ValueError(
                        "Artwork must be a JPEG, PNG or WebP within the explicit cache"
                    )
                result[variant] = target
            maps[kind][identifier] = result
    return maps


def tv_presentation_card(catalog, state):
    """Pure synthetic season examples, never saved as account inventory."""
    if state not in {"single", "multiple", "partial", "complete", "unknown"}:
        raise ValueError("Unknown TV presentation state")
    row = next(iter(catalog["tv"].values()))
    ids = {
        "single": [1],
        "multiple": [1, 2, 3],
        "partial": [1, 2],
        "complete": [1, 2],
        "unknown": [],
    }[state]
    known = state in {"partial", "complete"}
    seasons, coverage = [], []
    for number in ids:
        present = 1 if state == "partial" and number == 2 else 2
        coverage.append(
            {
                "season_number": number,
                "released_total": 2,
                "in_plex": present,
                "state": "complete" if present == 2 else "partial",
            }
        )
        seasons.append(
            {
                "season_number": number,
                "name": f"Season {number}",
                "episode_count": 2,
                "air_date": "2000-01-01",
                "released": True,
                "episodes_loaded": known,
                "episodes": [
                    {
                        "episode_number": episode,
                        "title": f"Sample episode {episode}",
                        "air_date": "2000-01-01",
                        "released": True,
                        "in_plex": episode <= present,
                        "watched": False,
                        "watch_source": "none",
                        "available": None,
                    }
                    for episode in (1, 2)
                ],
            }
        )
    indexed = 38 if state == "unknown" else sum(c["in_plex"] for c in coverage)
    summary = {
        "single": "1 season in Plex",
        "multiple": "3 seasons in Plex",
        "partial": "S1 complete · S2 partial",
        "complete": "S1 complete · S2 complete",
        "unknown": "In Plex",
    }[state]
    return {
        "tmdb_id": row["tmdb_id"],
        "title": row["title"],
        "year": str(row.get("year", "")),
        "poster_url": "cached-local-art",
        "overview": row.get("overview", ""),
        "seasons": seasons,
        "in_collection": True,
        "status": "unwatched",
        "status_source": "none",
        "watch_status_label": "Not tracked",
        "rating": None,
        "watched_episodes": 0,
        "in_plex_episodes": indexed,
        "plex_indexed_episodes": indexed,
        "plex_summary": summary,
        "plex_season_ids": ids,
        "plex_season_coverage": coverage if known else [],
        "season_coverage_known": known,
        "plex_snapshot_current": state != "unknown",
        "plex_observed_at": STAMP,
        "available_episodes": 0,
        "plex_url": None,
        "detail": STATE_NOTICE
        + " Synthetic season inventory. Media bytes and playback are not verified.",
    }


def title_presentation_card(catalog):
    """Populated media evidence for layout review, without a stored release or probe."""
    row = next(iter(catalog["anime"].values()))
    total = max(7, min(1000, int(row.get("episodes", 7))))
    required = total - 1
    covered = [1, 3, 4]
    source = (
        "[SAMPLE RELEASE] "
        + row["title"]
        + " 1080p BluRay Dual Audio English Subtitles "
        + "Long Original Release Description " * 8
        + "sample.mkv"
    )
    return {
        "id": 1,
        "anilist_id": row["anilist_id"],
        "title": row["title"],
        "format": "TV",
        "watch_status": "watching",
        "progress_episodes": 2,
        "required": required,
        "usable": len(covered),
        "plex_visible": len(covered),
        "episode_total": total,
        "display_episode_count": total,
        "season_number": 1,
        "release_status_label": "Sample episode schedule",
        "lifecycle": "in_progress",
        "lifecycle_label": "Watching",
        "description": row.get("overview", ""),
        "genres": row.get("genres", []),
        "removal": None,
        "publication_pending": False,
        "next_episode": 3,
        "usable_episodes": covered,
        "plex_visible_episodes": covered,
        "upcoming_episodes": [total],
        "next_release_at": None,
        "last_source_label": "Sample manual history",
        "state": "partial",
        "status_label": "Sample partial availability",
        "release_qualities": ["1080p"],
        "last_result": f"Synthetic library check: {len(covered)} of {required} episodes verified; no connection or file was checked.",
        "timeline": [
            {
                "created_at": STAMP,
                "outcome": "partial",
                "detail": "Synthetic receipt: episodes 1, 3 and 4 matched; remaining episodes pending.",
            },
            {
                "created_at": STAMP,
                "outcome": "success",
                "detail": "Synthetic subtitle check: a switchable English track was recorded.",
            },
        ],
        "watch_timeline": [],
        "pending_confirmations": [],
        "other_assets": [],
        "current_assets": [
            {
                "id": 701,
                "provider_label": "Sample local import",
                "quality": "1080p",
                "status_label": "Downloaded (sample)",
                "progress": 100,
                "usable_coverage": covered,
                "source": source,
                "subtitle_state": "selectable",
                "subtitle_label": "Selectable English subtitles",
                "subtitle_detail": "Synthetic verification receipt. No media bytes or playback were checked.",
                "subtitles": [
                    {
                        "language": "eng",
                        "codec": "ass",
                        "switchable": True,
                        "sync_policy": "exact_release",
                        "source": "sample embedded track",
                    }
                ],
                "last_error_label": "",
                "last_error": "",
                "storage_backend": "sample local library",
                "probe": {"outcome": "Sample verified receipt", "verified_at": STAMP},
            }
        ],
    }


def prepare_environment(data: Path):
    if any(name.startswith("OMAKASE_") for name in os.environ):
        raise SystemExit("Showcase refuses inherited OMAKASE_* configuration. Use a clean process.")
    if list(data.iterdir()):
        raise SystemExit("Showcase requires a newly created empty temporary directory")
    os.environ.update(
        OMAKASE_DATA_DIR=str(data),
        OMAKASE_PLUS_PRIVATE="true",
        OMAKASE_PLUS_MASTER_KEY=secrets.token_urlsafe(48),
        OMAKASE_PLUS_INVITE=secrets.token_urlsafe(32),
        OMAKASE_ALLOWED_HOSTS="127.0.0.1,localhost,testserver",
        OMAKASE_PLUS_HOUSEHOLDS_ENABLED="true",
        OMAKASE_PLUS_LANGUAGE_SETTINGS_ENABLED="true",
        PYTHON_DOTENV_DISABLED="1",
    )
    return dict(os.environ)


def import_without_dotenv():
    """Suppress the package's custom .env parser BEFORE its first import.

    PYTHON_DOTENV_DISABLED alone cannot stop omakase/__init__.py's manual reader.
    The exact repository .env receives no read or metadata lookup. This also
    installs a process-local dotenv guard before importing application modules.
    """
    if "omakase" in sys.modules:
        raise RuntimeError("Showcase must start in its own fresh Python process")
    import dotenv

    def denied_dotenv(*_args, **_kwargs):
        GUARD["dotenv_calls"] += 1
        raise RuntimeError("dotenv loading is prohibited in the showcase")

    dotenv.load_dotenv = denied_dotenv
    import dotenv.main

    dotenv.main.load_dotenv = denied_dotenv
    actual_exists = Path.exists
    actual_read = Path.read_text
    env_path = ROOT / ".env"

    def exists(path):
        if path == env_path:
            GUARD["package_env_suppressed"] += 1
            return False
        return actual_exists(path)

    def read_text(path, *args, **kwargs):
        if path.name == ".env" or path.name.startswith(".env."):
            GUARD["env_read_attempts"] += 1
            raise RuntimeError("Configuration file reads are prohibited in the showcase")
        return actual_read(path, *args, **kwargs)

    # Keep the configuration read guard for the entire isolated preview process.
    Path.read_text = read_text
    with patch.object(Path, "exists", exists):
        __import__("omakase")
    if GUARD["package_env_suppressed"] != 1 or GUARD["env_read_attempts"] or GUARD["dotenv_calls"]:
        raise RuntimeError("Package environment isolation failed")


def prohibit_outbound():
    def denied(*_args, **_kwargs):
        raise OSError("External networking is disabled in the sample-account showcase")

    original_connect, original_ex = socket.socket.connect, socket.socket.connect_ex

    def connect(sock, address):
        # Windows asyncio's wake-up socketpair requires a loopback connection.
        return (
            original_connect(sock, address)
            if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}
            else denied()
        )

    def connect_ex(sock, address):
        return (
            original_ex(sock, address)
            if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}
            else denied()
        )

    socket.socket.connect, socket.socket.connect_ex = connect, connect_ex
    socket.create_connection = denied
    lookup = socket.getaddrinfo

    def local_lookup(host, *args, **kwargs):
        return (
            lookup(host, *args, **kwargs)
            if host in (None, "127.0.0.1", "localhost", "::1", b"127.0.0.1", b"localhost")
            else denied()
        )

    socket.getaddrinfo = local_lookup


EPISODE_STATES = (
    "complete-fallback",
    "resurfaced",
    "download-pending",
    "publication-pending",
    "no-result",
    "busy",
)


def episode_presentation_card(catalog, state):
    """Finite display-only Q9 states; executable service proof lives in tests."""
    if state not in EPISODE_STATES:
        raise ValueError("Unknown episode presentation state")
    title = title_presentation_card(catalog)
    total = title["episode_total"]
    fallback = [episode for episode in (6, 7, 8, 9, 10, 12, 13) if episode <= total]
    title.update(
        usable=total,
        required=total,
        plex_visible=total,
        usable_episodes=list(range(1, total + 1)),
        upcoming_episodes=[],
        can_recheck=True,
        active=False,
        release_status="FINISHED",
        release_status_label="Finished airing",
        next_check_at="",
        state="ready",
        status_label="Sample full library",
        progress_episodes=3,
        next_episode=4,
        progress_percent=round(300 / total),
        episode_qualities={},
        fallback_episodes=fallback,
        watch_revision=7,
        resurfaced=False,
        score_ten=9,
    )
    for episode in title["usable_episodes"]:
        quality = "720p" if episode in fallback else "1080p"
        title["episode_qualities"][episode] = {
            "quality": quality,
            "label": "720p fallback release" if quality == "720p" else "1080p release",
            "detail": "Synthetic release-quality presentation; no physical media or current publication was verified.",
            "alternates": ["1080p"] if state == "publication-pending" and quality == "720p" else [],
        }
    if state == "resurfaced":
        title.update(
            watch_status="completed",
            progress_episodes=2,
            next_episode=3,
            required=3,
            usable=3,
            plex_visible=2,
            usable_episodes=[1, 2, 3],
            upcoming_episodes=list(range(4, total + 1)),
            resurfaced=True,
            lifecycle_label="New episode found",
            release_status="RELEASING",
            release_status_label="Currently airing",
            active=True,
            state="partial",
            status_label="Episode 3 found · Plex availability unconfirmed",
            fallback_episodes=[],
            episode_qualities={
                episode: title["episode_qualities"][episode] for episode in (1, 2, 3)
            },
        )
    return title


def seed_library_fixture(db, *, user_id: int, state: str):
    """Native, empty synthetic scopes; no promotion of personal records or media."""
    if state not in LIBRARY_STATES:
        raise ValueError("Unknown library fixture")
    from omakase.plus import households

    personal = households.ensure_personal_library(db, user_id=user_id)
    db.commit()
    if state in {"multiple", "shared"}:
        households.create_household(db, user_id=user_id, name="Morgan household")
    if state != "shared":
        households.select_library(
            db, households.resolve_scope(db, viewer_id=user_id, library_id=personal)
        )


def create_showcase(data: Path, catalog, *, library_state="personal"):
    environment = prepare_environment(data)
    sys.path.insert(0, str(ROOT / "src"))
    prohibit_outbound()
    import_without_dotenv()
    from omakase.plus import movies_store
    from omakase.plus.anime_synopsis import store_synopsis
    from omakase.plus.auth import create_session, hash_password
    from omakase.plus.avatar import save_avatar
    from omakase.plus.db import _connect
    from omakase.plus.media_store import upsert_monitor
    from omakase.plus.secrets import store_secret
    from omakase.plus.watch.models import WatchEvent
    from omakase.plus.watch.service import ingest_watch_event

    db = _connect()
    uid = db.execute(
        "INSERT INTO users(email,password_hash) VALUES(?,?)",
        ("alex.morgan@example.com", hash_password(secrets.token_urlsafe(32))),
    ).lastrowid
    db.execute(
        "INSERT INTO taste_profiles(user_id,content) VALUES(?,?)",
        (uid, "Sample account: science fiction, adventure and character-driven stories."),
    )
    db.commit()
    store_secret(db, uid, "display_name", "Alex Morgan")
    seed_library_fixture(db, user_id=uid, state=library_state)
    save_avatar(db, uid, next(iter(catalog["anime"].values()))["poster"].read_bytes())
    for index, (aid, row) in enumerate(catalog["anime"].items()):
        count = max(1, min(1000, int(row.get("episodes", 12))))
        mid = upsert_monitor(
            db,
            user_id=uid,
            anilist_id=aid,
            title=row["title"],
            format="TV",
            release_status="FINISHED",
            season_number=1,
            expected_episodes=count,
            aired_episodes=count,
        )
        db.commit()
        if index < 4:
            ingest_watch_event(
                db,
                user_id=uid,
                monitor_id=mid,
                event=WatchEvent.progress(
                    source="manual",
                    episode=min(count, index + 2),
                    confidence="manual",
                    event_key=f"sample:{aid}",
                ),
                observed_at=row.get("sample_last_viewed_at") or STAMP,
            )
        if row.get("overview"):
            store_synopsis(db, aid, row["overview"])
    for index, (tmdb, row) in enumerate(catalog["tv"].items()):
        episodes = row.get("episodes") or []
        metadata = {
            "tmdb_id": tmdb,
            "title": row["title"],
            "year": str(row.get("year", "")),
            "overview": row.get("overview", ""),
            "poster_url": "cached-local-art",
            "backdrop_url": "cached-local-art",
            "in_collection": True,
            "genres": row.get("genres", []),
            "seasons": [],
            "catalog_subset": True,
        }
        known_air_dates = sorted(
            episode["air_date"]
            for episode in episodes
            if isinstance(episode.get("air_date"), str)
            and re.fullmatch(r"\d{4}-\d{2}-\d{2}", episode["air_date"])
        )
        if episodes:
            metadata["seasons"] = [
                {
                    "season_number": 1,
                    "name": "Season 1 · cached subset",
                    "episode_count": len(episodes),
                    "air_date": known_air_dates[0] if known_air_dates else None,
                    "episodes_loaded": False,
                }
            ]
        db.execute(
            "INSERT INTO tv_catalog(user_id,tmdb_id,metadata_json,updated_at) VALUES(?,?,?,?)",
            (uid, tmdb, json.dumps(metadata), STAMP),
        )
        db.execute(
            "INSERT INTO tv_history(user_id,tmdb_id,status,rating,status_source,rating_source,updated_at) VALUES(?,?,'watching',8,'manual','manual',?)",
            (uid, tmdb, STAMP),
        )
        for number, episode in enumerate(episodes, 1):
            entry = {
                "episode_number": number,
                "title": episode["title"],
                "air_date": episode.get("air_date"),
                "overview": episode.get("overview", ""),
            }
            db.execute(
                "INSERT INTO tv_episodes(user_id,tmdb_id,season_number,episode_number,metadata_json,watched,watch_source,plex_present,updated_at) VALUES(?,?,1,?,?,?,'manual',0,?)",
                (uid, tmdb, number, json.dumps(entry), int(number <= index + 1), STAMP),
            )
    for imdb, row in catalog["movies"].items():
        metadata = {
            key: row.get(key) for key in ("imdb_id", "tmdb_id", "title", "year", "overview")
        }
        metadata.update(poster_url="cached-local-art", backdrop_url="cached-local-art")
        movies_store.upsert_movie(db, uid, metadata, now=STAMP, identity_verified=False)
        db.execute(
            "INSERT INTO movie_history(user_id,imdb_id,tmdb_id,status,status_source,status_updated_at,rating,rating_source,created_at,updated_at) VALUES(?,?,?,'completed','manual',?,8,'manual',?,?)",
            (uid, imdb, row.get("tmdb_id"), STAMP, STAMP, STAMP),
        )
    picks = [
        {
            "title": row["title"],
            "media_id": aid,
            "anilist_id": aid,
            "source": "anilist",
            "reasoning": "Sample recommendation for layout review. Personal fit has not been computed.",
            "best_match_from_history": "Sample taste profile",
            "predicted_score": 8.4,
            "episodes": row.get("episodes", 12),
        }
        for aid, row in list(catalog["anime"].items())[:4]
    ]
    run_id = db.execute(
        "INSERT INTO run_history(user_id,source,model,picks,created_at) VALUES(?,?,?,?,?)",
        (uid, "anilist", "sample-layout-fixture", json.dumps(picks), STAMP),
    ).lastrowid
    db.commit()
    session = create_session(db, uid)
    db.close()
    from fastapi import Depends, HTTPException
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
    from fastapi.routing import APIRoute
    from jinja2 import ChoiceLoader, DictLoader

    from omakase.plus import product_views, routes
    from omakase.plus.catalog_search import AnimeCatalogSearch, CatalogSearchError
    from omakase.plus.middleware import require_user
    from omakase.plus.movie_discovery import MovieDiscoveryService
    from omakase.plus.movies import MovieService
    from omakase.plus.tv_service import TVService
    from omakase.web.server import app

    if library_state == "unavailable":
        from omakase.plus import households

        def unavailable_navigation(*_args, **_kwargs):
            raise households.HouseholdError("Synthetic optional-library metadata unavailable")

        households.navigation_context = unavailable_navigation

    # Actual application templates, with only the demo disclosure text replaced.
    loader = routes.templates.env.loader
    base, _, _ = loader.get_source(routes.templates.env, "base.html")
    base = base.replace("Synthetic design review", BANNER).replace(
        "Sample titles and history. Production data is disconnected; external actions are blocked.",
        "Real media and cached artwork. Account, watch history, ratings and availability are demonstration fixtures.",
    )
    overrides = {"base.html": base}
    for name in ("login.html", "signup.html"):
        source, _, _ = loader.get_source(routes.templates.env, name)
        overrides[name] = source.replace(
            '<body class="auth-body">',
            '<body class="auth-body"><p class="product-notice" role="status">'
            + BANNER
            + ". "
            + STATE_NOTICE
            + "</p>",
        )
    routes.templates.env.loader = ChoiceLoader([DictLoader(overrides), loader])
    routes.templates.context_processors.append(lambda request: {"synthetic_review": True})
    actual_titles = product_views.library_titles

    def demo_titles(db, *, user_id, lifecycle="active", state="all", **filters):
        cards = actual_titles(db, user_id=user_id, lifecycle="all", state="all", **filters)
        for card in cards:
            count = card["required"]
            card.update(
                state="ready",
                status_label="Demo ready",
                usable=count,
                plex_visible=count,
                usable_episodes=list(range(1, count + 1)),
                plex_visible_episodes=list(range(1, count + 1)),
                provider_summary="Sample availability fixture",
                last_source_label="Sample history",
                new_episode_ready=False,
            )
            card["lifecycle"] = "in_progress" if card["progress_episodes"] else "ready_to_start"
        return [
            card
            for card in cards
            if (lifecycle in {"all", "active"} or card["lifecycle"] == lifecycle)
            and (state == "all" or card["state"] == state)
        ]

    product_views.library_titles = routes.library_titles = demo_titles

    actual_title_detail = product_views.title_detail

    def demo_title_detail(db, *, user_id, monitor_id):
        card = actual_title_detail(db, user_id=user_id, monitor_id=monitor_id)
        if card is not None:
            card["usable_episodes"] = list(range(1, card["usable"] + 1))
            card["plex_visible_episodes"] = list(range(1, card["plex_visible"] + 1))
        return card

    product_views.title_detail = routes.title_detail = demo_title_detail

    async def tv_search(self, user_id, query):
        return [
            card
            for card in await self.list(user_id)
            if query.casefold() in card["title"].casefold()
        ]

    TVService.search = tv_search

    async def movie_list(self, user_id, *, view="all"):
        movies_store.require_user(self.db, user_id)
        return (
            [self._card(user_id, row) for row in movies_store.list_movies(self.db, user_id)]
            if view in {"all", "discover"}
            else []
        )

    async def movie_search(self, user_id, query):
        return [
            card
            for card in await movie_list(self, user_id)
            if query.casefold() in card["title"].casefold()
        ]

    MovieService.list, MovieService.search = movie_list, movie_search

    async def anime_detail(self, media_id):
        row = catalog["anime"].get(media_id)
        if row is None:
            raise CatalogSearchError("not_found")
        card = {
            "id": media_id,
            "anilist_id": media_id,
            "title": row["title"],
            "year": row.get("year"),
            "format": "TV",
            "status": "FINISHED",
            "episode_total": row.get("episodes"),
            "description": row.get("overview", ""),
            "genres": row.get("genres", []),
            "poster_url": f"/plus/media/{media_id}/poster",
            "url": f"/plus/catalog/anime/{media_id}",
            "media_type": "anime",
            "watch_status": "unknown",
            "progress": 0,
            "rating": None,
        }
        return self._enrich(card)

    async def anime_search(self, query):
        return [
            await anime_detail(self, aid)
            for aid, row in catalog["anime"].items()
            if query.casefold() in row["title"].casefold() or str(aid) == query
        ][:8]

    AnimeCatalogSearch.search, AnimeCatalogSearch.detail = anime_search, anime_detail

    async def movie_options(self, user_id):
        movies_store.require_user(self.db, user_id)
        return {"genres": [{"id": 878, "name": "Science fiction"}]}

    async def movie_discover(self, user_id, **filters):
        cards = await movie_list(self.movies, user_id)
        if not filters.get("include_completed", False):
            completed = {
                row["imdb_id"]
                for row in self.db.execute(
                    "SELECT imdb_id FROM movie_history WHERE user_id=? AND status='completed' AND status_source!='none'",
                    (user_id,),
                )
            }
            cards = [card for card in cards if card["imdb_id"] not in completed]
        return {"results": cards}

    MovieDiscoveryService.options, MovieDiscoveryService.discover = movie_options, movie_discover

    sample_user = SimpleNamespace(id=uid, email="alex.morgan@example.com")
    first_anime = next(iter(catalog["anime"]))
    sample_title = catalog["anime"][first_anime]["title"]
    removal_states = {"review", "queued", "running", "blocked", "partial", "failed", "completed"}
    state_routes = [f"/plus/demo/states/removal/{state}" for state in sorted(removal_states)]
    state_routes += [f"/plus/demo/states/settings/{state}" for state in ("saved", "error")]
    state_routes += [f"/plus/demo/states/login/{state}" for state in ("validation", "failed")]
    state_routes += ["/plus/demo/states/signup/validation", "/plus/demo/states/title/populated"]
    state_routes += [f"/plus/demo/states/episodes/{state}" for state in EPISODE_STATES]
    state_routes += [
        f"/plus/demo/states/onboarding/{path}"
        for path in ("existing_stack", "fresh_plex", "app_only")
    ]
    for journey in ("tv", "tv-detail"):
        state_routes += [
            f"/plus/demo/states/{journey}/{state}"
            for state in ("single", "multiple", "partial", "complete", "unknown")
        ]
    route_map = {
        "disclosure": STATE_NOTICE,
        "account": "Alex Morgan (synthetic)",
        "get_journeys": [
            "/plus/home",
            "/plus/library",
            "/plus/tv",
            "/plus/movies",
            "/plus/search",
            "/plus/discover",
            "/plus/discover/movies",
            "/plus/add",
            "/plus/login",
            "/plus/signup",
            "/plus/health/overview",
            f"/plus/catalog/anime/{first_anime}",
            f"/plus/discover/title/{first_anime}?run={run_id}",
        ],
        "rendered_states": state_routes,
        "mutations": "All POST/PUT/PATCH/DELETE return 405. Rendered states do not verify persistence or worker execution.",
        "gaps": [
            "Live provider errors and recovery",
            "Real sign-in/signup validation and saved settings",
            "Real media removal and retry execution",
            "Physical-device acceptance",
        ],
    }
    app.state.showcase_route_map = route_map
    app.state.showcase_library_state = library_state

    async def fixture_routes():
        return JSONResponse(route_map)

    async def fixture_state(request: Request, journey: str, state: str, user=Depends(require_user)):
        context = {
            **routes._product_context(request, user, active_page="library"),
            "synthetic_review": True,
            "showcase_state_notice": STATE_NOTICE,
        }
        if journey == "removal" and state in removal_states:
            status = {
                "queued": "awaiting_plex",
                "running": "plex_removed",
                "partial": "provider_failed",
                "failed": "provider_failed",
                "completed": "complete",
            }.get(state)
            progress = (
                None
                if status is None
                else {
                    "status": status,
                    "scope": "full",
                    "deleted_count": 1 if state in {"partial", "completed"} else 0,
                    "failed_count": 1 if state in {"partial", "failed"} else 0,
                    "pending_count": 0,
                    "retained_count": 1,
                    "detail": "Synthetic outcome. No worker or destructive action ran.",
                }
            )
            removal = {
                "title": sample_title,
                "monitor_id": 1,
                "anilist_id": first_anime,
                "season_number": 1,
                "progress": progress,
                "eligible_provider_count": 2,
                "retained_asset_count": 1,
                "shared_plex_title": state == "blocked",
                "asset_count": 3,
            }
            response = routes.templates.TemplateResponse(
                request,
                "title_remove.html",
                {
                    **context,
                    "removal": removal,
                    "error": STATE_NOTICE,
                },
            )
        elif journey == "episodes" and state in EPISODE_STATES:
            outcomes = {
                "download-pending": "1080p downloads are in progress. Existing usable copies are retained.",
                "publication-pending": "Higher releases found; replacement publication is unconfirmed. Existing usable copies are retained.",
                "no-result": "Episode recheck finished. No publishable 1080p replacement found. Existing usable copies are retained.",
                "busy": "An episode check is already running. No additional check was started.",
            }
            rendered = routes.templates.TemplateResponse(
                request,
                "title.html",
                {
                    **context,
                    "title": episode_presentation_card(catalog, state),
                    "message": outcomes.get(state, ""),
                    "related_titles": [],
                    "retry_notice": "",
                },
            )
            body = rendered.body.decode("utf-8")
            notice = (
                '<p class="product-notice" role="status">'
                + STATE_NOTICE
                + " Episode quality, history and outcomes on this page are synthetic presentation fixtures.</p>"
            )
            body = re.sub(r"(<main\b[^>]*>)", lambda match: match.group(1) + notice, body, count=1)
            if state == "busy":
                body = body.replace(
                    'data-busy-label="Checking episodes…">Find missing episodes &amp; 1080p',
                    'data-busy-label="Checking episodes…" disabled aria-busy="true">Checking episodes…',
                )
            response = HTMLResponse(body)
        elif journey == "title" and state == "populated":
            rendered = routes.templates.TemplateResponse(
                request,
                "title.html",
                {
                    **context,
                    "title": title_presentation_card(catalog),
                    "message": "",
                    "related_titles": [],
                    "retry_notice": "",
                },
            )
            disclosure = (
                '<p class="product-notice" role="status">'
                + STATE_NOTICE
                + " Populated synthetic release, subtitle and library-check receipts. No media or provider was checked.</p>"
            )
            # The notice sits inside the actual main element; the template itself is unchanged.
            body = rendered.body.decode("utf-8")
            body = re.sub(
                r"(<main\b[^>]*>)", lambda match: match.group(1) + disclosure, body, count=1
            )
            response = HTMLResponse(body)
        elif journey in {"tv", "tv-detail"} and state in {
            "single",
            "multiple",
            "partial",
            "complete",
            "unknown",
        }:
            card = tv_presentation_card(catalog, state)
            payload = {
                **context,
                "active_page": "tv",
                "cards": [card],
                "card": card,
                "query": "",
                "view": "all",
                "selected_season": 1,
            }
            rendered = routes.templates.TemplateResponse(
                request, "tv.html" if journey == "tv" else "tv_detail.html", payload
            )
            disclosure = (
                '<p class="product-notice" role="status">'
                + STATE_NOTICE
                + " Synthetic season inventory. No Plex connection was checked.</p>"
            )
            response = HTMLResponse(
                rendered.body.replace(
                    b'<div class="tv-page">',
                    b'<div class="tv-page">' + disclosure.encode("utf-8"),
                    1,
                )
            )
        elif journey == "onboarding" and state in {"existing_stack", "fresh_plex", "app_only"}:
            setup_path, label = {
                "existing_stack": ("existing", "Connect an existing stack"),
                "fresh_plex": ("fresh_plex", "Set up optional Plex"),
                "app_only": ("standalone", "Explore with Omakase only"),
            }[state]
            response = routes.templates.TemplateResponse(
                request,
                "onboarding.html",
                {
                    **context,
                    "onboarding_step": 2,
                    "onboarding_setup_path": setup_path,
                    "onboarding_setup_path_label": label,
                    "onboarding_intent": "explore",
                    "onboarding_intent_label": "Explore first",
                    "onboarding_connections": [],
                    "onboarding_completed": False,
                    "onboarding_import_configured": False,
                    "onboarding_library_configured": False,
                },
            )
            rendered = response.body.replace(
                b'<div class="onboarding">',
                b'<div class="onboarding"><p class="product-notice" role="status">'
                + STATE_NOTICE.encode("utf-8")
                + b" Setup guidance only. Nothing was installed or configured.</p>",
                1,
            )
            response = HTMLResponse(rendered)
        elif journey == "settings" and state in {"saved", "error"}:
            db = _connect()
            try:
                shadow = Request({**request.scope, "path": "/plus/settings/downloads"})
                response = await routes.settings_page(
                    shadow, saved=STATE_NOTICE if state == "saved" else "", user=sample_user, db=db
                )
                payload = dict(response.context)
                if state == "error":
                    payload["acquisition_policy_warning"] = ""
                    payload["settings_error"] = (
                        "Could not save download preferences. " + STATE_NOTICE
                    )
                else:
                    payload["saved"] = (
                        "Download preferences saved (sample presentation only). " + STATE_NOTICE
                    )
                response = routes.templates.TemplateResponse(request, "settings.html", payload)
            finally:
                db.close()
        elif journey in {"login", "signup"} and state in {"validation", "failed"}:
            error = (
                "Enter a valid email and password."
                if state == "validation"
                else "Invalid email or password."
            )
            response = routes.templates.TemplateResponse(
                request, journey + ".html", {"error": error + " " + STATE_NOTICE}
            )
        else:
            raise HTTPException(404, "Unknown presentation fixture")
        response.headers["X-Omakase-Fixture"] = "presentation-only-no-mutation"
        return response

    app.router.routes.insert(0, APIRoute("/plus/demo/routes", fixture_routes, methods=["GET"]))
    app.router.routes.insert(
        0, APIRoute("/plus/demo/states/{journey}/{state}", fixture_state, methods=["GET"])
    )

    def art_for(kind, identifier, variant):
        row = catalog[kind].get(identifier)
        if row is None or variant not in {"poster", "banner", "backdrop"}:
            raise HTTPException(404, "Cached demo artwork not found")
        target = row["banner" if variant in {"banner", "backdrop"} else "poster"]
        return FileResponse(
            target,
            media_type=mimetypes.guess_type(target)[0],
            headers={"Cache-Control": "public, max-age=3600"},
        )

    async def anime_poster(anilist_id: int):
        return art_for("anime", anilist_id, "poster")

    async def anime_banner(anilist_id: int):
        return art_for("anime", anilist_id, "banner")

    async def tv_art(tmdb_id: int, variant: str = "poster"):
        return art_for("tv", tmdb_id, variant)

    async def movie_art(imdb_id: str, variant: str = "poster"):
        return art_for("movies", imdb_id, variant)

    for path, endpoint in (
        ("/plus/media/{anilist_id}/poster", anime_poster),
        ("/plus/media/{anilist_id}/banner", anime_banner),
        ("/plus/tv/{tmdb_id}/artwork", tv_art),
        ("/plus/movies/{imdb_id}/artwork", movie_art),
    ):
        app.router.routes.insert(0, APIRoute(path, endpoint, methods=["GET"]))

    def assert_isolated():
        if (
            dict(os.environ) != environment
            or {name for name in os.environ if name.startswith("OMAKASE_")} != ENV_NAMES
        ):
            raise RuntimeError("Showcase configuration changed unexpectedly")
        if (
            GUARD["dotenv_calls"]
            or GUARD["env_read_attempts"]
            or GUARD["package_env_suppressed"] != 1
        ):
            raise RuntimeError("Showcase dotenv isolation failed")

    assert_isolated()
    app.state.showcase_check_environment = assert_isolated

    @app.middleware("http")
    async def demo_boundary(request, call_next):
        assert_isolated()
        if request.url.hostname not in {"127.0.0.1", "localhost", "testserver"}:
            return JSONResponse({"detail": "Loopback showcase only"}, 421)
        if request.method not in {"GET", "HEAD"}:
            return JSONResponse(
                {"detail": "Demo is read-only. External actions are disabled."}, 405
            )
        path = request.url.path
        if path == "/":
            return RedirectResponse("/plus/home", 303)
        allowed = path in {
            "/plus",
            "/plus/",
            "/plus/home",
            "/plus/library",
            "/plus/tv",
            "/plus/movies",
            "/plus/activity",
            "/plus/onboarding",
            "/plus/account/avatar",
            "/plus/auth/google/account",
            "/plus/households/join",
            "/plus/search",
            "/plus/search/suggestions",
            "/plus/discover",
            "/plus/discover/movies",
            "/plus/add",
            "/plus/login",
            "/plus/signup",
            "/plus/health/overview",
            "/plus/setup",
            "/plus/api/notifications",
        } or path.startswith(
            (
                "/plus/static/",
                "/plus/media/",
                "/plus/tv/",
                "/plus/movies/",
                "/plus/library/",
                "/plus/libraries/",
                "/plus/settings",
                "/plus/demo/",
                "/plus/catalog/anime/",
                "/plus/discover/title/",
            )
        )
        if not allowed:
            return JSONResponse({"detail": "Outside the sample-account showcase"}, 404)
        if path.startswith("/plus/") and request.cookies.get("omakase_session") != session:
            response = RedirectResponse(str(request.url), 303)
            response.set_cookie(
                "omakase_session", session, httponly=True, samesite="strict", path="/plus"
            )
            return response
        response = await call_next(request)
        response.headers["X-Omakase-Demo"] = "real-media-sample-account-read-only"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
        )
        assert_isolated()
        return response

    return app


def check(app, catalog):
    from fastapi.testclient import TestClient

    anime = next(iter(catalog["anime"]))
    tv = next(iter(catalog["tv"]))
    movie = next(iter(catalog["movies"]))
    from omakase.plus.db import _connect

    def saved_outcomes():
        connection = _connect()
        try:
            return {
                table: [
                    tuple(row)
                    for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")
                ]
                for table in ("media_title_removals", "watch_events", "movie_history", "tv_history")
            }
        finally:
            connection.close()

    before = saved_outcomes()
    with TestClient(app) as client:
        for path in (
            "/plus/home",
            "/plus/tv",
            f"/plus/tv/{tv}",
            "/plus/movies",
            f"/plus/movies/{movie}",
            "/plus/activity",
        ):
            response = client.get(path)
            assert response.status_code == 200, (path, response.status_code, response.text[:100])
            assert BANNER in response.text, path
        route_map = client.get("/plus/demo/routes").json()
        for path in route_map["get_journeys"]:
            response = client.get(path)
            assert response.status_code == 200, (path, response.status_code, response.text[:100])
            assert BANNER in response.text, path
            assert client.post(path).status_code == 405
        for path in route_map["rendered_states"]:
            response = client.get(path)
            assert response.status_code == 200, (path, response.status_code, response.text[:100])
            assert STATE_NOTICE in response.text
            assert response.headers["X-Omakase-Fixture"] == "presentation-only-no-mutation"
            assert client.post(path).status_code == 405
        for kind, rows in catalog.items():
            title = next(iter(rows.values()))["title"]
            response = client.get("/plus/search", params={"kind": kind, "q": title})
            assert response.status_code == 200 and title in response.text
            assert client.get("/plus/search/suggestions", params={"kind": kind, "q": title}).json()[
                "results"
            ]
        assert (
            "No titles found" in client.get("/plus/search?q=NoCachedTitleMatchHere").text
            or not client.get("/plus/search/suggestions?q=NoCachedTitleMatchHere").json()["results"]
        )
        assert client.get("/plus/demo/states/removal/unknown").status_code == 404
        populated = client.get("/plus/demo/states/title/populated").text
        assert "Alex Morgan" in populated and "/plus/account/avatar?v=" in populated
        assert ("Library selection is temporarily unavailable" in populated) == (
            app.state.showcase_library_state == "unavailable"
        )
        feed = client.get("/plus/api/notifications")
        assert feed.status_code == 200
        assert "connect-src 'self'" in feed.headers["Content-Security-Policy"]
        assert "[SAMPLE RELEASE]" in populated and "Selectable English subtitles" in populated
        assert "Latest library check" in populated and "Synthetic library check" in populated
        assert (
            "No sync runs yet" not in populated
            and "A provider has not produced a candidate yet" not in populated
        )
        assert "8.4" in client.get("/plus/discover").text
        assert '<article class="movie-card">' not in client.get("/plus/discover/movies").text
        assert (
            '<article class="movie-card">'
            in client.get("/plus/discover/movies?include_completed=true").text
        )
        detail = client.get("/plus/library/1").text
        assert "Media verified" in detail and "Not yet verified" not in detail
        assert 'role="alert"' in client.get("/plus/demo/states/settings/error").text
        assert catalog["anime"][anime]["title"] in client.get("/plus/home").text
        for path in (
            f"/plus/media/{anime}/poster",
            f"/plus/media/{anime}/banner",
            f"/plus/tv/{tv}/artwork",
            f"/plus/movies/{movie}/artwork",
        ):
            assert client.get(path).headers["content-type"].startswith("image/")
        for step in range(1, 5):
            assert client.get(f"/plus/onboarding?step={step}").status_code == 200
        for section in (
            "",
            "/downloads",
            "/preferences",
            "/tracking",
            "/sources",
            "/storage",
            "/connections",
            "/household",
            "/languages",
            "/profile",
            "/agents",
            "/updates",
        ):
            assert client.get("/plus/settings" + section).status_code == 200
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            assert client.request(method, "/plus/settings/downloads").status_code == 405
        assert client.get("/plus/account/avatar").headers["content-type"].startswith("image/png")
        assert "alex.morgan@example.com" in client.get("/plus/settings").text
        assert "Alex Morgan" in client.get("/plus/settings").text
        assert client.post("/plus/onboarding/intent").status_code == 405
        assert client.get("/plus/auth/google").status_code == 404
        assert client.get("/plus/home", headers={"host": "untrusted.invalid"}).status_code in {
            400,
            421,
        }
        app.state.showcase_check_environment()
        assert GUARD == {"package_env_suppressed": 1, "dotenv_calls": 0, "env_read_attempts": 0}
    assert saved_outcomes() == before, "GET/fixture checks must not alter saved outcomes"
    try:
        socket.create_connection(("example.invalid", 443))
    except OSError:
        pass
    else:
        raise AssertionError("Outbound network guard failed")
    print(
        json.dumps({"route_state_map": app.state.showcase_route_map}, ensure_ascii=False),
        flush=True,
    )
    print(
        "PASS: actual mixed-media UI, cached catalog search/detail/discovery, auth/setup/health/settings GETs, labeled presentation states; writes/host/outbound denied; dotenv never called/read; clean environment",
        flush=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=4416)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--art-dir", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--library-state", choices=LIBRARY_STATES, default="personal")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Use an unprivileged local port")
    catalog = load_manifest(args.manifest, args.art_dir)
    with tempfile.TemporaryDirectory(prefix="omakase-sample-showcase-") as temporary:
        app = create_showcase(Path(temporary), catalog, library_state=args.library_state)
        if args.check:
            check(app, catalog)
        else:
            import uvicorn

            print(
                f"Real-media sample-account showcase: http://127.0.0.1:{args.port}/plus/home",
                flush=True,
            )
            uvicorn.run(
                app,
                host="127.0.0.1",
                port=args.port,
                log_level="warning",
                access_log=False,
                proxy_headers=False,
            )


if __name__ == "__main__":
    main()
