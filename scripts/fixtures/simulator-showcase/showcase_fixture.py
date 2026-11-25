"""Rich loopback showcase server for genuine tvOS and iOS Simulator captures.

Populates Home, Continue Watching shelves, Anime/Movies/TV catalogs, Series
details, and Requests with real public series artwork fetched from AniList and
TVMaze (or clean dark gradient fallbacks when offline), so Apple TV and iPhone
simulators render authentic, high-contrast native SwiftUI interfaces without any
private server credentials or hostnames.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import ssl
import struct
import urllib.parse
import urllib.request
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
API = "/plus/api/native-tv-test/v1/library/"
PERSONAL = "a" * 32
PORT = int(os.environ.get("OMAKASE_FIXTURE_PORT", "8854"))

# Numeric artwork IDs matching `^artwork/[1-9][0-9]{0,17}/(banner|poster)$`
ANIME_CATALOG = [
    ("1001", "Frieren: Beyond Journey's End", "Sousou no Frieren", 8, 28, "Watched 8/28"),
    ("1002", "Solo Leveling", "Ore dake Level Up na Ken", 10, 12, "Watched 10/12"),
    ("1003", "Hell's Paradise", "Jigokuraku", 9, 13, "Watched 9/13"),
    ("1004", "Vinland Saga", "Vinland Saga", 14, 24, "Watched 14/24"),
    ("1005", "Jujutsu Kaisen", "Jujutsu Kaisen", 6, 24, "Watched 6/24"),
    ("1006", "DAN DA DAN", "Dandadan", 4, 12, "Watched 4/12"),
]

TV_CATALOG = [
    ("2001", "True Detective", "True Detective", "S1 E1", "60 min left", 12),
    ("2002", "The Gentlemen", "The Gentlemen", "S1 E2", "42 min left", 34),
    ("2003", "Severance", "Severance", "S2 E4", "28 min left", 61),
    ("2004", "Black Sails", "Black Sails", "S1 E1", "54 min left", 18),
    ("2005", "Mr Inbetween", "Mr Inbetween", "S2 E3", "19 min left", 45),
    ("2006", "Mr. Robot", "Mr. Robot", "S1 E8", "35 min left", 52),
]

MOVIE_CATALOG = [
    ("3001", "2001: A Space Odyssey", "1968", "110 min left", 26, (26, 32, 64)),
    ("3002", "Blade Runner 2049", "2017", "84 min left", 48, (48, 30, 56)),
    ("3003", "Arrival", "2016", "40 min left", 66, (22, 42, 54)),
    ("3004", "Ex Machina", "2014", "108 min", 0, (34, 28, 52)),
    ("3005", "Nightcrawler", "2014", "111 min left", 14, (24, 34, 48)),
    ("3006", "Prisoners", "2013", "153 min", 0, (38, 32, 46)),
]

ARTWORK_CACHE: dict[tuple[str, str], tuple[bytes, str]] = {}
STATE_TOKENS: set[str] = {"showcase-default-token"}


def _gradient_png(
    width: int, height: int, c1: tuple[int, int, int], c2: tuple[int, int, int]
) -> bytes:
    """Clean dark gradient PNG fallback so artwork never renders harsh flat blocks."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw_rows = bytearray()
    for y in range(height):
        t = y / max(1, height - 1)
        r = int(c1[0] * (1 - t) + c2[0] * t)
        g = int(c1[1] * (1 - t) + c2[1] * t)
        b = int(c1[2] * (1 - t) + c2[2] * t)
        raw_rows.append(0)
        raw_rows.extend(bytes((r, g, b)) * width)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw_rows)))
        + chunk(b"IEND", b"")
    )


def _fetch_bytes(url: str, timeout: float = 8.0) -> tuple[bytes, str] | None:
    if not url or not url.startswith("https://"):
        return None
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "OmakaseShowcaseFixture/1.0 (+https://omakase-plus.jhinx.dev)"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(5 * 1024 * 1024)
            ctype = (
                resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip() or "image/jpeg"
            )
            if len(data) > 1024:
                return data, ctype
    except Exception:
        return None
    return None


def _populate_anilist() -> None:
    query = """
    query ($search: String) {
      Media(search: $search, type: ANIME, sort: POPULARITY_DESC) {
        coverImage { extraLarge large }
        bannerImage
      }
    }
    """
    for art_id, _title, search_term, *_rest in ANIME_CATALOG:
        try:
            body = json.dumps({"query": query, "variables": {"search": search_term}}).encode(
                "utf-8"
            )
            req = urllib.request.Request(
                "https://graphql.anilist.co",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "OmakaseShowcaseFixture/1.0",
                },
            )
            with urllib.request.urlopen(req, timeout=8.0) as resp:
                payload = json.loads(resp.read(256 * 1024).decode("utf-8"))
            media = (payload.get("data") or {}).get("Media") or {}
            cover_url = ((media.get("coverImage") or {}).get("extraLarge")) or (
                (media.get("coverImage") or {}).get("large")
            )
            banner_url = media.get("bannerImage")
            if cover_url:
                fetched = _fetch_bytes(cover_url)
                if fetched:
                    ARTWORK_CACHE[(art_id, "poster")] = fetched
            if banner_url:
                fetched = _fetch_bytes(banner_url)
                if fetched:
                    ARTWORK_CACHE[(art_id, "banner")] = fetched
        except Exception:
            pass
        # Reuse poster if AniList lacked bannerImage
        if (art_id, "banner") not in ARTWORK_CACHE and (art_id, "poster") in ARTWORK_CACHE:
            ARTWORK_CACHE[(art_id, "banner")] = ARTWORK_CACHE[(art_id, "poster")]


def _populate_tvmaze() -> None:
    for art_id, _title, search_term, *_rest in TV_CATALOG:
        try:
            url = f"https://api.tvmaze.com/singlesearch/shows?q={urllib.parse.quote(search_term)}&embed=episodes"
            req = urllib.request.Request(url, headers={"User-Agent": "OmakaseShowcaseFixture/1.0"})
            with urllib.request.urlopen(req, timeout=8.0) as resp:
                show = json.loads(resp.read(1024 * 1024).decode("utf-8"))
            poster_url = ((show.get("image") or {}).get("original")) or (
                (show.get("image") or {}).get("medium")
            )
            if poster_url:
                fetched = _fetch_bytes(poster_url.replace("http://", "https://"))
                if fetched:
                    ARTWORK_CACHE[(art_id, "poster")] = fetched
            episodes = (show.get("_embedded") or {}).get("episodes") or []
            for ep in episodes[:4]:
                ep_img = ((ep.get("image") or {}).get("original")) or (
                    (ep.get("image") or {}).get("medium")
                )
                if ep_img:
                    fetched = _fetch_bytes(ep_img.replace("http://", "https://"))
                    if fetched:
                        ARTWORK_CACHE[(art_id, "banner")] = fetched
                        break
        except Exception:
            pass
        if (art_id, "banner") not in ARTWORK_CACHE and (art_id, "poster") in ARTWORK_CACHE:
            ARTWORK_CACHE[(art_id, "banner")] = ARTWORK_CACHE[(art_id, "poster")]


def populate_artwork(*, offline: bool = False) -> int:
    if not offline:
        _populate_anilist()
        _populate_tvmaze()
    # Ensure every ID has fallback artwork so no request ever 404s
    for art_id, *_ in ANIME_CATALOG + TV_CATALOG:
        if (art_id, "poster") not in ARTWORK_CACHE:
            ARTWORK_CACHE[(art_id, "poster")] = (
                _gradient_png(120, 180, (28, 30, 48), (11, 13, 20)),
                "image/png",
            )
        if (art_id, "banner") not in ARTWORK_CACHE:
            ARTWORK_CACHE[(art_id, "banner")] = (
                _gradient_png(320, 180, (36, 32, 62), (11, 13, 20)),
                "image/png",
            )
    for art_id, _t, _y, _r, _p, rgb in MOVIE_CATALOG:
        if (art_id, "poster") not in ARTWORK_CACHE:
            ARTWORK_CACHE[(art_id, "poster")] = (
                _gradient_png(120, 180, rgb, (9, 11, 18)),
                "image/png",
            )
        if (art_id, "banner") not in ARTWORK_CACHE:
            ARTWORK_CACHE[(art_id, "banner")] = (
                _gradient_png(320, 180, rgb, (9, 11, 18)),
                "image/png",
            )
    return len(ARTWORK_CACHE)


def build_home_payload() -> dict:
    tv_lead = TV_CATALOG[0]
    tv_cards = [
        {
            "id": f"tv-{art_id}",
            "domain": "tv",
            "title": title,
            "title_id": art_id,
            "subtitle": f"{ep} · {rem}",
            "detail_label": f"{pct}% watched",
            "progress": {"percent": pct, "remaining_label": rem},
            "play_url": f"/plus/watch/title/tv/{art_id}/1/1?play=1",
            "play_label": "Resume",
            "artwork": {
                "landscape": f"artwork/{art_id}/banner",
                "poster": f"artwork/{art_id}/poster",
            },
        }
        for art_id, title, _q, ep, rem, pct in TV_CATALOG[:4]
    ]
    anime_cards = [
        {
            "id": f"anime-{art_id}",
            "domain": "anime",
            "title": title,
            "title_id": art_id,
            "subtitle": f"Episode {watched + 1}",
            "detail_label": "Up next",
            "progress": {"percent": int(100 * watched / total), "remaining_label": status},
            "play_url": f"/plus/watch/{art_id}/{watched + 1}?play=1",
            "play_label": f"Play episode {watched + 1}",
            "episode": {"season": 1, "episode": watched + 1},
            "artwork": {
                "landscape": f"artwork/{art_id}/banner",
                "poster": f"artwork/{art_id}/poster",
            },
        }
        for art_id, title, _q, watched, total, status in ANIME_CATALOG[:5]
    ]
    movie_cards = [
        {
            "id": f"movie-{art_id}",
            "domain": "movie",
            "title": title,
            "title_id": f"tt000{art_id}",
            "subtitle": f"Movie · {year}",
            "detail_label": rem,
            "progress": {"percent": pct, "remaining_label": rem},
            "play_url": f"/plus/watch/title/movie/tt000{art_id}?play=1",
            "play_label": "Resume" if pct > 0 else "Play",
            "artwork": {
                "landscape": f"artwork/{art_id}/banner",
                "poster": f"artwork/{art_id}/poster",
            },
        }
        for art_id, title, year, rem, pct, _rgb in MOVIE_CATALOG[:3]
    ]
    return {
        "banner": {
            "title": tv_lead[1],
            "media_detail": f"TV Shows · {tv_lead[3]} · {tv_lead[4]}",
            "artwork": {
                "landscape": f"artwork/{tv_lead[0]}/banner",
                "poster": f"artwork/{tv_lead[0]}/poster",
            },
        },
        "rows": [
            {"id": "continue-tv", "label": "Continue Watching: TV Shows", "cards": tv_cards},
            {"id": "continue-anime", "label": "Continue Watching: Anime", "cards": anime_cards},
            {"id": "continue-movies", "label": "Continue Watching: Movies", "cards": movie_cards},
        ],
        "layout": {"rows": []},
    }


def build_catalog_payload(kind: str, search: str = "") -> dict:
    if kind == "anime":
        entries = [
            {
                "id": art_id,
                "title": title,
                "note": status,
                "artwork_id": art_id,
                "watch_path": f"/plus/watch/{art_id}/{watched + 1}",
                "progress": watched / total,
                "duration_seconds": 1420,
            }
            for art_id, title, _q, watched, total, status in ANIME_CATALOG
        ]
    elif kind == "tv":
        entries = [
            {
                "id": art_id,
                "title": title,
                "note": f"{ep} · {rem}",
                "artwork_id": art_id,
                "watch_path": f"/plus/watch/title/tv/{art_id}/1/1",
                "progress": pct / 100.0,
                "duration_seconds": 3300,
            }
            for art_id, title, _q, ep, rem, pct in TV_CATALOG
        ]
    else:
        entries = [
            {
                "id": f"tt000{art_id}",
                "title": title,
                "note": f"{year} · {rem}",
                "artwork_id": art_id,
                "watch_path": f"/plus/watch/title/movie/tt000{art_id}",
                "progress": pct / 100.0,
                "duration_seconds": 7200,
            }
            for art_id, title, year, rem, pct, _rgb in MOVIE_CATALOG
        ]
    if search:
        entries = [e for e in entries if search.casefold() in e["title"].casefold()]
    return {
        "items": entries,
        "featured": entries[0] if entries and not search else None,
        "continue_watching": entries[:3] if not search else [],
    }


def build_episodes_payload(title_id: str) -> dict:
    numeric = re.sub(r"\D", "", title_id) or "1001"
    if numeric not in {a[0] for a in ANIME_CATALOG + TV_CATALOG}:
        numeric = "2001"
    domain = "anime" if numeric.startswith("1") else "tv"
    eps = [
        {
            "id": str(ep),
            "title": f"Episode {ep}",
            "note": "S1 · 44 min",
            "watch_path": f"/plus/watch/title/tv/{numeric}/1/{ep}"
            if domain == "tv"
            else f"/plus/watch/{numeric}/{ep}",
            "episode_artwork": f"episode-artwork/{domain}/{numeric}/1/{ep}",
            "progress": 1.0 if ep == 1 else (0.4 if ep == 2 else 0.0),
            "watched": ep == 1,
            "duration_seconds": 2640,
        }
        for ep in range(1, 7)
    ]
    return {
        "items": eps,
        "title_summary": "Follows investigators and interlocking storylines across seasons in your private library.",
        "availability": {"state": "ready", "seasons": [1, 2]},
    }


def build_requests_payload() -> dict:
    return {
        "categories": [
            {
                "key": "active",
                "title": "Active",
                "requests": [
                    {
                        "kind": "tv",
                        "title_id": "2002",
                        "title": "The Gentlemen",
                        "state": "partial",
                        "label": "15 / 16 episodes ready",
                        "status_label": "15 / 16 Ready",
                        "detail": "15 of 16 episodes ready to watch · 94%",
                        "can_check": True,
                        "last_checked_at": "2026-11-25T06:20:00Z",
                        "stages": [
                            {
                                "stage": "saved",
                                "label": "Request saved",
                                "state": "complete",
                                "occurred_at": "2026-11-25T06:00:00Z",
                            },
                            {
                                "stage": "checked",
                                "label": "Sources checked · 2 matches eligible",
                                "state": "complete",
                                "occurred_at": "2026-11-25T06:20:00Z",
                            },
                        ],
                    },
                    {
                        "kind": "tv",
                        "title_id": "2005",
                        "title": "Mr Inbetween",
                        "state": "partial",
                        "label": "19 / 26 episodes ready",
                        "status_label": "19 / 26 Ready",
                        "detail": "19 of 26 episodes ready to watch · 73%",
                        "can_check": True,
                        "last_checked_at": "2026-11-25T06:19:00Z",
                        "stages": [
                            {
                                "stage": "saved",
                                "label": "Request saved",
                                "state": "complete",
                                "occurred_at": "2026-11-25T06:00:00Z",
                            },
                        ],
                    },
                ],
            },
            {"key": "attention", "title": "Needs attention", "requests": []},
            {"key": "history", "title": "History", "requests": []},
        ]
    }


class ShowcaseHandler(BaseHTTPRequestHandler):
    server_version = "OmakaseShowcaseFixture/1"

    def log_message(self, format: str, *args) -> None:
        print(f"[fixture] {self.command} {self.path.split('?')[0]}", flush=True)

    def reply(
        self, status: int, body: bytes = b"", *, content_type: str = "application/json"
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json_reply(self, status: int, data: dict) -> None:
        self.reply(status, json.dumps(data).encode("utf-8"))

    def do_POST(self) -> None:
        route = urlsplit(self.path)
        if route.path == API + "device":
            token = secrets.token_urlsafe(32)
            STATE_TOKENS.add(token)
            return self.json_reply(
                200, {"device_token": token, "user_code": "OMKS-2468", "expires_in": 300}
            )
        if route.path == API + "unlink":
            return self.json_reply(200, {"unlinked": True})
        if route.path in (API + "for-you/request", API + "for-you/request/check"):
            return self.json_reply(200, build_requests_payload()["categories"][0]["requests"][0])
        self.json_reply(200, {"ok": True})

    def do_GET(self) -> None:
        route = urlsplit(self.path)
        if route.path == "/qa/health":
            return self.json_reply(200, {"ok": True, "artwork_loaded": len(ARTWORK_CACHE)})
        if route.path == API + "device":
            return self.json_reply(200, {"expires_in": 2_592_000})
        if route.path == API + "profile":
            return self.json_reply(200, {"id": "1", "name": "Jhinx", "has_avatar": False})
        if route.path == API + "libraries":
            return self.json_reply(
                200, {"items": [{"id": PERSONAL, "name": "Personal", "kind": "personal"}]}
            )
        if route.path == API + "home":
            return self.json_reply(200, build_home_payload())
        if route.path == API + "requests":
            return self.json_reply(200, build_requests_payload())
        if route.path == API + "catalog":
            q = parse_qs(route.query)
            return self.json_reply(
                200, build_catalog_payload(q.get("kind", ["anime"])[0], q.get("q", [""])[0])
            )
        if route.path == API + "episodes":
            q = parse_qs(route.query)
            return self.json_reply(200, build_episodes_payload(q.get("title_id", ["2001"])[0]))
        if route.path == API + "details":
            return self.json_reply(
                200,
                {
                    "title": "The Gentlemen",
                    "episode_title": "Refined Aggression",
                    "episode_number": 1,
                    "note": "TV series · 3 seasons",
                    "summary": "When aristocratic Eddie inherits the family estate, he discovers it anchors a sprawling empire.",
                    "artwork": "episode-artwork/tv/2002/1/1",
                },
            )
        if route.path == API + "watchlist":
            return self.json_reply(200, {"items": build_catalog_payload("anime")["items"][:4]})
        if route.path in (API + "for-you", API + "catalog-search"):
            q = parse_qs(route.query)
            category = (
                "all" if route.path.endswith("catalog-search") else q.get("category", ["anime"])[0]
            )
            kind = "movie" if category == "movies" else ("tv" if category == "tv" else "anime")
            cards = [
                {
                    "id": item["id"],
                    "kind": kind,
                    "title": item["title"],
                    "summary": item["note"],
                    "can_request": False,
                }
                for item in build_catalog_payload(kind)["items"][:5]
            ]
            return self.json_reply(
                200,
                {
                    "category": category,
                    "rows": [{"key": "featured", "title": "Featured Picks", "cards": cards}],
                    "requests": [],
                    "warnings": [],
                },
            )
        m_art = re.fullmatch(re.escape(API) + r"artwork/(\d+)/(banner|poster)", route.path)
        if m_art:
            data, ctype = ARTWORK_CACHE.get((m_art.group(1), m_art.group(2))) or (
                _gradient_png(160, 120, (32, 28, 52), (9, 11, 18)),
                "image/png",
            )
            return self.reply(200, data, content_type=ctype)
        m_ep = re.fullmatch(
            re.escape(API) + r"episode-artwork/(anime|tv)/(\d+)/(\d+)/(\d+)", route.path
        )
        if m_ep:
            data, ctype = ARTWORK_CACHE.get((m_ep.group(2), "banner")) or (
                _gradient_png(320, 180, (32, 28, 52), (9, 11, 18)),
                "image/png",
            )
            return self.reply(200, data, content_type=ctype)
        self.json_reply(404, {"error": "not_found"})


class ShowcaseServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, context: ssl.SSLContext) -> None:
        self.context = context
        super().__init__(("127.0.0.1", PORT), ShowcaseHandler)

    def server_bind(self) -> None:
        TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = self.server_address[1]

    def process_request_thread(self, request, client_address) -> None:
        request.settimeout(15)
        try:
            secure = self.context.wrap_socket(request, server_side=True)
        except OSError:
            self.shutdown_request(request)
            return
        super().process_request_thread(secure, client_address)


def self_test() -> int:
    count = populate_artwork(offline=True)
    home = build_home_payload()
    anime = build_catalog_payload("anime")
    reqs = build_requests_payload()
    assert len(secrets.token_urlsafe(32)) == 43
    assert count >= 30, f"expected >= 30 cached artwork slots, got {count}"
    assert len(home["rows"]) == 3
    assert len(anime["items"]) == 6
    assert [c["key"] for c in reqs["categories"]] == ["active", "attention", "history"]
    for row in home["rows"]:
        for card in row["cards"]:
            assert re.fullmatch(
                r"artwork/[1-9][0-9]{0,17}/(banner|poster)", card["artwork"]["landscape"]
            )
    print(f"SHOWCASE_SELF_TEST_OK artwork={count} rows={len(home['rows'])}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cert")
    parser.add_argument("--key")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if not args.cert or not args.key:
        raise SystemExit("Provide --cert and --key or --self-test")
    count = populate_artwork(offline=False)
    print(f"Showcase artwork populated: {count} entries", flush=True)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=args.cert, keyfile=args.key)
    server = ShowcaseServer(ctx)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
