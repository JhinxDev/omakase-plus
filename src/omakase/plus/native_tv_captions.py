"""Publish embedded text renditions against the native HLS video timeline."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from omakase.plus.language_policy import LANGUAGES, normalize_language
from omakase.plus.web_playback_delivery import DeliveryError
from omakase.plus.web_playback_source import PlexStream

MAX_TRACKS = 16
_MANIFEST_LIMIT = 256 * 1024
_TRACK_LIMIT = 1024 * 1024
_FRAGMENT_LIMIT = 256 * 1024
_TIMESTAMP = r"(?:[0-9]{2}:)?[0-5][0-9]:[0-5][0-9]\.[0-9]{3}"
_CUE_TIMING = re.compile(rf"({_TIMESTAMP}) --> ({_TIMESTAMP})(?:[ \t].*)?")
_FRAGMENT_HEADER = "WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:0\n\n"
_ASSET = re.compile(r"captions-(0|[1-9][0-9]?)(?:\.m3u8|-([0-9]{5})\.vtt)")
_ROLE_SUFFIX = {
    "forced": " (Forced)",
    "signs": " (Signs)",
    "sdh": " (SDH)",
    "commentary": " (Commentary)",
}
_SDH_CHARACTERISTICS = (
    ',CHARACTERISTICS="public.accessibility.transcribes-spoken-dialog,'
    'public.accessibility.describes-music-and-sound"'
)


def _read(path: Path, limit: int) -> str:
    with path.open("rb") as stream:
        body = stream.read(limit + 1)
    if len(body) > limit:
        raise DeliveryError("Apple TV captions unavailable.", 404)
    try:
        return body.decode("utf-8").replace("\r\n", "\n")
    except UnicodeDecodeError:
        raise DeliveryError("Apple TV captions unavailable.", 404) from None


def rendition_tags(tracks: list[tuple[PlexStream, str]]) -> list[str]:
    labels = Counter()
    renditions = []
    for track, uri in tracks:
        language = normalize_language(track.language, observation=True) or "und"
        role = "forced" if track.forced else track.role
        label = LANGUAGES.get(language, "Unknown language") + _ROLE_SUFFIX.get(role, "")
        labels[label] += 1
        name = f"{label} {labels[label]}" if labels[label] > 1 else label
        forced = "YES" if track.forced else "NO"
        autoselect = "YES" if track.forced or role == "sdh" else "NO"
        characteristics = _SDH_CHARACTERISTICS if role == "sdh" else ""
        renditions.append(
            f'#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subtitles",NAME="{name}",'
            f'LANGUAGE="{language}",DEFAULT=NO,AUTOSELECT={autoselect},FORCED={forced}'
            f'{characteristics},URI="{uri}"'
        )
    return renditions


def master(path: Path, tracks: list[PlexStream]) -> bytes:
    playlist = _read(path, _MANIFEST_LIMIT)
    if not tracks:
        return playlist.encode()
    lines = playlist.split("\n")
    for index, (tag, uri) in enumerate(zip(lines, lines[1:], strict=False)):
        if uri == "media-video.m3u8" and tag.startswith("#EXT-X-STREAM-INF:"):
            renditions = rendition_tags(
                [(track, f"captions-{number}.m3u8") for number, track in enumerate(tracks)]
            )
            lines[index : index + 1] = [*renditions, tag + ',SUBTITLES="subtitles"']
            return "\n".join(lines).encode()
    raise DeliveryError("Apple TV captions are still preparing.", 404)


def _video_windows(path: Path):
    data = _read(path, _MANIFEST_LIMIT)
    target = re.search(r"(?m)^#EXT-X-TARGETDURATION:([0-9]{1,2})$", data)
    rows = re.findall(
        r"(?m)^#EXTINF:([0-9]{1,2}(?:\.[0-9]{1,6})?),\npart-video-([0-9]{5})\.m4s$", data
    )
    if not target or not 1 <= len(rows) <= 4096:
        raise DeliveryError("Apple TV captions are still preparing.", 404)
    windows = []
    elapsed = 0.0
    for index, (duration, number) in enumerate(rows):
        start = round(elapsed)
        elapsed += float(duration) * 1000
        end = round(elapsed)
        if int(number) != index or end <= start:
            raise DeliveryError("Apple TV captions unavailable.", 404)
        windows.append((duration, start, end))
    return target[1], windows, data.endswith("#EXT-X-ENDLIST\n")


def _milliseconds(timestamp: str) -> int:
    clock, ms = timestamp.split(".")
    value = 0
    for part in clock.split(":"):
        value = value * 60 + int(part)
    return value * 1000 + int(ms)


def _fragment(path: Path, start: int, end: int) -> bytes:
    try:
        data = _read(path, _TRACK_LIMIT)
    except FileNotFoundError:
        return _FRAGMENT_HEADER.encode()
    if not data:
        return _FRAGMENT_HEADER.encode()
    if not data.startswith("WEBVTT\n"):
        raise DeliveryError("Apple TV captions are still preparing.", 404)
    if not data.endswith("\n"):
        data = data.rsplit("\n\n", 1)[0]
    cues = []
    for block in data.split("\n\n")[1:]:
        lines = block.splitlines()
        timing_line = next((index for index, line in enumerate(lines[:2]) if " --> " in line), -1)
        if timing_line < 0 or len(lines) <= timing_line + 1:
            continue
        timing = _CUE_TIMING.fullmatch(lines[timing_line])
        if not timing:
            continue
        left, right = map(_milliseconds, timing.groups())
        if left < right and left < end and start < right:
            # HLS expects cues that overlap a boundary in both adjacent fragments.
            cues.append("\n".join(lines[timing_line:]) + "\n\n")
    result = (_FRAGMENT_HEADER + "".join(cues)).encode()
    if len(result) > _FRAGMENT_LIMIT:
        raise DeliveryError("Apple TV captions unavailable.", 404)
    return result


def rendition(folder: Path, name: str, track_count: int) -> tuple[bytes, str]:
    asset = _ASSET.fullmatch(name)
    if not asset or int(asset[1]) >= track_count:
        raise DeliveryError("Apple TV captions unavailable.", 404)
    track = int(asset[1])
    target, windows, ended = _video_windows(folder / "media-video.m3u8")
    if asset[2] is not None:
        segment = int(asset[2])
        if segment >= len(windows):
            raise DeliveryError("Apple TV captions unavailable.", 404)
        _, start, end = windows[segment]
        return _fragment(folder / f"text-{track}.vtt", start, end), "text/vtt"
    rows = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{target}",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:EVENT",
    ]
    for segment, (duration, _, _) in enumerate(windows):
        rows.extend((f"#EXTINF:{duration},", f"captions-{track}-{segment:05d}.vtt"))
    if ended:
        rows.append("#EXT-X-ENDLIST")
    return ("\n".join(rows) + "\n").encode(), "application/vnd.apple.mpegurl"
