"""Labelled synthetic fixture server for the Home/shell visual review.

Not production code and not part of the shipped app. It seeds two profiles with
synthetic titles, resume positions and watch history shaped like the real read
models, generates local gradient artwork so the cards have wide art without any
network call, and reuses the Requests fixture rows for the Requests screens.

Artwork routes are registered before the product router, so they answer instead
of the real AniList/TMDB proxies. Nothing here contacts a provider, Plex or the
live server.

Run:
  .venv/Scripts/python.exe scripts/fixtures/home-shell/server.py --port 8796
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import sqlite3
import tempfile
from pathlib import Path

ORANGE = "#e08d4f"
TEAL = "#2f7f8f"
PLUM = "#6b4a8f"
SLATE = "#2a3244"

ANIME = [
    # (anilist_id, title, episodes, progress, season)
    (151807, "Solo Leveling", 12, 4, 1),
    (170732, "Bleach: Thousand-Year Blood War", 24, 6, 2),
    (123456, "Moonlit Requiem", 13, 1, 1),
    (654321, "The Horizon Spell", 12, 11, 1),
]
MOVIES = [
    # (imdb_id, tmdb_id, title, year, backdrop, last_viewed, position, duration)
    ("tt1160419", 438631, "Dune", 2021, "bright", "2026-11-13T21:10:00Z", 4020, 9360),
    ("tt1375666", 27205, "Inception", 2010, "dark", "2026-11-12T20:05:00Z", 3780, 8880),
    ("tt0111161", 278, "The Shawshank Redemption", 1994, "", None, None, None),
]
TV = [
    # (tmdb_id, title, seasons, watched, resume, backdrop)
    (1396, "MobLand", 1, {2: True, 3: True}, (1, 3, 2280, 3540), "dark"),
    (76479, "The Boys", 3, {1: True}, (1, 1, 900, 3600), ""),
    (94997, "House of the Dragon", 2, {1: True, 2: True}, None, "wide"),
]


def _art(moment: str, title: str) -> bytes:
    """Deterministic gradient artwork with the title burned in."""
    from PIL import Image, ImageDraw

    width, height = (1280, 720) if moment == "banner" else (480, 720)
    if moment == "banner":
        top, bottom = {
            "bright": ((248, 231, 200), (214, 160, 96)),
            "dark": ((16, 18, 30), (38, 30, 58)),
            "wide": ((24, 30, 44), (52, 70, 96)),
        }.get("dark" if title.startswith("Mob") else "wide", ((30, 34, 48), (16, 18, 26)))
        if title in {"Dune", "Inception"} and moment == "banner":
            top, bottom = (
                ((244, 226, 196), (206, 150, 92))
                if title == "Dune"
                else ((18, 22, 36), (44, 36, 66))
            )
    else:
        top, bottom = (34, 40, 58), (14, 16, 24)
    image = Image.new("RGB", (width, height), top)
    draw = ImageDraw.Draw(image)
    for y in range(height):
        blend = y / max(1, height - 1)
        draw.line(
            [(0, y), (width, y)],
            fill=tuple(round(a + (b - a) * blend) for a, b in zip(top, bottom)),
        )
    draw.text((28, height - 60), title[:34], fill=(246, 243, 250))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _load_requests_seed():
    path = Path(__file__).resolve().parent.parent / "request-status" / "server.py"
    spec = importlib.util.spec_from_file_location("request_status_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _resume(db, user_id, library_id, domain, canonical, season, episode, position, duration):
    version = "a" * 64
    db.execute(
        """INSERT OR REPLACE INTO web_playback_resumes(
             target_key,user_id,library_id,domain,canonical_id,season_number,episode_number,
             media_version,duration_seconds,position_seconds,revision,intervals_json,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,0,'[]',?)""",
        (
            f"fixture:{user_id}:{domain}:{canonical}:{season}:{episode}",
            user_id,
            library_id,
            domain,
            str(canonical),
            season,
            episode,
            version,
            duration,
            position,
            1_762_000_000 + user_id,
        ),
    )


def _seed_profile(db, user_id, *, variant="a"):
    from omakase.plus.media_store import upsert_monitor
    from omakase.plus.watch.models import WatchEvent
    from omakase.plus.watch.service import ingest_watch_event

    library_id = db.execute(
        "SELECT library_id FROM personal_libraries WHERE user_id=?", (user_id,)
    ).fetchone()[0]
    for index, (anilist_id, title, episodes, progress, season) in enumerate(ANIME):
        monitor_id = upsert_monitor(
            db,
            user_id=user_id,
            anilist_id=anilist_id,
            title=title,
            format="TV",
            release_status="FINISHED",
            season_number=season,
            expected_episodes=episodes,
            aired_episodes=episodes,
        )
        if progress:
            ingest_watch_event(
                db,
                user_id=user_id,
                monitor_id=monitor_id,
                event=WatchEvent.progress(
                    source="manual",
                    episode=progress,
                    confidence="manual",
                    event_key=f"fixture:{user_id}:anime:{anilist_id}",
                ),
                observed_at=f"2026-11-1{index}T20:00:00Z",
            )
        if progress == 4:
            _resume(db, user_id, library_id, "anime", anilist_id, season, progress, 640, 1440)
    for imdb_id, tmdb_id, title, year, backdrop, viewed, position, duration in MOVIES:
        poster = f"/plus/movies/{imdb_id}/artwork?variant=poster"
        art = f"/plus/movies/{imdb_id}/artwork?variant=backdrop" if backdrop else None
        db.execute(
            """INSERT OR REPLACE INTO movie_catalog(
                 user_id,imdb_id,tmdb_id,title,year,poster_url,backdrop_url,identity_verified,
                 updated_at)
               VALUES(?,?,?,?,?,?,?,1,'2026-11-01T00:00:00Z')""",
            (user_id, imdb_id, tmdb_id, title, year, poster, art or poster),
        )
        db.execute(
            """INSERT OR REPLACE INTO movie_history(
                 user_id,imdb_id,tmdb_id,status,status_source,status_updated_at,last_viewed_at,
                 created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                user_id,
                imdb_id,
                tmdb_id,
                "watching" if viewed else "unwatched",
                "manual" if viewed else "none",
                "2026-11-01T00:00:00Z",
                viewed,
                "2026-11-01T00:00:00Z",
                "2026-11-01T00:00:00Z",
            ),
        )
        if viewed and position:
            _resume(db, user_id, library_id, "movie", imdb_id, 0, 0, position, duration)
    for tmdb_id, title, seasons, watched, resume, backdrop in TV:
        episodes = []
        for season in range(1, seasons + 1):
            count = 3 if season == 1 else 2
            for number in range(1, count + 1):
                episodes.append(
                    {
                        "season_number": season,
                        "episode_number": number,
                        "title": f"{title} S{season}E{number}",
                        "air_date": f"2026-10-0{min(9, number + season)}",
                    }
                )
        metadata = {
            "tmdb_id": tmdb_id,
            "title": title,
            "poster_url": f"/plus/tv/{tmdb_id}/artwork?variant=poster",
            "backdrop_url": f"/plus/tv/{tmdb_id}/artwork?variant=backdrop" if backdrop else None,
            "seasons": [
                {"season_number": s, "episode_count": 3 if s == 1 else 2, "episodes_loaded": True}
                for s in range(1, seasons + 1)
            ],
        }
        db.execute(
            "INSERT OR REPLACE INTO tv_catalog(user_id,tmdb_id,metadata_json,updated_at) "
            "VALUES(?,?,?,'2026-11-01T00:00:00Z')",
            (user_id, tmdb_id, json.dumps(metadata)),
        )
        for episode in episodes:
            number = episode["episode_number"]
            season = episode["season_number"]
            is_watched = bool(watched.get(season)) and (
                season != 1 or number <= max(watched.values() and [1] or [1])
            )
            if season == 1:
                is_watched = number in {1, 2} if watched.get(1) else False
            db.execute(
                """INSERT OR REPLACE INTO tv_episodes(
                     user_id,tmdb_id,season_number,episode_number,metadata_json,watched,
                     watch_source,plex_present,updated_at)
                   VALUES(?,?,?,?,?,?,?,1,?)""",
                (
                    user_id,
                    tmdb_id,
                    season,
                    number,
                    json.dumps(episode),
                    1 if is_watched else 0,
                    "manual" if is_watched else "none",
                    f"2026-11-0{min(9, number)}T19:00:00Z",
                ),
            )
        if resume:
            season, episode, position, duration = resume
            _resume(db, user_id, library_id, "tv", tmdb_id, season, episode, position, duration)
        db.execute(
            "INSERT OR REPLACE INTO tv_history(user_id,tmdb_id,status,status_source,updated_at) "
            "VALUES(?,?,?,?,?)",
            (user_id, tmdb_id, "watching", "manual", "2026-11-10T20:00:00Z"),
        )
    for anilist_id, title in ((210001, "Crown of Ashes"), (210002, "Our Spring Days")):
        db.execute(
            "INSERT OR REPLACE INTO anilist_plannings(user_id,anilist_id,title,status,added_at) "
            "VALUES(?,?,?,'PLANNING','2026-11-05T00:00:00Z')",
            (user_id, anilist_id, title),
        )
    if variant == "b":
        # Profile B ships a different shelf order and hides anime.
        db.execute(
            """INSERT OR REPLACE INTO viewer_home_layout(user_id,rows_json,revision,updated_at)
               VALUES(?,?,1,'2026-11-14T00:00:00Z')""",
            (
                user_id,
                json.dumps(
                    [
                        {"id": "continue-tv", "hidden": False},
                        {"id": "continue-movies", "hidden": False},
                        {"id": "continue-anime", "hidden": True},
                        {"id": "watchlist", "hidden": False},
                        {"id": "ready-to-watch", "hidden": True},
                        {"id": "recently-watched", "hidden": True},
                    ]
                ),
            ),
        )
    if variant == "c":
        # Profile C watched a dark-artwork show most recently.
        db.execute(
            "UPDATE web_playback_resumes SET updated_at = updated_at + 900000 "
            "WHERE user_id=? AND domain='tv'",
            (user_id,),
        )
    if variant == "e":
        # Profile E leads with a title that has no artwork at all.
        db.execute(
            "UPDATE movie_catalog SET poster_url=NULL, backdrop_url=NULL "
            "WHERE user_id=? AND imdb_id='tt0111161'",
            (user_id,),
        )
        _resume(db, user_id, library_id, "movie", "tt0111161", 0, 0, 3200, 8520)
        db.execute(
            "UPDATE movie_history SET last_viewed_at='2026-11-14T21:00:00Z' "
            "WHERE user_id=? AND imdb_id='tt0111161'",
            (user_id,),
        )
        db.execute(
            "UPDATE web_playback_resumes SET updated_at = updated_at + 900000 "
            "WHERE user_id=? AND domain='movie' AND canonical_id='tt0111161'",
            (user_id,),
        )
    db.commit()


def main():
    parser = argparse.ArgumentParser(description="Home/shell visual fixture server")
    parser.add_argument("--port", type=int, default=8796)
    parser.add_argument("--web-player", action="store_true")
    args = parser.parse_args()
    data_dir = Path(tempfile.mkdtemp(prefix="omakase-home-fixture-"))
    os.environ["OMAKASE_DATA_DIR"] = str(data_dir)
    os.environ["OMAKASE_PLUS_PRIVATE"] = "true"
    os.environ["OMAKASE_PLUS_REVIEW_MODE"] = "true"
    os.environ.pop("OMAKASE_PLUS_HOUSEHOLDS_ENABLED", None)
    if args.web_player:
        os.environ["OMAKASE_PLUS_WEB_PLAYER_ENABLED"] = "true"
    else:
        os.environ.pop("OMAKASE_PLUS_WEB_PLAYER_ENABLED", None)

    from fastapi import FastAPI, Response
    from fastapi.responses import JSONResponse, RedirectResponse

    from omakase.plus import households
    from omakase.plus.auth import create_session
    from omakase.plus.db import run_migrations
    from omakase.plus.deps import get_db
    from omakase.plus.secrets import store_secret

    db_path = data_dir / "omakase-plus.db"
    db = sqlite3.connect(db_path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    run_migrations(db)
    profiles = {}
    for key, email, variant in (
        ("a", "viewer@example.test", "a"),
        ("b", "second@example.test", "b"),
        ("c", "dark@example.test", "c"),
        ("e", "missing@example.test", "e"),
    ):
        db.execute("INSERT INTO users(email,password_hash) VALUES(?, 'fixture')", (email,))
        user_id = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()[0]
        households.ensure_personal_library(db, user_id=user_id)
        _seed_profile(db, user_id, variant=variant)
        if args.web_player:
            store_secret(db, user_id, "realdebrid_api_key", "synthetic-fixture-token")
        profiles[key] = {"user_id": user_id, "email": email, "session": create_session(db, user_id)}
    requests_fixture = _load_requests_seed()
    requests_fixture._seed(
        db,
        profiles["a"]["user_id"],
        __import__("omakase.plus.request_status", fromlist=["request_status"]),
    )
    inventory = {requests_fixture.RESOLVED: ("f" * 64, "2026-11-11T08:00:00Z")}
    service = requests_fixture.ReadOnlyMovieService(inventory)
    import omakase.plus.for_you as for_you

    for_you.MovieService = lambda _db, _connections: service

    app = FastAPI(title="Omakase Home/shell fixture")

    def fixture_db():
        connection = sqlite3.connect(db_path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def art(moment: str, title: str) -> Response:
        return Response(
            _art(moment, title),
            media_type="image/png",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/plus/media/{anilist_id}/banner")
    async def fixture_anime_banner(anilist_id: int):
        title = next((a[1] for a in ANIME if a[0] == anilist_id), "Anime")
        return art("banner", title)

    @app.get("/plus/media/{anilist_id}/poster")
    async def fixture_anime_poster(anilist_id: int):
        title = next((a[1] for a in ANIME if a[0] == anilist_id), "Anime")
        return art("poster", title)

    @app.get("/plus/movies/{imdb_id}/artwork")
    async def fixture_movie_artwork(imdb_id: str, variant: str = "poster"):
        row = db.execute(
            "SELECT title FROM movie_catalog WHERE imdb_id=? LIMIT 1", (imdb_id,)
        ).fetchone()
        title = row["title"] if row else "Movie"
        missing = db.execute(
            "SELECT backdrop_url FROM movie_catalog WHERE imdb_id=? LIMIT 1", (imdb_id,)
        ).fetchone()
        if variant == "backdrop" and missing and not (missing["backdrop_url"] or ""):
            return Response(status_code=404)
        return art("banner" if variant == "backdrop" else "poster", title)

    @app.get("/plus/tv/{tmdb_id}/artwork")
    async def fixture_tv_artwork(tmdb_id: int, variant: str = "poster"):
        row = db.execute(
            "SELECT metadata_json FROM tv_catalog WHERE tmdb_id=? LIMIT 1", (tmdb_id,)
        ).fetchone()
        title = json.loads(row["metadata_json"])["title"] if row else "TV"
        return art("banner" if variant == "backdrop" else "poster", title)

    app.dependency_overrides[get_db] = fixture_db

    @app.middleware("http")
    async def fixture_profile(request, call_next):
        """Bind every request to a fixture profile.

        `?as=b` wins, otherwise the `fixture_profile` cookie set by
        `/fixture/enter` decides. The session cookie is rewritten server-side so
        POST flows (Edit Home) authenticate without cookie replacement tricks.
        """
        key = request.query_params.get("as") or request.cookies.get("fixture_profile")
        if key in profiles:
            kept = [
                (name, value)
                for name, value in request.scope["headers"]
                if name.lower() == b"cookie"
            ]
            rest = kept[0][1].decode() if kept else ""
            pairs = [
                part
                for part in rest.split("; ")
                if part and not part.startswith("omakase_session=")
            ]
            pairs.append(f"omakase_session={profiles[key]['session']}")
            headers = [
                (name, value)
                for name, value in request.scope["headers"]
                if name.lower() != b"cookie"
            ]
            headers.append((b"cookie", "; ".join(pairs).encode()))
            request.scope["headers"] = headers
        return await call_next(request)

    from omakase.plus.routes import router

    app.include_router(router)

    @app.get("/fixture/enter")
    async def fixture_enter(as_: str = "a", next: str = "/plus/home"):
        key = as_ if as_ in profiles else "a"
        response = RedirectResponse(next, status_code=303)
        response.set_cookie("fixture_profile", key, path="/")
        response.set_cookie(
            "omakase_session", profiles[key]["session"], path="/plus", httponly=True
        )
        return response

    @app.get("/fixture/state")
    async def fixture_state():
        connection = fixture_db()
        try:
            layouts = {
                row["user_id"]: row["rows_json"]
                for row in connection.execute("SELECT user_id,rows_json FROM viewer_home_layout")
            }
            return JSONResponse(
                {
                    "fixture": True,
                    "profiles": {key: value["user_id"] for key, value in profiles.items()},
                    "email": {key: value["email"] for key, value in profiles.items()},
                    "sessions": {key: value["session"] for key, value in profiles.items()},
                    "layouts": layouts,
                }
            )
        finally:
            connection.close()

    import uvicorn

    print(f"fixture ready on http://127.0.0.1:{args.port} data_dir={data_dir}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
