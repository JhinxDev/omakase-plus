"""Loopback companion fixture for the connected tvOS qualification.

`tv-source/OmakasePlusTVUITests/ConnectedQualification.swift` drives the real
Omakase Plus Apple TV app against this server. The app talks only to
`-omakase.tv.server-origin`, so this fixture stands in for the Omakase Plus
server and exposes the assertion surface the UI test reads over `/qa/*`.

Dependency-free: standard library only, Python 3.10+. It never imports the
repository's venv or any third-party package. TLS material is not vendored: the
launcher (`start_connected_fixture.sh`) generates a throwaway CA and server
certificate at run time and the server takes their paths.

Two layers, deliberately separated:

* `FixtureState` is pure (no sockets, no files, no clock of its own). Every
  recovery, stale-close and counter decision lives there, so `--self-test` can
  check it on any host, including Windows where tvOS cannot run at all.
* `Handler` is the HTTP shell: auth, routing, media bytes and `/qa/*` readback.

Routes are documented in `README.md` next to this file. Nothing here contacts a
provider, a real server or a device: the media is the synthetic HLS sample in
`hls/`, replayed by the fixture's own generated playlists.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import ssl
import subprocess
import sys
import time
import uuid
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPSHandler, ProxyHandler, build_opener

HERE = Path(__file__).resolve().parent
HLS_DIR = HERE / "hls"

API = "/plus/api/native-tv-test/v1/library/"
MEDIA = "/plus/api/native-tv-test/v1/media/"

PERSONAL = "a" * 32
HOUSEHOLD = "b" * 32
MOVIE_ID = "tt9000001"
MOVIE_TITLE = "QA Requested Movie"
HLS_ID = "tt0000002"
HLS_TITLE = "Harbor Signal"
HLS_WATCH_PATH = f"/plus/watch/title/movie/{HLS_ID}"
WATCHLIST_ID = "901"
WATCHLIST_TITLE = "QA Watchlist"
DETAILS_SUMMARY = "Synthetic saved series details."
RESUME_POSITION = 9.0
PLAYLIST_REPEATS = 8
SUBTITLE_RENDITIONS = {
    "captions-0.m3u8": ("English", "en"),
    "captions-1.m3u8": ("French", "fr"),
}
AUDIO_RENDITIONS = {
    "media-a0.m3u8": ("English", "en"),
    "media-a1.m3u8": ("Commentary", "en"),
}
PLAN_TTL_SECONDS = float(os.environ.get("OMAKASE_PLAN_TTL_SECONDS", "120"))
PORT = int(os.environ.get("OMAKASE_FIXTURE_PORT", "8854"))
CONTENT_TYPES = {
    ".m3u8": "application/vnd.apple.mpegurl",
    ".mp4": "video/mp4",
    ".m4s": "video/iso.segment",
    ".vtt": "text/vtt",
}


# --------------------------------------------------------------------- playlists
def _playlist_text(name: str) -> str:
    path = HLS_DIR / name
    if path.is_file():
        return path.read_text(encoding="utf-8")
    # The second audio rendition reuses the first one's segments. That is the
    # point: the fixture can tell which rendition the player asked for by the
    # playlist name, without shipping a second encode.
    if name == "media-a1.m3u8":
        return _playlist_text("media-a0.m3u8")
    raise FileNotFoundError(name)


def _segments(text: str) -> tuple[list[str], list[list[str]], float]:
    """Split a VOD playlist into header lines and `#EXTINF` + URI entries."""
    header: list[str] = []
    entries: list[list[str]] = []
    duration = 0.0
    for line in text.splitlines():
        if not line or line.startswith("#EXT-X-ENDLIST"):
            continue
        if line.startswith("#EXTINF"):
            duration += float(line.split(":", 1)[1].split(",", 1)[0])
            entries.append([line])
            continue
        if line.startswith("#"):
            if not line.startswith("#EXT-X-MEDIA-SEQUENCE"):
                header.append(line)
            continue
        entries[-1].append(line)
    return header, entries, duration


def _single_period(name: str) -> float:
    return _segments(_playlist_text(name))[2]


def extended_vod(name: str) -> str:
    """Repeat the sample `PLAYLIST_REPEATS` times behind discontinuities.

    The audited sample is about 21 seconds. A 45 second pause would outlive it,
    which is one of the defects the review found. HLS represents a repeated
    boundary with `#EXT-X-DISCONTINUITY`, so the playlist stays a legal VOD
    playlist while describing a sample of `PLAYLIST_REPEATS * 21` seconds.
    """
    header, entries, _ = _segments(_playlist_text(name))
    body: list[str] = ["#EXT-X-MEDIA-SEQUENCE:0"]
    for index in range(PLAYLIST_REPEATS):
        if index:
            body.append("#EXT-X-DISCONTINUITY")
        for entry in entries:
            body.extend(entry)
    body.append("#EXT-X-ENDLIST")
    return "\n".join([*header, *body, ""])


def master_playlist() -> str:
    lines = ["#EXTM3U", "#EXT-X-VERSION:7"]
    for index, (name, (label, language)) in enumerate(AUDIO_RENDITIONS.items()):
        lines.append(
            '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="group_audio",'
            f'NAME="{label}",DEFAULT={"YES" if index == 0 else "NO"},'
            f'LANGUAGE="{language}",CHANNELS="1",URI="{name}"'
        )
    for name, (label, language) in SUBTITLE_RENDITIONS.items():
        lines.append(
            '#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subtitles",'
            f'NAME="{label}",LANGUAGE="{language}",DEFAULT=NO,AUTOSELECT=NO,'
            f'FORCED=NO,URI="{name}"'
        )
    lines.append(
        "#EXT-X-STREAM-INF:BANDWIDTH=9354,AVERAGE-BANDWIDTH=8280,"
        'RESOLUTION=640x360,CODECS="avc1.64001e,mp4a.40.2",'
        'AUDIO="group_audio",SUBTITLES="subtitles"'
    )
    lines.append("media-video.m3u8")
    return "\n".join([*lines, ""])


GENERATED = {
    "index.m3u8": master_playlist,
    "media-video.m3u8": lambda: extended_vod("media-video.m3u8"),
    "media-a0.m3u8": lambda: extended_vod("media-a0.m3u8"),
    "media-a1.m3u8": lambda: extended_vod("media-a1.m3u8"),
    "captions-0.m3u8": lambda: extended_vod("captions-0.m3u8"),
    "captions-1.m3u8": lambda: extended_vod("captions-1.m3u8"),
}


def clip_seconds() -> float:
    """Playable length of the served sample, in seconds."""
    return round(_single_period("media-video.m3u8") * PLAYLIST_REPEATS, 3)


# ------------------------------------------------------------------ pure state
class Pass:
    """One media pass: a grant plus the plan that actually serves the bytes."""

    def __init__(self, media_id: str, token: str, watch_path: str, now: float) -> None:
        self.media_id = media_id
        self.token = token
        self.watch_path = watch_path
        self.opened_at = now
        self.plan_live = True
        self.plan_expires_at = now + PLAN_TTL_SECONDS
        self.registered = True
        self.withdrawn = False
        self.renditions: list[str] = []
        self.resume: list[dict] = []


class FixtureState:
    """Every assertion surface, with no I/O so `--self-test` can drive it."""

    def __init__(self, plan_ttl: float = PLAN_TTL_SECONDS, clip: float | None = None) -> None:
        self.plan_ttl = plan_ttl
        self.clip = clip if clip is not None else clip_seconds()
        self.passes: dict[str, Pass] = {}
        self.gate_owner: str | None = None
        self.fresh_passes_opened = 0
        self.stale_close_received = False
        self.stale_close_media: str | None = None
        self.checkpoint: float | None = None
        self.checkpoint_seq = 0
        self.first_checkpoint: float | None = None
        self.withdrawn_media: set[str] = set()
        self.renditions: dict[str, int] = {}
        self.qa_calls: list[dict] = []
        self.requests: dict[str, dict] = {}
        self.details_reads = 0
        self.shelf_probe: dict = {}
        self.profiles: dict[str, int] = {}

    # -- lifecycle
    def open_pass(self, watch_path: str, now: float) -> Pass:
        media_id = secrets.token_hex(16)
        token = secrets.token_urlsafe(32)
        entry = Pass(media_id, token, watch_path, now)
        self.passes[media_id] = entry
        self.fresh_passes_opened += 1
        self.gate_owner = media_id
        # The saved resume position belongs to the title, not to the pass: a
        # reopen must report the last checkpoint the viewer actually reached.
        self.checkpoint_seq = 0
        self.qa_calls.append({"action": "play", "watch_path": watch_path, "media_id": media_id})
        return entry

    def plan_state(self, media_id: str, now: float) -> str:
        entry = self.passes.get(media_id)
        if entry is None or not entry.registered or entry.withdrawn:
            return "missing"
        if not entry.plan_live or now >= entry.plan_expires_at:
            return "expired"
        return "live"

    def access_reply(self, media_id: str, now: float) -> dict | None:
        """`None` means 404 (unknown, closed or revoked). A reclaimed plan
        answers 200 with an explicit recovery hint so the client can keep
        revocation, a changed copy and plan expiry apart."""
        state = self.plan_state(media_id, now)
        if state == "missing":
            return None
        if state == "expired":
            return {"allowed": True, "stream": "expired", "recover": "reopen"}
        return {"allowed": True}

    def media_allowed(self, media_id: str, now: float) -> bool:
        return self.plan_state(media_id, now) == "live"

    def save_checkpoint(self, media_id: str, seq: int, position: float) -> dict:
        entry = self.passes.get(media_id)
        if entry is None or not entry.registered:
            return {}
        if self.first_checkpoint is None:
            self.first_checkpoint = position
        self.checkpoint = position
        self.checkpoint_seq = seq
        entry.resume.append({"seq": seq, "position": position})
        return {"saved_at": 1}

    def record_rendition(self, media_id: str, name: str) -> None:
        entry = self.passes.get(media_id)
        if entry is not None:
            entry.renditions.append(name)
        self.renditions[name] = self.renditions.get(name, 0) + 1

    # -- controls
    def expire_plan(self, now: float) -> dict:
        """Keep the grant authorized, reclaim the plan. This is the sequence the
        audited fixture could not express."""
        target = self.gate_owner or self._newest()
        entry = self.passes.get(target or "")
        if entry is None:
            return {"expired": None, "reason": "no_pass"}
        entry.plan_live = False
        entry.plan_expires_at = now
        return {
            "expired": entry.media_id,
            "stream": "expired",
            "recover": "reopen",
            "registered": entry.registered,
        }

    def stale_close(self, media_id: str | None = None) -> dict:
        """Close an older pass while a newer one stays live.

        This is the ownership edge a stale writer produces in production: a close
        names an older media id and the newer pass must be untouched. The older
        copy is re-registered first, because a client that correctly released the
        pass it replaced leaves nothing older to close. `reused_older` reports
        that honestly instead of implying a second session existed.
        """
        ordered = self._ordered()
        if len(ordered) < 2:
            return {"stale_close": None, "reason": "need_two_passes"}
        newer = ordered[-1]
        older = self.passes.get(media_id) if media_id is not None else ordered[0]
        if older is None or older is newer:
            return {"stale_close": None, "reason": "not_an_older_pass"}
        reused = not older.registered
        older.registered = True  # the server still holds the older copy
        older.registered = False  # the stale close arrives for it
        self.stale_close_received = True
        self.stale_close_media = older.media_id
        self.qa_calls.append({"action": "release", "media_id": older.media_id, "stale": True})
        if self.gate_owner == older.media_id:
            self.gate_owner = newer.media_id
        return {
            "stale_close": older.media_id,
            "older_registered": older.registered,
            "successor": newer.media_id,
            "successor_live": self.plan_state(newer.media_id, time.monotonic()) != "missing",
            "gate_owner": self.gate_owner,
            "reused_older": reused,
        }

    def close(self, media_id: str) -> dict:
        entry = self.passes.get(media_id)
        if entry is None:
            return {"closed": False, "reason": "unknown_media"}
        entry.registered = False
        self.qa_calls.append({"action": "release", "media_id": media_id})
        newer = self._newest()
        stale = newer is not None and newer != media_id
        if stale:
            self.stale_close_received = True
            self.stale_close_media = media_id
        if self.gate_owner == media_id:
            self.gate_owner = newer if stale else None
        return {"closed": True, "stale": stale, "gate_owner": self.gate_owner}

    def withdraw(self, media_id: str) -> dict:
        entry = self.passes.get(media_id)
        if entry is None:
            return {"withdrawn": None}
        entry.withdrawn = True
        self.withdrawn_media.add(media_id)
        return {"withdrawn": media_id}

    def withdraw_older(self) -> dict:
        older, newer = self._older_and_newer()
        if older is None or newer is None:
            return {"withdrawn": None, "reason": "need_two_passes"}
        return {
            "withdrawn": self.withdraw(older.media_id)["withdrawn"],
            "successor": newer.media_id,
        }

    # -- lookups
    def _ordered(self) -> list[Pass]:
        # Insertion order is open order, which is what "older pass" means here.
        return list(self.passes.values())

    def _newest(self) -> str | None:
        ordered = self._ordered()
        return ordered[-1].media_id if ordered else None

    def _older_and_newer(self) -> tuple[Pass | None, Pass | None]:
        ordered = [item for item in self._ordered() if item.registered]
        if len(ordered) < 2:
            return None, None
        return ordered[0], ordered[-1]

    def position_for_reply(self) -> float:
        """The resume position a fresh pass reports: the last checkpoint."""
        value = self.checkpoint if self.checkpoint is not None else RESUME_POSITION
        return round(min(max(value, 0.0), self.clip - 0.5), 3)

    def snapshot(self, now: float) -> dict:
        checkpoints: list[dict] = []
        for entry in self._ordered():
            checkpoints.append(
                {
                    "media_id": entry.media_id,
                    "watch_path": entry.watch_path,
                    "plan": "expired"
                    if self.plan_state(entry.media_id, now) == "expired"
                    else ("live" if entry.registered else "closed"),
                    "registered": entry.registered,
                    "plan_registered": entry.registered,
                    "renditions": sorted(set(entry.renditions)),
                }
            )
        # The older pass is the first one opened in this run, so a stale close
        # can be observed after it is deregistered (the registered-only
        # `_older_and_newer` would report nothing once it is gone).
        ordered = self._ordered()
        older = ordered[0] if ordered else None
        newer = self._newest()
        return {
            "qa_calls": self.qa_calls,
            "requests": list(self.requests.values()),
            "fresh_passes_opened": self.fresh_passes_opened,
            "stale_close_received": self.stale_close_received,
            "stale_close_media": self.stale_close_media,
            "older_pass_registered": None if older is None else older.registered,
            "successor_media": newer,
            "gate_owner": self.gate_owner,
            "checkpoint": self.checkpoint,
            "checkpoint_seq": self.checkpoint_seq,
            "first_checkpoint": self.first_checkpoint,
            "clip_seconds": self.clip,
            "resume_position": self.position_for_reply(),
            "renditions": self.renditions,
            "passes": checkpoints,
            "withdrawn_media": sorted(self.withdrawn_media),
            "details_reads": self.details_reads,
            "shelf_probe": self.shelf_probe,
            "profiles": sorted(self.profiles.values()),
        }


# ----------------------------------------------------------------- HTTP shell
STATE = FixtureState()


class Handler(BaseHTTPRequestHandler):
    server_version = "OmakaseTVFixture/2"

    # -- replies
    def reply(self, status, body=b"", *, content_type="application/json", headers=()):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        for key, value in headers:
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json_reply(self, status, data):
        self.reply(status, json.dumps(data).encode())

    def read_json(self):
        size = self.headers.get("Content-Length", "")
        if not size.isascii() or not size.isdigit() or not 0 < int(size) <= 8192:
            return {}
        try:
            data = json.loads(self.rfile.read(int(size)))
        except (ValueError, UnicodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def bearer(self):
        value = self.headers.get("Authorization", "")
        return value.removeprefix("Bearer ") if value.startswith("Bearer ") else None

    def cookie_ok(self, entry: Pass):
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", "")).get("omakase_tv_test_media")
        except CookieError:
            return False
        return cookie is not None and secrets.compare_digest(cookie.value, entry.token)

    # -- routing
    def do_POST(self):
        route = urlsplit(self.path)
        if self.qa_post(route):
            return
        if route.path == API + "device":
            token = secrets.token_urlsafe(32)
            profile = 1 if not STATE.profiles else 2
            if profile in STATE.profiles.values():
                return self.json_reply(429, {})
            STATE.profiles[token] = profile
            return self.json_reply(
                200, {"device_token": token, "user_code": "XMKT-YJPR", "expires_in": 300}
            )
        token = self.bearer()
        if token not in STATE.profiles:
            return self.json_reply(401, {})
        if route.path == API + "unlink":
            del STATE.profiles[token]
            return self.json_reply(200, {"unlinked": True})
        if route.path == API + "play":
            path = self.read_json().get("watch_path")
            # Only the HLS sample is playable here; the mp4 movie title exists in
            # the catalog so the request/check flow has something to name.
            if path == HLS_WATCH_PATH:
                return self.start_media(path)
            return self.json_reply(404, {})
        if route.path in (API + "for-you/request", API + "for-you/request/check"):
            return self.request_title(route)
        resume = re.fullmatch(re.escape(API) + r"media/([0-9a-f]{32})/resume", route.path)
        if resume:
            return self.resume(resume.group(1))
        close = re.fullmatch(re.escape(API) + r"media/([0-9a-f]{32})/close", route.path)
        if close:
            return self.json_reply(200, STATE.close(close.group(1)))
        if route.path == API + "diagnostic":
            body = self.read_json()
            media_id = body.get("media_id")
            state = body.get("time_control")
            if (
                isinstance(media_id, str)
                and media_id in STATE.passes
                and body.get("engine") == "avplayer"
                and body.get("trigger") in ("state", "heartbeat")
                and state in ("playing", "paused", "buffering")
            ):
                STATE.qa_calls.append(
                    {"action": "diagnostic", "media_id": media_id, "time_control": state}
                )
            return self.json_reply(200, {"stored": True})
        return self.json_reply(404, {})

    def do_GET(self):
        route = urlsplit(self.path)
        if self.qa_get(route):
            return
        if route.path.startswith(MEDIA):
            return self.serve_media(route)
        token = self.bearer()
        if route.path == API + "device":
            if token not in STATE.profiles:
                return self.json_reply(401, {})
            return self.json_reply(200, {"expires_in": 2_592_000})
        if token not in STATE.profiles:
            return self.json_reply(401, {})
        profile = STATE.profiles[token]
        access = re.fullmatch(re.escape(API) + r"media/([0-9a-f]{32})/access", route.path)
        if access:
            media_id = access.group(1)
            reply = STATE.access_reply(media_id, time.monotonic())
            if reply is None:
                return self.json_reply(404, {})
            if reply.get("stream") == "expired":
                STATE.qa_calls.append({"action": "expired-access", "media_id": media_id})
            return self.json_reply(200, reply)
        if route.path == API + "profile":
            name = "Alex" if profile == 1 else "Morgan"
            return self.json_reply(200, {"id": str(profile), "name": name, "has_avatar": False})
        if route.path == API + "libraries":
            return self.json_reply(
                200, {"items": [{"id": PERSONAL, "name": "QA Personal", "kind": "personal"}]}
            )
        if route.path == API + "home":
            return self.home_page()
        if route.path == API + "requests":
            return self.requests_page()
        if route.path == API + "catalog":
            return self.catalog_page(parse_qs(route.query))
        if route.path in (API + "for-you", API + "catalog-search"):
            return self.recommendations(route)
        if route.path == API + "episodes":
            return self.json_reply(200, {"items": [], "title_summary": DETAILS_SUMMARY})
        if route.path == API + "watchlist":
            return self.json_reply(
                200,
                {
                    "items": [
                        {
                            "id": WATCHLIST_ID,
                            "title": WATCHLIST_TITLE,
                            "note": "Synthetic saved series",
                            "summary": DETAILS_SUMMARY,
                        }
                    ]
                },
            )
        if route.path == API + "details":
            STATE.details_reads += 1
            return self.json_reply(
                200,
                {
                    "title": WATCHLIST_TITLE,
                    "episode_title": "",
                    "note": "Synthetic saved series",
                    "summary": DETAILS_SUMMARY,
                },
            )
        return self.json_reply(404, {})

    def do_HEAD(self):
        route = urlsplit(self.path)
        if route.path.startswith(MEDIA):
            return self.serve_media(route)
        self.send_error(501)

    # -- /qa readback
    def qa_get(self, route):
        if route.path == "/qa/state":
            self.json_reply(200, STATE.snapshot(time.monotonic()))
            return True
        if route.path == "/qa/health":
            self.json_reply(
                200,
                {
                    "fixture": "omakase-tvos-connected",
                    "clip_seconds": STATE.clip,
                    "plan_ttl_seconds": STATE.plan_ttl,
                },
            )
            return True
        if route.path == "/qa/shelf":
            self.json_reply(200, self.shelf(None))
            return True
        if route.path == "/qa/open-shelf":
            action = parse_qs(route.query).get("action", [""])[0]
            self.json_reply(200, self.shelf(action))
            return True
        return False

    # -- /qa controls (all POST: a control that changes state is not a GET)
    def qa_post(self, route):
        if route.path == "/qa/result":
            STATE.qa_calls.append({"action": "result"})
            self.json_reply(200, {"stored": True})
            return True
        if route.path == "/qa/expire-plan":
            self.json_reply(200, STATE.expire_plan(time.monotonic()))
            return True
        if route.path == "/qa/stale-close":
            media_id = self.read_json().get("media_id")
            if media_id is not None and not isinstance(media_id, str):
                self.json_reply(400, {"reason": "invalid_media"})
            else:
                self.json_reply(200, STATE.stale_close(media_id))
            return True
        if route.path == "/qa/withdraw-older-pass":
            self.json_reply(200, STATE.withdraw_older())
            return True
        if route.path == "/qa/close-current":
            media = STATE.gate_owner or STATE._newest()
            self.json_reply(200, STATE.close(media) if media else {"closed": False})
            return True
        if route.path == "/qa/reset":
            # Re-initialise in place: rebinding a module global from a handler
            # thread would need `global` and buys nothing here.
            STATE.__init__()
            self.json_reply(200, {"reset": True})
            return True
        return False

    # -- app API
    def start_media(self, path):
        entry = STATE.open_pass(path, time.monotonic())
        languages = [label for label, _ in AUDIO_RENDITIONS.values()]
        captions = [label for label, _ in SUBTITLE_RENDITIONS.values()]
        return self.json_reply(
            200,
            {
                "title": HLS_TITLE,
                "media_id": entry.media_id,
                "media_token": entry.token,
                "media_format": "hls",
                "media_playlist": "vod",
                "duration_seconds": int(STATE.clip),
                "position_seconds": STATE.position_for_reply(),
                "expires_in": 3600,
                "access_check": True,
                "resume_sync": True,
                "resume_updated_at": 1,
                "resume_scope": "c" * 64,
                "identity": {
                    "library_id": PERSONAL,
                    "domain": "movie",
                    "canonical_id": HLS_ID,
                    "season_number": 0,
                    "episode_number": 0,
                    "media_version": "d" * 64,
                },
                "audio_languages": languages,
                "subtitle_languages": captions,
            },
        )

    def resume(self, media_id):
        body = self.read_json()
        seq = body.get("seq")
        position = body.get("position_seconds")
        if type(seq) is not int or type(position) not in (int, float):
            return self.json_reply(400, {})
        if not 0.0 <= float(position) <= STATE.clip:
            return self.json_reply(422, {})
        reply = STATE.save_checkpoint(media_id, seq, float(position))
        if not reply:
            return self.json_reply(404, {})
        return self.json_reply(200, reply)

    def request_title(self, route):
        body = self.read_json()
        kind, title_id = body.get("kind"), body.get("title_id")
        if (kind, title_id) != ("movie", MOVIE_ID):
            return self.json_reply(400, {})
        check = route.path.endswith("/check")
        STATE.qa_calls.append(
            {"action": "check" if check else "request", "kind": kind, "title_id": title_id}
        )
        status = {
            "kind": kind,
            "title_id": title_id,
            "title": MOVIE_TITLE,
            "state": "ready" if check else "requested",
            "label": "Ready in QA library" if check else "Requested in QA",
            "detail": "Synthetic result. No provider acquisition.",
            "can_check": not check,
        }
        STATE.requests[f"{kind}:{title_id}"] = status
        return self.json_reply(200, status)

    def home_page(self):
        artwork = {"landscape": None, "poster": None}
        card = {
            "id": "qa-movie",
            "domain": "movie",
            "title": HLS_TITLE,
            "title_id": HLS_ID,
            "subtitle": "Synthetic HLS movie",
            "detail_label": "",
            "progress": {
                "percent": int(100 * RESUME_POSITION / STATE.clip),
                "remaining_label": "",
            },
            "play_url": HLS_WATCH_PATH + "?play=1",
            "play_label": "Resume",
            "artwork": artwork,
        }
        return self.json_reply(
            200,
            {
                "banner": {"title": HLS_TITLE, "media_detail": "Synthetic Home", "artwork": artwork},
                "rows": [
                    {
                        "id": "continue-movies",
                        "label": "Continue Watching \u2014 Movies",
                        "cards": [card],
                    }
                ],
                "layout": {"rows": []},
            },
        )

    def requests_page(self):
        request = {
            "kind": "movie",
            "title_id": "tt9000011",
            "title": "QA Pending Movie",
            "state": "pending",
            "label": "Waiting for a copy",
            "detail": "The provider acknowledged the request.",
            "can_check": False,
            "stages": [
                {
                    "stage": "saved",
                    "label": "Request saved",
                    "state": "complete",
                    "occurred_at": "2026-11-15T05:00:00Z",
                }
            ],
        }
        return self.json_reply(
            200,
            {
                "categories": [
                    {"key": "active", "title": "Active", "requests": [request]},
                    {"key": "attention", "title": "Needs attention", "requests": []},
                    {"key": "history", "title": "History", "requests": []},
                ]
            },
        )

    def catalog_page(self, query):
        kind = query.get("kind", ["movie"])[0]
        scope = query.get("library_id", [PERSONAL])[0]
        search = query.get("q", [""])[0]
        if scope not in (PERSONAL, HOUSEHOLD):
            return self.json_reply(404, {})
        entry = {
            "id": HLS_ID,
            "title": HLS_TITLE,
            "note": "Synthetic HLS movie",
            "watch_path": HLS_WATCH_PATH,
            "progress": RESUME_POSITION / STATE.clip,
            "duration_seconds": int(STATE.clip),
        }
        if kind == "anime":
            entry = {"id": WATCHLIST_ID, "title": WATCHLIST_TITLE, "note": "Synthetic saved series"}
        if search:
            STATE.qa_calls.append({"action": "catalog-search", "query": search})
        items = [] if search and search.casefold() not in entry["title"].casefold() else [entry]
        return self.json_reply(
            200,
            {
                "items": items,
                "featured": entry if not search else None,
                "continue_watching": [entry] if kind == "movie" and not search else [],
            },
        )

    def recommendations(self, route):
        query = parse_qs(route.query)
        search = route.path.endswith("catalog-search")
        category = "all" if search else query.get("category", ["anime"])[0]
        kind = (
            "movie" if category in ("movies", "all") else ("anime" if category == "anime" else "tv")
        )
        title = {
            "movie": MOVIE_TITLE,
            "anime": "QA Anime Pick",
            "tv": "QA TV Pick",
        }[kind]
        card_id = MOVIE_ID if kind == "movie" else "902"
        STATE.qa_calls.append(
            {
                "action": "catalog-search" if search else "for-you",
                "category": category,
                "query": query.get("q", [""])[0],
            }
        )
        card = {
            "id": card_id,
            "kind": kind,
            "title": title,
            "summary": "A synthetic catalog title for remote navigation.",
            "can_request": kind == "movie",
            "request": STATE.requests.get(f"{kind}:{card_id}"),
        }
        return self.json_reply(
            200,
            {
                "category": category,
                "rows": [{"key": "qa", "title": "QA Picks", "cards": [card]}],
                "requests": list(STATE.requests.values()),
                "warnings": [],
            },
        )

    # -- media bytes
    def serve_media(self, route):
        parts = route.path.removeprefix(MEDIA).split("/", 1)
        if len(parts) != 2 or not re.fullmatch(r"[0-9a-f]{32}", parts[0]):
            return self.json_reply(404, {})
        media_id, name = parts
        entry = STATE.passes.get(media_id)
        if entry is None or not self.cookie_ok(entry):
            return self.json_reply(401, {})
        if not STATE.media_allowed(media_id, time.monotonic()):
            return self.json_reply(404, {})
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", name):
            return self.json_reply(404, {})
        suffix = Path(name).suffix
        mime = CONTENT_TYPES.get(suffix)
        if mime is None:
            return self.json_reply(404, {})
        if self.command == "GET":
            STATE.record_rendition(media_id, name)
        if name in GENERATED:
            return self.reply(200, GENERATED[name]().encode(), content_type=mime)
        asset = HLS_DIR / name
        if not asset.is_file() or asset.name != name:
            return self.json_reply(404, {})
        return self.reply(200, asset.read_bytes(), content_type=mime)

    # -- Top Shelf
    def shelf(self, action):
        """Read the app group snapshot this disposable fixture app wrote.

        Never seed one: a fake snapshot would report the shelf as working when
        nothing wrote it.
        """
        device = os.environ.get("OMAKASE_SIMULATOR_UDID", "")
        if not device:
            STATE.shelf_probe.update(available=False, reason="no_simulator_udid")
            return {"available": False, "reason": "no_simulator_udid"}
        bundle = "dev.omakase.plus.fixture"
        group = "group.dev.omakase.plus.fixture.top-shelf"
        try:
            result = subprocess.run(
                ["xcrun", "simctl", "get_app_container", device, bundle, group],
                capture_output=True,
                text=True,
                timeout=8,
                check=True,
            )
            snapshot = Path(result.stdout.strip()) / "Library/Caches/TopShelf-v1/shelf.json"
            if not snapshot.is_file() or snapshot.stat().st_size > 32768:
                raise ValueError("missing_snapshot")
            data = json.loads(snapshot.read_text(encoding="utf-8"))
            cards = data["cards"]
            if not isinstance(cards, list) or not 0 < len(cards) <= 16:
                raise ValueError("invalid_snapshot")
            actions = sorted({card["action"] for card in cards})
            STATE.shelf_probe.update(available=True, actions=actions)
            if action is None:
                return {"available": True, "actions": actions}
            if action not in ("resume", "details"):
                return {"available": True, "actions": actions, "opened": None}
            card = next(item for item in cards if item["action"] == action)
            url = "omakaseplus-tv://top-shelf/" + action + "/" + str(uuid.UUID(str(card["id"])))
            subprocess.run(
                ["xcrun", "simctl", "openurl", device, url],
                capture_output=True,
                timeout=8,
                check=True,
            )
            STATE.qa_calls.append({"action": "top-shelf-openurl", "destination": action})
            return {"available": True, "actions": actions, "opened": action}
        except (OSError, ValueError, KeyError, StopIteration, subprocess.SubprocessError) as error:
            reason = type(error).__name__
            STATE.shelf_probe.update(available=False, reason=reason)
            return {"available": False, "reason": reason}


class FixtureServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, context):
        self.context = context
        super().__init__(("127.0.0.1", PORT), Handler)

    def server_bind(self):
        # Always loopback: startup must not depend on DNS or a network namespace.
        TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = self.server_address[1]

    def process_request_thread(self, request, client_address):
        request.settimeout(15)
        try:
            secure = self.context.wrap_socket(request, server_side=True)
        except OSError as error:
            print(f"fixture TLS handshake failed: {error}", file=sys.stderr, flush=True)
            self.shutdown_request(request)
            return
        super().process_request_thread(secure, client_address)


# ------------------------------------------------------------------- self-test
def self_test() -> int:
    """Drive the pure state. Runs anywhere, including this Windows host."""
    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        status = "PASS" if condition else "FAIL"
        print(f"{status} {name}{'' if condition else '  ' + detail}")
        if not condition:
            failures.append(name)

    clip = clip_seconds()
    check("clip covers a 45 second hold", clip >= 90, f"clip={clip}")
    check("master advertises two audio renditions", master_playlist().count("TYPE=AUDIO") == 2)
    check(
        "master advertises two subtitle renditions", master_playlist().count("TYPE=SUBTITLES") == 2
    )
    check(
        "extended video playlist repeats the sample",
        extended_vod("media-video.m3u8").count("#EXT-X-DISCONTINUITY") == PLAYLIST_REPEATS - 1,
    )
    check(
        "extended video playlist sums to the clip",
        abs(_single_period("media-video.m3u8") * PLAYLIST_REPEATS - clip) < 0.01,
    )

    state = FixtureState(plan_ttl=120.0, clip=clip)
    now = 1000.0
    first = state.open_pass(HLS_WATCH_PATH, now)
    check("fresh pass opens live", state.plan_state(first.media_id, now) == "live")
    check(
        "live plan answers allowed only",
        state.access_reply(first.media_id, now) == {"allowed": True},
    )
    check("fresh pass counter is 1", state.fresh_passes_opened == 1)
    check("gate belongs to the fresh pass", state.gate_owner == first.media_id)

    state.save_checkpoint(first.media_id, 3, 41.5)
    expired = state.expire_plan(now + 1)
    check("plan expiry names the same pass", expired.get("expired") == first.media_id)
    check(
        "expired plan stays authorized",
        state.access_reply(first.media_id, now + 2)
        == {"allowed": True, "stream": "expired", "recover": "reopen"},
    )
    check("expired plan is registered", state.passes[first.media_id].registered)
    check("expired plan serves no bytes", not state.media_allowed(first.media_id, now + 2))
    check("expired plan keeps the checkpoint", state.checkpoint == 41.5)

    second = state.open_pass(HLS_WATCH_PATH, now + 3)
    check("reopen increments the fresh counter", state.fresh_passes_opened == 2)
    check(
        "reopen reports the checkpointed position",
        state.position_for_reply() == 41.5,
        str(state.position_for_reply()),
    )
    check(
        "reopened plan is live", state.access_reply(second.media_id, now + 4) == {"allowed": True}
    )

    stale = state.stale_close()
    check("stale close names the older pass", stale.get("stale_close") == first.media_id)
    check("stale close was received", state.stale_close_received)
    check("older pass is no longer registered", state.passes[first.media_id].registered is False)
    check(
        "older pass reads as registered false",
        state.snapshot(now + 5)["older_pass_registered"] is False,
    )
    check("successor stays live", state.plan_state(second.media_id, now + 5) == "live")
    check("successor keeps the gate", state.gate_owner == second.media_id)
    check("stale close does not open a pass", state.fresh_passes_opened == 2)

    withdrawn = state.withdraw_older()
    check(
        "withdrawal is a distinct outcome",
        withdrawn.get("withdrawn") is None and withdrawn.get("reason") == "need_two_passes",
    )
    check(
        "withdrawn or closed media answers 404", state.access_reply(first.media_id, now + 6) is None
    )

    closed = state.close(second.media_id)
    check(
        "closing the live pass clears the gate",
        closed == {"closed": True, "stale": False, "gate_owner": None},
    )
    check("closed pass answers 404", state.access_reply(second.media_id, now + 7) is None)

    late = state.close("f" * 32)
    check(
        "a close for an unknown pass is rejected",
        late == {"closed": False, "reason": "unknown_media"},
    )

    fresh = FixtureState(plan_ttl=120.0, clip=clip)
    a = fresh.open_pass(HLS_WATCH_PATH, now)
    b = fresh.open_pass(HLS_WATCH_PATH, now + 1)
    check(
        "two passes can be live at once",
        fresh.plan_state(a.media_id, now + 2) == "live"
        and fresh.plan_state(b.media_id, now + 2) == "live",
    )
    fresh.record_rendition(b.media_id, "captions-1.m3u8")
    fresh.record_rendition(b.media_id, "media-a1.m3u8")
    snapshot = fresh.snapshot(now + 3)
    successor = next(item for item in snapshot["passes"] if item["media_id"] == b.media_id)
    check(
        "renditions are recorded per pass",
        successor["renditions"] == ["captions-1.m3u8", "media-a1.m3u8"],
        str(successor["renditions"]),
    )
    check("rendition counters are global", snapshot["renditions"].get("media-a1.m3u8") == 1)

    fresh.close(a.media_id)
    replayed = fresh.stale_close()
    check(
        "stale close re-registers a released older copy",
        replayed.get("reused_older") is True,
        str(replayed),
    )
    check(
        "replayed stale close still targets the older pass",
        replayed.get("stale_close") == a.media_id,
    )
    check(
        "replaying the older copy leaves the successor live",
        fresh.plan_state(b.media_id, now + 4) == "live",
    )
    check("replaying the older copy does not move the gate", fresh.gate_owner == b.media_id)
    check("only two passes were ever opened", fresh.fresh_passes_opened == 2)

    print(f"{len(failures)} failure(s)")
    return 1 if failures else 0


# ----------------------------------------------------------------------- main
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="connected tvOS qualification fixture")
    parser.add_argument(
        "--self-test", action="store_true", help="check the pure state machine and exit"
    )
    parser.add_argument("--state", metavar="CAFILE", help="print /qa/state over HTTPS and exit")
    parser.add_argument("--cert", type=Path, help="server certificate (PEM)")
    parser.add_argument("--key", type=Path, help="server private key (PEM)")
    arguments = parser.parse_args(argv)

    if arguments.self_test:
        return self_test()
    if arguments.state:
        return print_state(arguments.state)
    if arguments.cert is None or arguments.key is None:
        parser.error(
            "--cert and --key are required to serve; TLS material is generated at run time"
        )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(arguments.cert, arguments.key)
    print(
        f"fixture clip={clip_seconds()}s plan_ttl={PLAN_TTL_SECONDS}s port={PORT}",
        file=sys.stderr,
        flush=True,
    )
    with FixtureServer(context) as server:
        print(f"fixture listening on https://127.0.0.1:{PORT}", file=sys.stderr, flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


def print_state(cafile) -> int:
    context = ssl.create_default_context(cafile=str(cafile))
    opener = build_opener(ProxyHandler({}), HTTPSHandler(context=context))
    try:
        with opener.open(f"https://127.0.0.1:{PORT}/qa/state", timeout=4) as response:
            print(response.read().decode())
    except OSError as error:
        print(f"fixture not ready: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
