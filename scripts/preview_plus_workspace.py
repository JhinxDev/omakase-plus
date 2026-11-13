"""Loopback-only actual-template review. No application or provider imports."""

from __future__ import annotations

import argparse
import html
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "src/omakase/plus/templates"
STATIC = ROOT / "src/omakase/plus/static"
SNAPSHOT = None
ARTWORK_ROOT = None
MOVIE_ARTWORK_ROOT = None
AVATAR_PATH = None


def template_environment():
    return Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]))


def fixture_context(page, query=None):
    query = query or {}
    empty = query.get("fixture", [""])[0] == "empty"
    titles = (
        []
        if empty
        else [
            dict(
                id=index,
                anilist_id=index,
                title=title,
                state=state,
                lifecycle=lifecycle,
                lifecycle_label=label,
                watch_status=watch,
                progress_episodes=progress,
                episode_total=12,
                progress_percent=round(progress / 12 * 100),
                next_episode=progress + 1 if progress < 12 else None,
                required=12,
                usable=usable,
                release_qualities=["1080p", "720p"] if index == 1 else ["1080p"] if usable else [],
                fallback_episodes=[11, 12] if index == 1 else [],
                plex_visible=usable,
                status_label=status,
                last_source_label=source,
            )
            for index, title, state, lifecycle, label, watch, progress, usable, status, source in [
                (
                    1,
                    "The Mapmaker's Garden",
                    "ready",
                    "in_progress",
                    "In progress",
                    "watching",
                    4,
                    12,
                    "Ready to watch",
                    "Manual confirmation",
                ),
                (
                    2,
                    "Luminous Harbor",
                    "ready",
                    "ready_to_start",
                    "Ready to start",
                    "unwatched",
                    0,
                    12,
                    "Ready to watch",
                    "AniList",
                ),
                (
                    3,
                    "Letters from the Northern Observatory",
                    "attention",
                    "waiting",
                    "Not ready",
                    "watching",
                    3,
                    7,
                    "5 episodes missing; provider access restricted",
                    "Plex",
                ),
                (
                    4,
                    "A Small Season of Rain",
                    "ready",
                    "in_progress",
                    "In progress",
                    "watching",
                    8,
                    12,
                    "Ready to watch",
                    "Plex",
                ),
                (
                    5,
                    "After the Last Train",
                    "ready",
                    "completed",
                    "Completed",
                    "completed",
                    12,
                    12,
                    "Completed",
                    "Manual confirmation",
                ),
                (
                    6,
                    "The Clockmaker and the Tides",
                    "ready",
                    "ready_to_start",
                    "Ready to start",
                    "unwatched",
                    0,
                    12,
                    "Ready to watch",
                    "AniList",
                ),
            ]
        ]
    )
    events = (
        []
        if empty
        else [
            dict(
                id=1,
                title="The Mapmaker's Garden",
                monitor_id=1,
                category="success",
                outcome="Verified",
                detail="12 of 12 required episodes have usable media and Plex visibility.",
                created_at="2026-10-05T14:20:00Z",
                covered=12,
                required=12,
                accepted=12,
            ),
            dict(
                id=2,
                title="Letters from the Northern Observatory",
                monitor_id=3,
                category="attention",
                outcome="Provider restricted",
                detail="Provider reported download access restricted. Retry does not mark missing episodes ready.",
                created_at="2026-10-05T13:40:00Z",
                covered=7,
                required=12,
                accepted=0,
            ),
            dict(
                id=3,
                title="The Clockmaker and the Tides",
                monitor_id=6,
                category="running",
                outcome="Finding episodes",
                detail="Eligible-source search queued; existing verified media retained.",
                created_at="2026-10-05T12:10:00Z",
                covered=0,
                required=12,
                accepted=0,
            ),
        ]
    )
    lifecycle = query.get("lifecycle", ["active"])[0]
    search = query.get("query", [""])[0]
    state = query.get("state", ["all"])[0]
    content = query.get("content_type", ["all"])[0]
    shown = [
        item
        for item in titles
        if (
            lifecycle == "all"
            or (lifecycle == "active" and item["lifecycle"] != "completed")
            or item["lifecycle"] == lifecycle
        )
        and search.lower() in item["title"].lower()
        and (state == "all" or item["state"] == state)
        and content != "movies"
    ]
    outcome = query.get("outcome", ["all"])[0]
    context = dict(
        email="review@example.invalid",
        csrf_token="synthetic-no-auth",
        asset_version="workspace-review",
        active_page=page,
        review_mode=False,
        synthetic_review=True,
        message="Sample error: no production actions are available in this design review."
        if query.get("fixture", [""])[0] == "error"
        else "",
        continue_watching=[item for item in titles if item["lifecycle"] == "in_progress"],
        ready_to_watch=[item for item in titles if item["lifecycle"] == "ready_to_start"],
        confirmation_needed=[],
        attention=[item for item in titles if item["state"] == "attention"],
        new_episodes=[],
        recent_activity=events,
        titles=shown,
        saved_picks=[]
        if empty or lifecycle not in {"active", "all"}
        else [dict(anilist_id=11, title="The Quiet Astronaut", added_at="2026-10-11T12:00:00Z")],
        title_suggestions=[item["title"] for item in titles],
        lifecycle_filter=lifecycle,
        query=search,
        state_filter=state,
        provider_filter=query.get("provider", ["all"])[0],
        content_type_filter=content,
        plex_library=dict(connected=not empty, title_count=6, generated_at="2026-10-05T14:20:00Z"),
        discover_mode=query.get("mode", ["recommend"])[0],
        anilist_username="sample-viewer",
        discovery_preferences={"mode": "pro", "count": 8, "lane": "best_match"},
        codex_worker={"online": True, "last_seen_at": "2026-10-11T12:00:00Z"},
        configured_backend="deepseek",
        taste_profile="Inventive worlds, characters worth caring about, and an ending that lands. I enjoy a slower pace when the atmosphere earns it.",
        runs=[],
        latest=None
        if empty
        else dict(
            id=1,
            source="anilist",
            created_at="2026-10-05T14:00:00Z",
            picks=[
                dict(
                    title="The Wind Archive",
                    reasoning="A fictional example: a quiet ensemble story with patient world-building, paired with the pace of your strongest sample ratings.",
                    best_match_from_history="The Mapmaker's Garden",
                    media_id=10,
                    anilist_id=10,
                    source="anilist",
                    predicted_score=8.4,
                    episodes=12,
                    airing_status="FINISHED",
                    availability="Not in your library",
                    availability_state="unknown",
                ),
                dict(
                    title="Letters from the Northern Observatory",
                    reasoning="Another fictional sample: a warm mystery told through a small group of people whose friendships matter as much as the answer.",
                    best_match_from_history="A Small Season of Rain",
                    media_id=3,
                    anilist_id=3,
                    source="anilist",
                    predicted_score=8.1,
                    episodes=12,
                    airing_status="FINISHED",
                    library_id=3,
                    availability="Ready to watch",
                    availability_state="ready",
                    verified_episodes=12,
                    required_episodes=12,
                ),
                dict(
                    title="After the Last Train",
                    reasoning="A fictional sample with an unusual setting and a quieter emotional arc. Try this when you want curiosity without a frantic pace.",
                    best_match_from_history="The Mapmaker's Garden",
                    media_id=4,
                    anilist_id=4,
                    source="anilist",
                    predicted_score=7.8,
                    episodes=12,
                    airing_status="RELEASING",
                    library_id=4,
                    availability="Waiting for episodes",
                    availability_state="pending",
                    verified_episodes=5,
                    required_episodes=8,
                ),
            ],
        ),
        watch_activity=[]
        if empty
        else [
            dict(
                category="success",
                title="A Small Season of Rain",
                outcome="Episode 8 confirmed",
                detail="Manual confirmation; progress saved locally.",
                created_at="2026-10-05T15:00:00Z",
            )
        ],
        tracker_actions=[]
        if empty
        else [
            dict(
                id=1,
                category="attention",
                title="After the Last Train",
                tracker="anilist",
                score=8,
                state="failed",
                detail="Sample sync failure; retry awaits your decision.",
            )
        ],
        events=[event for event in events if outcome == "all" or event["category"] == outcome],
        filter=outcome,
        total_pages=1,
        page=1,
        page_numbers=[1],
    )
    if page == "title":
        item_id = int(query.get("title_id", ["1"])[0])
        selected = next((item for item in titles if item["id"] == item_id), titles[0])
        context["active_page"] = "library"
        context["title"] = dict(
            selected,
            season_number=1,
            format="TV",
            release_status_label="Finished airing",
            genres=["Fantasy", "Adventure"],
            description="A fictional review story about a young mapmaker tracing an island that changes with the seasons. Sample artwork and data only.",
            usable_episodes=list(range(1, selected["usable"] + 1)),
            current_assets=[],
            other_assets=[],
            assets=[],
            timeline=[],
            watch_timeline=[],
            pending_confirmations=[],
            removal=None,
            publication_pending=False,
            last_result="Sample media verified",
            publication_detail="",
        )
        if query.get("fixture", [""])[0] == "airing":
            context["title"].update(
                release_status_label="Currently airing",
                episode_total=10,
                required=8,
                display_episode_count=10,
                upcoming_episodes=[9, 10],
                next_release_at="2026-10-19T14:00:00Z",
                usable=8,
                plex_visible=8,
                usable_episodes=list(range(1, 9)),
                progress_episodes=3,
                progress_percent=30,
                next_episode=4,
                release_qualities=["1080p"],
                fallback_episodes=[],
                last_source_label="Sample Plex inventory",
            )
    if page == "recommendation":
        selected = next(
            (
                pick
                for pick in context["latest"]["picks"]
                if pick["media_id"] == int(query.get("media_id", ["10"])[0])
            ),
            context["latest"]["picks"][0],
        )
        context.update(
            active_page="discover",
            run=context["latest"],
            media_id=selected["media_id"],
            pick=dict(
                selected,
                genres=["Drama", "Fantasy"],
                format="TV",
                studio="Sample studio",
                description="A fictional review synopsis: an archivist follows a trail of letters through a harbor town, piecing together the stories its inhabitants thought were lost.",
            ),
        )
    if page in {"login", "google_account"}:
        context.update(active_page="settings", google_enabled=not empty, google_linked=False)
    if page == "login" and query.get("fixture", [""])[0] == "error":
        context["error"] = context["message"]
    context["movies_enabled"] = True
    if SNAPSHOT is not None and not empty and page in {"home", "library"}:
        if page == "home":
            context.update(SNAPSHOT["home"])
        else:
            context["titles"] = [
                item
                for item in SNAPSHOT["titles"]
                if (
                    lifecycle == "all"
                    or (lifecycle == "active" and item["lifecycle"] != "completed")
                    or item["lifecycle"] == lifecycle
                )
                and search.casefold() in item["title"].casefold()
                and (state == "all" or item["state"] == state)
            ]
        context.update(synthetic_review=False, review_mode=True)
    return review_extensions(context, page, query)


def review_extensions(context, page, query):
    """Explicit synthetic fixtures for new viewing and account surfaces."""
    fixture = query.get("fixture", [""])[0]
    empty = fixture == "empty"
    samples = list(SNAPSHOT["home"].get("continue_watching", [])) if SNAPSHOT else []
    if SNAPSHOT:
        samples += SNAPSHOT["home"].get("ready_to_watch", [])
    if not samples:
        samples = context["continue_watching"] + context["ready_to_watch"]
    samples = list({item["anilist_id"]: item for item in samples}.values())
    context.update(
        avatar_available=AVATAR_PATH is not None, avatar_revision="review-only", watch_history=[]
    )
    catalog = [
        dict(
            id=item["anilist_id"],
            anilist_id=item["anilist_id"],
            title=item["title"],
            year=2026,
            format=item.get("format", "TV"),
            poster_url=f"/plus/media/{item['anilist_id']}/poster",
            url=f"/plus/catalog/anime/{item['anilist_id']}",
            watch_status=item.get("watch_status", "unknown"),
            progress=item.get("progress_episodes", 0),
            episode_total=item.get("episode_total"),
            rating=8.5 if index == 0 else None,
            library_id=item["id"],
            library_status=item["state"],
            description="A synopsis is not included in this local layout fixture.",
            genres=["Adventure", "Fantasy"],
            status="FINISHED",
            studio=None,
        )
        for index, item in enumerate(samples)
    ]
    if page == "discover" and context.get("latest"):
        context["latest"]["picks"] = [
            dict(
                title=item["title"],
                media_id=item["anilist_id"],
                anilist_id=item["anilist_id"],
                source="anilist",
                predicted_score=round(8.7 - index * 0.2, 1),
                episodes=item.get("episode_total") or 12,
                airing_status="FINISHED",
                format=item.get("format", "TV"),
                reasoning="Sample recommendation for layout review. Personal fit has not been computed in this preview.",
                best_match_from_history="",
                library_id=item["id"],
                availability_state=item["state"],
                availability="In your library",
                verified_episodes=item["usable"],
                required_episodes=item["required"],
            )
            for index, item in enumerate(samples[:8])
        ]
    if page == "recommendation" and catalog:
        selected = next(
            (item for item in catalog if str(item["anilist_id"]) == query.get("media_id", [""])[0]),
            catalog[0],
        )
        context.update(
            media_id=selected["anilist_id"],
            pick=dict(
                selected,
                source="anilist",
                predicted_score=8.7,
                reasoning="Sample recommendation for layout review. Personal fit has not been computed in this preview.",
                best_match_from_history="",
                availability="In your library",
                availability_state="ready",
            ),
        )
    if page == "title" and samples:
        selected_id = query.get("title_id", [str(samples[0]["id"])])[0]
        selected = next((item for item in samples if str(item["id"]) == selected_id), samples[0])
        context["title"].update(selected)
        context["title"].update(
            description="A synopsis is not included in this local review fixture.",
            current_assets=[],
            other_assets=[],
            assets=[],
            timeline=[],
            watch_timeline=[],
            pending_confirmations=[],
            removal=None,
            genres=["Adventure", "Fantasy"],
            usable_episodes=list(range(1, selected["usable"] + 1)),
            score_ten=8.5,
            last_result="Sample review state",
            cleanup_block="",
        )
        if fixture == "airing":
            context["title"].update(
                required=8,
                usable=8,
                plex_visible=8,
                release_episode_total=10,
                episode_total=10,
                display_episode_count=10,
                upcoming_episodes=[9, 10],
                usable_episodes=list(range(1, 9)),
                next_release_at="2026-11-01T14:00:00Z",
            )
    movie_samples = [
        ("tt0084787", 1091, "The Thing", 1982, "ready", "2160p"),
        ("tt2866360", 220289, "Coherence", 2013, "ready", "1080p"),
        ("tt5638642", 433808, "The Ritual", 2017, "verifying", "1080p"),
        ("tt2872718", 242582, "Nightcrawler", 2014, "searching", None),
        ("tt1130884", 11324, "Shutter Island", 2010, "ready", "1080p"),
        ("tt0114369", 807, "Se7en", 1995, "deferred", None),
        ("tt0230600", 1933, "The Others", 2001, "ready", "1080p"),
        ("tt0167404", 745, "The Sixth Sense", 1999, "acquiring", None),
    ]
    movies = [
        dict(
            imdb_id=imdb,
            tmdb_id=tmdb,
            title=title,
            year=year,
            state=state,
            code=state,
            resolution=resolution,
            overview="A sample movie card for local layout review. No new request or playback check was performed.",
            detail="Sample state for layout review. Actual media access is not connected in this preview.",
            requested=index != 3,
            requestable=True,
            poster_url="review-only",
            backdrop_url="review-only",
            reason="",
            retry_at="2026-10-27T20:00:00Z" if state == "deferred" else None,
            history={
                "status": "completed" if index == 1 else "unwatched",
                "rating": 8.5 if index == 1 else None,
            },
            plex_url=None,
            selected_audio="English" if state == "ready" else None,
            verified_at=None,
            edition=None,
            genres=["Mystery", "Thriller"],
        )
        for index, (imdb, tmdb, title, year, state, resolution) in enumerate(movie_samples)
    ]
    if page in {"movies", "movie_detail"}:
        discover = query.get("discover", [""])[0] == "1"
        search = query.get("q", [""])[0]
        shown = [card for card in movies if search.casefold() in card["title"].casefold()]
        view = query.get("view", ["all"])[0]
        if view == "ready":
            shown = [card for card in shown if card["state"] == "ready"]
        elif view == "requests":
            shown = [card for card in shown if card["requested"] and card["state"] != "ready"]
        context.update(
            active_page="discover" if discover else "movies",
            cards=[] if empty else shown,
            discover_movies=discover,
            query=search,
            view=view,
            notice="",
            action_card=None,
            movies_error="Sample service connection is unavailable." if fixture == "error" else "",
            genre_options={
                "genres": [
                    {"id": 9648, "name": "Mystery"},
                    {"id": 53, "name": "Thriller"},
                    {"id": 878, "name": "Science fiction"},
                ]
            },
            genre_id=int(query.get("genre_id", ["0"])[0])
            if query.get("genre_id", [""])[0].isdigit()
            else None,
            sort=query.get("sort", ["personalized"])[0],
            include_completed=query.get("include_completed", [""])[0] == "true",
        )
        if page == "movie_detail":
            context["card"] = next(
                (card for card in movies if card["imdb_id"] == query.get("imdb_id", [""])[0]),
                movies[0],
            )
        if fixture == "unresolved":
            context["cards"] = [
                dict(
                    movies[0],
                    imdb_id=None,
                    requestable=False,
                    poster_url=None,
                    title="Metadata-only preview",
                )
            ]
    if page in {"search", "catalog_anime"}:
        term = query.get("q", [""])[0]
        kind = "movies" if query.get("kind", [""])[0] == "movies" else "anime"
        results = (
            catalog
            if kind == "anime"
            else [
                dict(
                    card,
                    id=card["imdb_id"],
                    url=f"/plus/movies/{card['imdb_id']}",
                    poster_url=f"/plus/movies/{card['imdb_id']}/artwork",
                    watch_status=card["history"]["status"],
                    rating=card["history"]["rating"],
                    library_id=None,
                    progress=0,
                    format="Movie",
                )
                for card in movies
            ]
        )
        matched = [card for card in results if term.casefold() in card["title"].casefold()]
        context.update(
            active_page="search",
            query=term,
            search_query=term,
            kind=kind,
            cards=matched[:8] if term and not empty else [],
            search_error="Sample catalog service is unavailable." if fixture == "error" else "",
        )
        if page == "catalog_anime":
            context["card"] = next(
                (
                    card
                    for card in catalog
                    if str(card["anilist_id"]) == query.get("anime_id", [""])[0]
                ),
                catalog[0],
            )
    history = [
        dict(
            id=index,
            media_type="anime",
            anilist_id=item["anilist_id"],
            monitor_id=item["id"],
            title=item["title"],
            episode=max(1, item.get("progress_episodes", 1)),
            episode_title=None,
            season_number=item.get("season_number"),
            episode_total=item.get("episode_total"),
            progress_percent=item.get("progress_percent", 0),
            watched_at=None,
            recorded_at=f"2026-10-{27 - index:02}T14:00:00Z",
            source_label="Sample local record",
            time_label="Recorded",
            artwork_url=f"/plus/media/{item['anilist_id']}/poster",
            detail_url=f"/plus/library/{item['id']}",
        )
        for index, item in enumerate(samples[:8])
    ]
    if page == "activity" or fixture == "history":
        context.update(
            watch_history=[] if empty else history,
            activity_view=query.get("view", ["watch"])[0],
            history_page=1,
            history_has_more=False,
            synthetic_review=True,
            review_mode=False,
        )
    if page in {"settings", "connections", "avatar"}:
        context.update(
            active_page="settings",
            saved="",
            backends=["openai", "anthropic", "gemini"],
            stored_backend="openai",
            anilist_connected=True,
            anilist_oauth_ready=True,
            keys={"llm_api_key": "Your recommendation provider API key"},
            stored={"llm_api_key": True},
            catalog_keys={
                "prowlarr_indexer_ids": "Source IDs",
                "prowlarr_categories": "Anime categories",
            },
            catalog_stored={},
            catalog_configuration=SimpleNamespace(
                state="ready", label="Configured", detail="Prowlarr settings are saved."
            ),
            apple_tv_integration=None,
            apple_tv_watch_enabled=False,
            remove_completed_from_plex=False,
            acquisition_policy=SimpleNamespace(
                allowed_resolutions=("1080p", "2160p", "720p"),
                minimum_seeders=1,
                preferred_seeders=5,
                max_release_size_gib=80,
                allow_hardsub_fallback=False,
                prefer_trusted=True,
                prefer_batches=True,
            ),
            acquisition_policy_view=dict(
                resolution_order="1080p, then 2160p, then 720p",
                availability="At least one seeder",
                subtitles="English subtitles",
                source_safety="CAM, RAW and samples excluded",
                maximum_size="80 GiB",
                batch_behavior="Prefer complete seasons",
                revision="0000000000000000",
            ),
            acquisition_policy_warning="",
            supported_resolutions=["2160p", "1080p", "720p"],
            profile_notice="",
            profile_error="Sample image could not be decoded. Choose another picture."
            if fixture == "error"
            else "",
            connections_notice="",
            connections_error="Sample connection was not accepted." if fixture == "error" else "",
            movies_ready=not empty,
        )
        context["connections"] = []
        for service, name, mark, purpose in [
            ("realdebrid", "Real-Debrid", "RD", "Provider access for eligible anime downloads."),
            ("plex", "Plex", "PX", "Your movie library and verified playback access."),
            ("riven", "Riven", "RV", "Movie requests and acquisition progress."),
            ("seerr", "Seerr", "SE", "Movie discovery and exact metadata."),
            (
                "prowlarr",
                "Prowlarr",
                "PR",
                "Additional anime sources and individual source availability.",
            ),
        ]:
            degraded = service == "prowlarr" and not empty
            context["connections"].append(
                dict(
                    id=service,
                    name=name,
                    mark=mark,
                    purpose=purpose,
                    has_url=service != "realdebrid",
                    key_saved=not empty,
                    url_saved=not empty and service != "realdebrid",
                    label="Not connected"
                    if empty
                    else "Needs attention"
                    if degraded
                    else "Connected",
                    health=dict(
                        state="disconnected" if empty else "degraded" if degraded else "connected",
                        code="disconnected" if empty else "connected",
                        connected=not empty,
                        detail="Save your address and key to connect."
                        if empty
                        else "Prowlarr is connected. One indexer is temporarily unavailable."
                        if degraded
                        else "Sample read-only access check passed.",
                        checked_at=None if empty else "2026-10-27T18:00:00Z",
                        retry_at=None,
                        indexers=[
                            dict(
                                id=1,
                                name="Tokyo Toshokan",
                                state="cooldown",
                                disabled_till="2026-10-27T19:54:51Z",
                            )
                        ]
                        if degraded
                        else [],
                    ),
                )
            )
    return context


def render(page, query=None, environment=None):
    return (
        (environment or template_environment())
        .get_template(page + ".html")
        .render(**fixture_context(page, query))
    )


# Capture before the app's deferred script installs handlers. Server also rejects
# every mutation route. No real app/auth/database/provider is instantiated.
GUARD = """<script>
document.addEventListener('submit',function(e){if(e.target.method.toLowerCase()==='get'&&e.target.id!=='run-form')return;e.preventDefault();e.stopImmediatePropagation();alert('Synthetic review: this action is blocked.');},true);
document.addEventListener('click',function(e){if(e.target.closest('.btn-plan,.btn-feedback')){e.preventDefault();e.stopImmediatePropagation();alert('Synthetic review: this action is blocked.');}},true);
</script>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def send(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self'; style-src 'self'; script-src 'self' 'unsafe-inline'; connect-src 'self'; form-action 'self'; frame-src 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.send(405, b"Synthetic review: all mutations are blocked.", "text/plain; charset=utf-8")

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/plus"):
            self.send_response(302)
            self.send_header("Location", "/plus/home")
            self.end_headers()
            return
        if path.startswith("/plus/static/"):
            name = path.removeprefix("/plus/static/")
            if name not in {
                "dashboard.css",
                "streaming.css",
                "atmosphere.css",
                "cinema.css",
                "connections.css",
                "avatar.css",
                "profile.js",
                "search.js",
                "shelves.js",
                "geist-variable.woff2",
                "omakase-icon.svg",
                "workspace.css",
                "dashboard.js",
                "omakase-seal.svg",
            }:
                self.send(404, b"Not in the preview allowlist", "text/plain")
                return
            types = {
                ".css": "text/css",
                ".js": "text/javascript",
                ".svg": "image/svg+xml",
                ".woff2": "font/woff2",
            }
            self.send(200, (STATIC / name).read_bytes(), types[Path(name).suffix])
            return
        if path == "/plus/account/avatar":
            if AVATAR_PATH is not None and AVATAR_PATH.is_file():
                self.send(200, AVATAR_PATH.read_bytes(), "image/png")
            else:
                self.send(404, b"No preview avatar", "text/plain")
            return
        if path == "/plus/search/suggestions":
            self.send(
                200,
                json.dumps(
                    {"results": fixture_context("search", parse_qs(parsed.query))["cards"]}
                ).encode(),
                "application/json",
            )
            return
        if path.startswith("/plus/movies/") and path.endswith("/artwork"):
            imdb_id = path.split("/")[3]
            variant = (
                "backdrop"
                if parse_qs(parsed.query).get("variant", [""])[0] == "backdrop"
                else "poster"
            )
            if MOVIE_ARTWORK_ROOT is not None:
                cached = MOVIE_ARTWORK_ROOT / f"{imdb_id}.{variant}.image"
                if cached.is_file():
                    payload = cached.read_bytes()
                    self.send(
                        200,
                        payload,
                        "image/png"
                        if payload.startswith(b"\x89PNG")
                        else "image/webp"
                        if payload.startswith(b"RIFF")
                        else "image/jpeg",
                    )
                    return
            ordinal = sum(ord(char) for char in imdb_id) % 6
            colours = ["#314359", "#465044", "#453b59", "#524136", "#344e55", "#533c43"]
            svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 450"><rect width="300" height="450" fill="{colours[ordinal]}"/><circle cx="200" cy="130" r="90" fill="#c6b399" opacity=".7"/><path d="M0 450V250L150 140L300 380V450Z" fill="#101723"/><path d="M0 450V330L100 240L300 410V450Z" fill="#080e19"/></svg>'
            self.send(200, svg.encode(), "image/svg+xml")
            return
        if path.startswith("/plus/media/") and path.endswith(("/poster", "/banner")):
            try:
                ordinal = int(path.split("/")[3])
            except (ValueError, IndexError):
                self.send(404, b"No sample artwork", "text/plain")
                return
            if ARTWORK_ROOT is not None:
                suffix = ".banner.image" if path.endswith("/banner") else ".image"
                cached = ARTWORK_ROOT / f"{ordinal}{suffix}"
                if cached.is_file():
                    payload = cached.read_bytes()
                    content_type = (
                        "image/png"
                        if payload.startswith(b"\x89PNG")
                        else "image/webp"
                        if payload.startswith(b"RIFF")
                        else "image/jpeg"
                    )
                    self.send(200, payload, content_type)
                    return
                self.send(
                    200,
                    b'<svg xmlns="http://www.w3.org/2000/svg" width="2" height="3"/>',
                    "image/svg+xml",
                )
                return
            colours = ["#71868e", "#ba876c", "#7b8070", "#76859b", "#aaa08b", "#888179"]
            colour = colours[(ordinal - 1) % len(colours)]
            svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 400"><rect width="300" height="400" fill="{colour}"/><circle cx="220" cy="110" r="56" fill="#eee9dd"/><path d="M0 300L100 160L205 275L300 190V400H0Z" fill="#222b33"/><path d="M0 340L150 240L300 330V400H0Z" fill="#111b23"/><text x="22" y="48" fill="#fff" font-family="sans-serif" font-size="15" letter-spacing="3">STUDY {ordinal:02}</text></svg>'
            self.send(200, svg.encode(), "image/svg+xml")
            return
        page = path.removeprefix("/plus/")
        queries = parse_qs(parsed.query)
        if page.startswith("library/") and page.split("/")[-1].isdigit():
            queries["title_id"] = [page.split("/")[-1]]
            page = "title"
        if page.startswith("discover/title/") and page.split("/")[-1].isdigit():
            queries["media_id"] = [page.split("/")[-1]]
            page = "recommendation"
        if page == "auth/google/account":
            page = "google_account"
        if page == "settings/connections":
            page = "connections"
        if page == "settings/profile":
            page = "avatar"
        if page == "discover/movies":
            page = "movies"
            queries["discover"] = ["1"]
        if page.startswith("movies/tt") and page.count("/") == 1:
            queries["imdb_id"] = [page.split("/")[-1]]
            page = "movie_detail"
        if page.startswith("catalog/anime/"):
            queries["anime_id"] = [page.split("/")[-1]]
            page = "catalog_anime"
        if page in {
            "home",
            "library",
            "discover",
            "activity",
            "title",
            "recommendation",
            "login",
            "google_account",
            "signup",
            "settings",
            "connections",
            "avatar",
            "movies",
            "movie_detail",
            "search",
            "catalog_anime",
        }:
            body = render(page, queries).replace("</head>", GUARD + "</head>")
            controls = f'<nav class="fixture-tools" aria-label="Preview fixtures"><a href="{path}">Sample data</a><a href="{path}?fixture=empty">Empty state</a><a href="{path}?fixture=error">Notice state</a></nav>'
            body = body.replace(
                '<div class="product-workspace">', controls + '<div class="product-workspace">'
            )
            self.send(200, body.encode(), "text/html; charset=utf-8")
            return
        self.send(
            200,
            (
                '<!doctype html><html lang="en"><meta name="viewport" content="width=device-width"><title>Review boundary</title><body><h1>Synthetic review boundary</h1><p>This detail or utility route requires the actual authenticated application. No action was performed.</p><p><a href="/plus/library">Return to Library</a></p><code>'
                + html.escape(path)
                + "</code></body></html>"
            ).encode(),
            "text/html; charset=utf-8",
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=4414)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--artwork-root", type=Path)
    parser.add_argument("--movie-artwork-root", type=Path)
    parser.add_argument("--avatar", type=Path)
    args = parser.parse_args()
    if args.snapshot:
        SNAPSHOT = json.loads(args.snapshot.read_text(encoding="utf-8-sig"))
    ARTWORK_ROOT = args.artwork_root
    MOVIE_ARTWORK_ROOT = args.movie_artwork_root
    AVATAR_PATH = args.avatar
    print(f"Synthetic template review: http://127.0.0.1:{args.port}/plus/home", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
