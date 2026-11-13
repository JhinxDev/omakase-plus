"""Pre-materialized HLS titles: one ffmpeg pass owns both segments and playlist.

The on-demand seek path predicts a segment timeline and then asks ffmpeg for
each chunk as the player needs it. The prediction and the produced chunks can
disagree, which repeats pictures and moves subtitles off the timeline. A
prepared title is materialized once, with a single ffmpeg HLS run that writes
the segments and the playlist from the same pass, so the durations the
playlist declares are the durations the files contain by construction.

This module is the read side of that store. Nothing here writes or transcodes.

Layout::

    <root>/<key>/stream.m3u8             playlist written by the prep pass
    <root>/<key>/s000.ts, s001.ts, ...   segments named by -hls_segment_filename
    <root>/<key>/subtitles-0.vtt, ...    optional WebVTT renditions

``<key>`` is the SHA-256 of the media URL path, never of the whole link:
Real-Debrid rotates the host and the token on every play, but the path
identifies the same file. See ``debrid_seek.keyframes_cache_path`` for the same
idea applied to the keyframe cache.

A directory is served only when its playlist is a complete VOD playlist and
every segment it names is already on disk. A run that is still writing, or one
that was aborted, therefore stays invisible and the caller keeps the existing
on-demand path unchanged.

The materialization pass, run once per title, is::

    ffmpeg -hide_banner -loglevel error -nostdin -y -i "<direct link>" \\
      -map 0:v:0 -map 0:a:0 -c:v copy -c:a aac -b:a 192k -ac 2 \\
      -f hls -hls_time 6 -hls_playlist_type vod \\
      -hls_segment_filename "<root>/<key>/s%03d.ts" "<root>/<key>/stream.m3u8"

Audio has to be converted: a TrueHD track cannot mux into MPEG-TS, and copying
the audio track produced an empty playlist on the real media. This module never
runs ffmpeg; it only reads what that single pass wrote.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

PLAYLIST_NAME = "stream.m3u8"
_PLAYLIST_LIMIT = 1024 * 1024
# One prepared title cannot legitimately exceed this many segments; the bound
# keeps a corrupt playlist from turning every request into a long read loop.
_MAX_SEGMENTS = 10000
_SEGMENT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\.ts")
_SIDECAR_NAME = re.compile(r"subtitles-([0-9]{1,2})\.vtt")
_EXTINF = re.compile(r"#EXTINF:([0-9]+(?:\.[0-9]+)?),?")
_KEY = re.compile(r"[a-f0-9]{64}")
# A WebVTT rendition larger than this is not a text track the player can use.
_SUBTITLE_LIMIT = 8 * 1024 * 1024


def prepared_key(url):
    """Stable directory name for one media URL, keyed from its path alone.

    A provider rotates the host and the token of the direct link on every play,
    so the path is the only part that identifies the same file.
    """
    if not isinstance(url, str) or not url:
        return ""
    try:
        path = urllib.parse.urlsplit(url).path
    except ValueError:
        return ""
    if not path:
        return ""
    return hashlib.sha256(path.encode("utf-8")).hexdigest()


def prepared_root():
    """Operator-configured store root, defaulting inside the app data directory."""
    configured = os.getenv("OMAKASE_PREPARED_ROOT", "").strip()
    if configured:
        return Path(configured)
    data = os.getenv("OMAKASE_DATA_DIR", "data").strip() or "data"
    return Path(data) / "prepared"


@dataclass(frozen=True)
class PreparedTitle:
    """One validated prepared title read back from disk."""

    key: str
    directory: Path
    playlist: bytes
    segments: tuple[str, ...]
    subtitles: tuple[Path, ...]
    duration: float


def _parse_playlist(data):
    """Return ``(segment names, total duration)`` for a complete VOD playlist."""
    try:
        lines = [line.strip() for line in data.decode("ascii").splitlines()]
    except UnicodeDecodeError:
        return None
    if not lines or lines[0] != "#EXTM3U":
        return None
    names = []
    duration = 0.0
    ended = False
    for line in lines[1:]:
        if not line:
            continue
        if line == "#EXT-X-ENDLIST":
            ended = True
            continue
        if line.startswith("#"):
            if match := _EXTINF.fullmatch(line):
                duration += float(match[1])
            continue
        if ended or len(names) >= _MAX_SEGMENTS or not _SEGMENT_NAME.fullmatch(line):
            return None
        names.append(line)
    # ffmpeg appends the endlist when the run finishes, so its absence means
    # the playlist still describes a run in progress.
    if not ended or not names or duration <= 0:
        return None
    return tuple(names), duration


def _sidecars(directory):
    found = []
    try:
        paths = list(directory.glob("subtitles-*.vtt"))
    except OSError:
        return ()
    for path in paths:
        if not (match := _SIDECAR_NAME.fullmatch(path.name)):
            continue
        try:
            info = path.stat()
        except OSError:
            continue
        if (
            path.is_symlink()
            or not stat.S_ISREG(info.st_mode)
            or not 0 < info.st_size <= _SUBTITLE_LIMIT
        ):
            continue
        found.append((int(match[1]), path))
    found.sort(key=lambda item: item[0])
    return tuple(path for _, path in found)


class PreparedStore:
    """Read side of the prepared-title store. No write path lives here."""

    def __init__(self, root=None):
        self.root = Path(root) if root is not None else None

    def base(self):
        return self.root if self.root is not None else prepared_root()

    def open(self, key):
        """Open one prepared title, or return ``None`` when it is absent or partial."""
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            return None
        directory = self.base() / key
        playlist_path = directory / PLAYLIST_NAME
        try:
            info = playlist_path.stat()
            if (
                not directory.is_dir()
                or playlist_path.is_symlink()
                or not stat.S_ISREG(info.st_mode)
                or not 0 < info.st_size <= _PLAYLIST_LIMIT
            ):
                return None
            data = playlist_path.read_bytes()
        except OSError:
            return None
        parsed = _parse_playlist(data)
        if parsed is None:
            return None
        names, duration = parsed
        for name in names:
            path = directory / name
            try:
                info = path.stat()
            except OSError:
                # A playlist that names a segment which is not on disk belongs
                # to a run that did not finish; serving it would fail mid-play.
                return None
            if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
                return None
        return PreparedTitle(
            key=key,
            directory=directory,
            playlist=data,
            segments=names,
            subtitles=_sidecars(directory),
            duration=duration,
        )


def _main(argv):
    if len(argv) != 2 or argv[0] not in {"key", "dir"}:
        print("usage: python -m omakase.plus.prepared_hls key|dir <media-url>", file=sys.stderr)
        return 2
    command, url = argv
    key = prepared_key(url)
    if not key:
        print("the media URL has no path to key from", file=sys.stderr)
        return 2
    print(prepared_root() / key if command == "dir" else key)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
