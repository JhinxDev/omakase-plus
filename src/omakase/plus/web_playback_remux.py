"""Bounded private COPY-only derivatives. No Plex transcoding or encoder fallback.

Routes must independently authorize every resource and hold its generation while
reading. A returned Path is not authorization. Only Linux admits real workers.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import re
import secrets
import signal
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx

from omakase.plus.outbound import pin_destination
from omakase.plus.web_playback_codecs import CodecMetadataError, normalize_partial_manifest
from omakase.plus.web_playback_source import ResolvedAnimeSource

MIB = 1024**2
JOB_LIMIT = 768 * MIB  # Monitored threshold, not a directory quota.
FILE_LIMIT = 4096
PACKET_FILE = "packet-observer.json"
PACKET_BYTES = 16 * 1024
OBSERVER_BYTES = 16 * MIB
PACKET_LINES = 1_000_000
PACKET_LINE_BYTES = 128
# Metadata millisecond rounding, codec priming and ordinary frame reordering may
# differ slightly at endpoints. Never permit a GOP-sized missing tail: quarter
# second is a fixed admission tolerance, independent for every track and MPD.
TIMELINE_TOLERANCE = 0.250
_NAME = re.compile(
    r"(?:manifest\.mpd|init-[0-9]+\.mp4|chunk-[0-9]+-[0-9]{5,}\.m4s|subtitle-[0-9]{1,20}\.ass|font-[0-9]{1,3}\.bin)"
)
_JOB = re.compile(r"[0-9a-f]{32}")
_NS = {"d": "urn:mpeg:dash:schema:mpd:2011"}


class RemuxError(ValueError):
    """Constant public diagnostic, with no upstream response or stderr."""

    def __init__(self, code="remux_failed"):
        self.code = code
        super().__init__(code)


async def _authorize(callback):
    result = callback()
    if inspect.isawaitable(result):
        result = await result
    if result is False:
        raise RemuxError("authorization_changed")


def _inventory(source):
    if not isinstance(source, ResolvedAnimeSource):
        raise RemuxError("unresolved_source")
    if (
        source.container != "mkv"
        or not 0 < source.part_size <= 256 * MIB
        or not 0 < source.duration_ms <= 2400000
    ):
        raise RemuxError("unsupported_source")
    video = [s for s in source.streams if s.kind == "video"]
    audio = [s for s in source.streams if s.kind == "audio"]
    if not 1 <= len(audio) <= 8:
        raise RemuxError("unsupported_multi_audio")
    if (
        len(video) != 1
        or video[0].codec not in {"h264", "hevc", "av1"}
        or any(s.codec not in {"aac", "opus", "ac3", "eac3"} for s in audio)
    ):
        raise RemuxError("unsupported_codec")
    if not re.fullmatch(r"/library/parts/[0-9]+/[0-9]+/[^/?#\\]+", source.part_key):
        raise RemuxError("unsupported_source")
    if not re.fullmatch("[0-9a-f]{64}", source.media_version):
        raise RemuxError("unsupported_source")
    subtitles = [s for s in source.streams if s.kind == "subtitle"]
    if len(subtitles) > 16 or any(s.codec not in {"ass", "ssa"} for s in subtitles):
        raise RemuxError("unsupported_subtitle_codec")
    if len({s.stream_id for s in source.streams}) != len(source.streams):
        raise RemuxError("ambiguous_track_inventory")
    return video[0], audio


def _language(value):
    value = (value or "und").lower()
    return {
        "ja": "jpn",
        "en": "eng",
        "fr": "fra",
        "fre": "fra",
        "de": "deu",
        "ger": "deu",
        "es": "spa",
        "it": "ita",
        "ko": "kor",
        "zh": "zho",
        "chi": "zho",
        "pt": "por",
        "ru": "rus",
        "ar": "ara",
    }.get(value, value)


def qualify_tracks(source, document):
    """Bind Plex IDs to verified container indexes, never to opaque-ID guesses."""
    _inventory(source)
    start_value = (
        document.get("format", {}).get("start_time") if isinstance(document, dict) else None
    )
    if (
        not isinstance(start_value, str)
        or not re.fullmatch(r"-?[0-9]{1,4}(?:\.[0-9]{1,9})?", start_value)
        or abs(float(start_value)) > TIMELINE_TOLERANCE
    ):
        raise RemuxError("unsupported_timestamp_basis")
    streams = document.get("streams") if isinstance(document, dict) else None
    if not isinstance(streams, list) or not 2 <= len(streams) <= 41:
        raise RemuxError("invalid_probe")
    indexes = [s.get("index") for s in streams]
    if any(type(i) is not int or not 0 <= i <= 127 for i in indexes) or len(set(indexes)) != len(
        indexes
    ):
        raise RemuxError("invalid_probe")
    matched = {}
    for kind in ("video", "audio", "subtitle"):
        plex = [s for s in source.streams if s.kind == kind]
        probed = [s for s in streams if s.get("codec_type") == kind]
        if len(plex) != len(probed):
            raise RemuxError("track_inventory")
        for item in plex:
            index = getattr(item, "container_index", None)
            candidates = (
                [s for s in probed if s["index"] == index]
                if index is not None
                else (probed if len(plex) == 1 else [])
            )
            if len(candidates) != 1:
                raise RemuxError("ambiguous_track_inventory")
            track = candidates[0]
            codec = {"ssa": "ass"}.get(item.codec, item.codec)
            actual_language = _language(track.get("tags", {}).get("language"))
            # Plex may infer a video language from the release/title although the
            # exact container video is untagged. Audio/subtitle language identifies
            # a user's actual track choice and must still match exactly.
            language = actual_language if kind == "video" else _language(item.language)
            if track.get("codec_name") != codec or (
                kind != "video" and actual_language != language
            ):
                raise RemuxError("track_inventory")
            if kind == "video" and not all(
                type(track.get(v)) is int and 0 < track[v] <= 16384 for v in ("width", "height")
            ):
                raise RemuxError("invalid_probe")
            if kind == "audio" and not (
                type(track.get("channels")) is int and 0 < track["channels"] <= 32
            ):
                raise RemuxError("invalid_probe")
            if kind == "audio" and not re.fullmatch(
                r"[1-9][0-9]{0,8}/[1-9][0-9]{0,8}", track.get("time_base", "")
            ):
                raise RemuxError("unsupported_timestamp_basis")
            if not re.fullmatch("[0-9]{1,20}", item.stream_id) or not re.fullmatch(
                "[a-z]{2,3}", language
            ):
                raise RemuxError("invalid_probe")
            if track["index"] in matched:
                raise RemuxError("ambiguous_track_inventory")
            matched[track["index"]] = (
                item,
                {
                    "index": track["index"],
                    "id": item.stream_id,
                    "language": language,
                    "default": item.default,
                    "forced": item.forced,
                },
            )
            if kind == "audio":
                matched[track["index"]][1]["time_base"] = track["time_base"]
    fonts = []
    for track in streams:
        if track.get("codec_type") == "attachment":
            if track.get("codec_name") not in {"ttf", "otf"} or track.get("tags", {}).get(
                "mimetype", ""
            ).lower() not in {
                "application/x-truetype-font",
                "application/vnd.ms-opentype",
                "application/x-font-ttf",
                "application/x-font-opentype",
                "font/ttf",
                "font/otf",
            }:
                raise RemuxError("unsupported_attachment")
            fonts.append(track["index"])
        elif track["index"] not in matched:
            raise RemuxError("unsupported_track")
    if len(fonts) > 16:
        raise RemuxError("unsupported_attachment")

    def tracks(kind):
        return [entry[1] for _, entry in sorted(matched.items()) if entry[0].kind == kind]

    return {
        "mode": "remux",
        "video": tracks("video")[0],
        "audio": tracks("audio"),
        "subtitles": tracks("subtitle"),
        "fonts": sorted(fonts),
        "input_start_time": float(start_value),
    }


def _cpu_usage():
    """Whole-container cumulative CPU survives child reaping; conservative scope."""
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        quota, period = int(quota), int(period)
        if not 0 < quota <= period <= 1_000_000:
            raise ValueError
        fields = dict(
            line.split() for line in Path("/sys/fs/cgroup/cpu.stat").read_text().splitlines()
        )
        usage = int(fields["usage_usec"])
        if usage < 0:
            raise ValueError
        return usage
    except (OSError, ValueError, KeyError):
        raise RemuxError("cpu_budget_unqualified") from None


async def _observe_packets(reader, source, recipe):
    """Incremental keyed FFprobe packets; raw output is bounded and never stored."""
    codecs = {s.stream_id: s.codec for s in source.streams}
    expected = {
        track["index"]: (
            i + 1,
            track,
            codecs[track["id"]],
            *map(int, track["time_base"].split("/")),
        )
        for i, track in enumerate(recipe["audio"])
    }
    stats = {index: {"packets": 0} for index in expected}
    buffer = b""
    received = lines = 0

    def line(raw):
        nonlocal lines
        lines += 1
        if lines > PACKET_LINES or len(raw) + 1 > PACKET_LINE_BYTES:
            raise RemuxError("packet_evidence_bound")
        try:
            # Empty explicitly-disabled packet_side_data sections may emit one
            # terminal separator; no side-data or encoded payload is requested.
            parts = raw.decode("ascii").rstrip("\r").removesuffix("|").split("|")
            fields = dict(part.split("=", 1) for part in parts)
        except (UnicodeDecodeError, ValueError):
            raise RemuxError("invalid_packet_evidence") from None
        if (
            set(fields) != {"stream_index", "pts", "duration"}
            or len(parts) != 3
            or any(not re.fullmatch(r"-?[0-9]{1,19}", value) for value in fields.values())
        ):
            raise RemuxError("invalid_packet_evidence")
        index, pts, duration = (int(fields[key]) for key in ("stream_index", "pts", "duration"))
        if index not in expected:
            raise RemuxError("packet_mapping_mismatch")
        _, track, codec, num, den = expected[index]
        start = pts * num / den - recipe["input_start_time"]
        end = (pts + duration) * num / den - recipe["input_start_time"]
        if (
            not 0 < duration < 2**63
            or not -(2**63) < pts < 2**63
            or start < -TIMELINE_TOLERANCE
            or end > source.duration_ms / 1000 + TIMELINE_TOLERANCE
            or (
                track["index"] in stats
                and stats[index]["packets"]
                and pts < stats[index]["last_pts"]
            )
            or (codec == "opus" and duration * num * 1000 > 120 * den)
        ):
            raise RemuxError("invalid_packet_evidence")
        item = stats[index]
        item["packets"] += 1
        item["last_pts"] = pts
        item["start_seconds"] = min(item.get("start_seconds", start), start)
        item["end_seconds"] = max(item.get("end_seconds", end), end)

    while chunk := await reader.read(65536):
        received += len(chunk)
        if received > OBSERVER_BYTES:
            raise RemuxError("packet_evidence_bound")
        buffer += chunk
        complete = buffer.split(b"\n")
        buffer = complete.pop()
        for raw in complete:
            line(raw)
        if len(buffer) > PACKET_LINE_BYTES:
            raise RemuxError("packet_evidence_bound")
    if buffer or any(not item["packets"] for item in stats.values()):
        raise RemuxError("packet_evidence_missing")
    streams = []
    for index, item in stats.items():
        output, track, codec, num, den = expected[index]
        item.pop("last_pts")
        item.update(
            representation_id=str(output),
            container_index=index,
            plex_stream_id=track["id"],
            timebase=[num, den],
            codec=codec,
        )
        streams.append(item)
    return {
        "kind": "same-source-audio-packet-observer-v1",
        "media_version": source.media_version,
        "part_size": source.part_size,
        "input_start_time": recipe["input_start_time"],
        "output_bytes": received,
        "lines": lines,
        "streams": streams,
    }


def _read_packet_evidence(folder, source, ordered, *, required, recipe=None):
    path = folder / PACKET_FILE
    if not path.exists():
        if required:
            raise RemuxError("packet_evidence_missing")
        return None
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= PACKET_BYTES:
        raise RemuxError("packet_evidence_bound")
    raw = path.read_bytes()
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        raise RemuxError("invalid_packet_evidence") from None
    if (
        not isinstance(value, dict)
        or value.get("kind") != "same-source-audio-packet-observer-v1"
        or value.get("media_version") != source.media_version
        or value.get("part_size") != source.part_size
        or not isinstance(value.get("streams"), list)
        or len(value["streams"]) != len(ordered) - 1
    ):
        raise RemuxError("packet_mapping_mismatch")
    for index, stream in enumerate(ordered[1:], 1):
        item = value["streams"][index - 1]
        if (
            not isinstance(item, dict)
            or item.get("representation_id") != str(index)
            or item.get("plex_stream_id") != stream.stream_id
            or item.get("codec") != stream.codec
            or type(item.get("packets")) is not int
            or item["packets"] <= 0
            or not all(
                type(item.get(k)) in (int, float)
                and -TIMELINE_TOLERANCE <= item[k] <= source.duration_ms / 1000 + TIMELINE_TOLERANCE
                for k in ("start_seconds", "end_seconds")
            )
            or item["end_seconds"] <= item["start_seconds"]
        ):
            raise RemuxError("packet_mapping_mismatch")
        if recipe is not None:
            track = recipe["audio"][index - 1]
            if (
                item.get("container_index") != track["index"]
                or item.get("timebase") != list(map(int, track["time_base"].split("/")))
                or value.get("input_start_time") != recipe["input_start_time"]
            ):
                raise RemuxError("packet_mapping_mismatch")
    value.update(sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw))
    return value


def _directory(root, job):
    root = Path(root)
    if not _JOB.fullmatch(job) or root.is_symlink() or not root.is_dir():
        raise RemuxError("unsafe_cache")
    folder = root / job
    if folder.is_symlink() or folder.resolve().parent != root.resolve():
        raise RemuxError("unsafe_cache")
    return folder


def admit_cache(root, source_size):
    """Require an exact dedicated <=2GiB tmpfs and >=3GiB cgroup budget.

    This is deliberately fail-closed for ordinary directories and unknown memory
    topology. A trusted deployment may inject a separately qualified quota check.
    """
    if sys.platform != "linux":
        raise RemuxError("linux_worker_required")
    root = Path(root).resolve(strict=True)
    mounts = Path("/proc/self/mountinfo").read_text().splitlines()
    exact = [
        line.split(" - ") for line in mounts if line.split()[4].replace("\\040", " ") == str(root)
    ]
    if len(exact) != 1 or exact[0][1].split()[0] != "tmpfs":
        raise RemuxError("hard_cache_quota_required")
    stat = os.statvfs(root)
    if (
        stat.f_blocks * stat.f_frsize > 2 * 1024**3
        or stat.f_bavail * stat.f_frsize < max(JOB_LIMIT, source_size + 128 * MIB)
        or stat.f_favail < FILE_LIMIT + 1
    ):
        raise RemuxError("cache_capacity")
    try:
        maximum = int(Path("/sys/fs/cgroup/memory.max").read_text().strip())
        current = int(Path("/sys/fs/cgroup/memory.current").read_text().strip())
        if maximum < 3 * 1024**3 or maximum - current < JOB_LIMIT + 1024 * MIB:
            raise ValueError
    except (OSError, ValueError):
        raise RemuxError("memory_budget_unqualified") from None


def _admit_with_eviction(db, root, size, held_versions, admission):
    """Bounded idle LRU turnover: TTL is maximum idle retention, not minimum."""
    for attempt in range(3):
        if db.execute("SELECT 1 FROM web_playback_remux WHERE state='RUNNING'").fetchone():
            raise RemuxError("worker_busy")
        count = db.execute(
            "SELECT COUNT(*) FROM web_playback_remux WHERE state='READY'"
        ).fetchone()[0]
        try:
            admission(root, size)
            if count < 2:
                return
        except RemuxError as error:
            if error.code != "cache_capacity":
                raise
        if attempt == 2 or not _evict_idle_ready(db, root, held_versions):
            raise RemuxError("cache_busy")


def _evict_idle_ready(db, root, held_versions):
    # A short writer transaction prevents a concurrent session/hold insertion
    # between the fresh hold check and revoking the READY artifact. No await here.
    previous = db.execute("PRAGMA busy_timeout").fetchone()[0]
    selected = None
    try:
        db.execute("PRAGMA busy_timeout=0")
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM web_playback_remux WHERE state='RUNNING'").fetchone():
            db.rollback()
            return False
        rows = db.execute(
            "SELECT job_id,media_version,artifact_json FROM web_playback_remux WHERE state='READY' ORDER BY expires,created,job_id LIMIT 20"
        ).fetchall()
        held = set(held_versions())
        for job, version, raw in rows:
            if version in held:
                continue
            folder = _directory(root, job)
            marker = folder / ".omakase-remux"
            if (
                not folder.is_dir()
                or marker.is_symlink()
                or not marker.is_file()
                or marker.stat().st_size != 32
                or marker.read_text() != job
            ):
                continue
            try:
                artifact = json.loads(raw)
            except (ValueError, TypeError):
                continue
            names = artifact.get("resources") if isinstance(artifact, dict) else None
            if (
                not isinstance(artifact, dict)
                or artifact.get("job_id") != job
                or artifact.get("media_version") != version
                or not isinstance(names, list)
                or not names
                or len(names) > FILE_LIMIT
                or any(not isinstance(n, str) or not _NAME.fullmatch(n) for n in names)
            ):
                continue
            entries = list(folder.iterdir())
            allowed = set(names) | {".omakase-remux"}
            if (folder / PACKET_FILE).exists():
                allowed.add(PACKET_FILE)
            if (
                len(entries) > FILE_LIMIT + 1
                or {p.name for p in entries} != allowed
                or any(p.is_symlink() or not p.is_file() for p in entries)
            ):
                continue
            db.execute(
                "UPDATE web_playback_remux SET state='ERROR',phase='complete' WHERE job_id=? AND state='READY'",
                (job,),
            )
            db.commit()
            selected = (job, folder, entries)
            break
        else:
            db.rollback()
    except sqlite3.OperationalError:
        db.rollback()
        return False
    finally:
        if db.in_transaction:
            db.rollback()
        db.execute(f"PRAGMA busy_timeout={int(previous)}")
    if selected is None:
        return False
    job, folder, entries = selected
    try:
        for path in entries:
            if os.name == "nt":
                path.chmod(0o600)
            path.unlink()
        folder.rmdir()
    except OSError:
        # Revoked partial remains ERROR, never exposed or mistaken for READY.
        return False
    db.execute("DELETE FROM web_playback_remux WHERE job_id=? AND state='ERROR'", (job,))
    db.commit()
    return True


def _identity(pid):
    """None proves absence; other failures quarantine rather than release a lease."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return None
    except OSError:
        raise RemuxError("process_identity_unknown") from None
    try:
        return (
            stat[stat.rfind(")") + 2 :].split()[19],
            Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        )
    except (OSError, IndexError):
        raise RemuxError("process_identity_unknown") from None


def held_media_versions(db):
    """Conservative source-deletion holds; separate from authenticated play holds."""
    return {
        row[0]
        for row in db.execute(
            "SELECT DISTINCT media_version FROM web_playback_remux WHERE state='RUNNING'"
        )
    }


def _lease_update(db, job, owner, statement, parameters=()):
    """Internal fixed SQL fragments; no state transition after lost ownership."""
    result = db.execute(
        f"UPDATE web_playback_remux SET {statement} WHERE job_id=? AND state='RUNNING' AND coordinator_token=?",
        (*parameters, job, owner),
    )
    if result.rowcount != 1:
        db.rollback()
        raise RemuxError("lease_lost")
    db.commit()


def _read_cwd(pid_path):
    return os.readlink(pid_path / "cwd")


def _cwd_scan_absent(folder, *, proc_root=Path("/proc"), pid_limit=1024):
    """Finite cwd-only absence proof. Any incomplete inventory stays quarantined.

    Deployed workers run in the app's qualified private container PID namespace.
    Never inspect cmdlines, arguments, environments or credentials, and never kill
    a process discovered by this scan. The launcher/bootstrap/watchdog all inherit
    the exact app-owned job cwd before exec, so any match preserves the lease.
    """

    def inventory():
        pids = set()
        count = 0
        with os.scandir(proc_root) as entries:
            for entry in entries:
                count += 1
                if count > pid_limit + 256:
                    raise OSError
                if not entry.name.isascii() or not entry.name.isdigit():
                    continue
                if not entry.is_dir(follow_symlinks=False):
                    raise OSError
                pids.add(entry.name)
                if len(pids) > pid_limit:
                    raise OSError
        if not pids:
            raise OSError
        return pids

    try:
        target = os.path.normcase(os.path.normpath(str(folder.resolve(strict=True))))
        before = inventory()
        for pid in before:
            path = proc_root / pid
            try:
                cwd = _read_cwd(path)
            except FileNotFoundError:
                # An exited process is safe; an existing but unreadable cwd is not.
                try:
                    path.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                return False
            if not isinstance(cwd, str) or not (cwd.startswith("/") or Path(cwd).is_absolute()):
                return False
            normalized = os.path.normcase(os.path.normpath(cwd))
            if normalized == target or normalized == target + " (deleted)":
                return False
        # New processes during the scan make this attempt incomplete. Disappearing
        # processes cannot become a working owner, so a subset remains sufficient.
        return inventory().issubset(before)
    except (OSError, ValueError, RuntimeError):
        return False


def cleanup(db, cache_root, *, held_versions=lambda: (), now=None):
    """Finite known-row cleanup; unknown directories and uncertain workers survive."""
    now = time.time() if now is None else now
    held = set(held_versions())
    rows = db.execute("SELECT * FROM web_playback_remux ORDER BY created LIMIT 20").fetchall()
    names = [
        column[0] for column in db.execute("SELECT * FROM web_playback_remux LIMIT 0").description
    ]
    for raw in rows:
        row = dict(zip(names, raw))
        if row["state"] == "RUNNING":
            if sys.platform != "linux":
                continue
            deadline = row.get("deadline")
            if (
                not isinstance(deadline, (int, float))
                or not row["created"] <= deadline <= row["created"] + 45
                or now <= deadline + 5
            ):
                continue
            if (
                type(row.get("coordinator_pid")) is not int
                or row["coordinator_pid"] <= 0
                or not row.get("coordinator_start_ticks")
                or not row.get("coordinator_boot_id")
                or not row.get("coordinator_token")
            ):
                continue
            try:
                coordinator = _identity(row["coordinator_pid"])
                if coordinator == (row["coordinator_start_ticks"], row["coordinator_boot_id"]):
                    continue  # Child exit/handoff is never coordinator completion.
                identity = _identity(row["pid"]) if row["pid"] is not None else None
            except RemuxError:
                continue  # Unknown coordinator/worker identity remains quarantined.
            if row["pid"] is not None:
                expected = (row["start_ticks"], row["boot_id"])
                if identity == expected:
                    continue  # Deadline alone never frees a live worker's reservation.
                if identity is not None:
                    continue  # PID reuse is not kill or cleanup authority.
            folder = _directory(cache_root, row["job_id"])
            marker = folder / ".omakase-remux"
            if (
                not folder.is_dir()
                or marker.is_symlink()
                or not marker.is_file()
                or marker.stat().st_size != 32
                or marker.read_text() != row["job_id"]
                or not _cwd_scan_absent(folder)
            ):
                continue
            changed = db.execute(
                "UPDATE web_playback_remux SET state='ERROR',phase='complete' WHERE job_id=? AND state='RUNNING' AND coordinator_token=?",
                (row["job_id"], row["coordinator_token"]),
            )
            if changed.rowcount != 1:
                continue
        elif row["state"] == "READY" and row["expires"] > now:
            continue
        if row["media_version"] in held:
            continue
        folder = _directory(cache_root, row["job_id"])
        marker = folder / ".omakase-remux"
        if folder.exists():
            if marker.is_symlink() or not marker.is_file() or marker.read_text() != row["job_id"]:
                continue
            # Refuse symlinks or nested entries rather than traversing them.
            entries = list(folder.iterdir())
            if len(entries) > FILE_LIMIT + 1 or any(
                p.is_symlink() or not p.is_file() for p in entries
            ):
                continue
            for path in entries:
                path.unlink()
            folder.rmdir()
        db.execute("DELETE FROM web_playback_remux WHERE job_id=?", (row["job_id"],))
    db.commit()


def _usage(folder):
    total = count = 0
    for path in folder.iterdir():
        count += 1
        if path.is_symlink() or not path.is_file() or count > FILE_LIMIT + 1:
            raise RemuxError("output_bound")
        stat = path.stat()
        if path.name == PACKET_FILE and stat.st_size > PACKET_BYTES:
            raise RemuxError("packet_evidence_bound")
        total += max(stat.st_size, getattr(stat, "st_blocks", 0) * 512)
        if total > JOB_LIMIT:
            raise RemuxError("output_bound")
    return total


def _duration(value):
    match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", value or "")
    if not match or not any(match.groups()):
        raise RemuxError("invalid_manifest")
    h, m, s = (float(v or 0) for v in match.groups())
    return h * 3600 + m * 60 + s


def validate_artifact(folder, source, recipe=None, *, require_packet_evidence=False):
    """Validate static MPD, complete timelines and a local exact resource inventory."""
    video, audio = _inventory(source)
    by_id = {s.stream_id: s for s in source.streams}
    ordered = [video] + audio
    mapped_tracks = None
    if recipe is not None:
        mapped_tracks = [recipe["video"]] + recipe["audio"]
        ordered = [by_id[t["id"]] for t in mapped_tracks]
    _usage(folder)
    packet_evidence = _read_packet_evidence(
        folder, source, ordered, required=require_packet_evidence, recipe=recipe
    )
    path = folder / "manifest.mpd"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MIB:
        raise RemuxError("invalid_manifest")
    raw = path.read_bytes()
    if any(s in raw.lower() for s in (b"<!doctype", b"<!entity", b"baseurl")):
        raise RemuxError("invalid_manifest")
    try:
        mpd = ET.fromstring(raw)
    except ET.ParseError:
        raise RemuxError("invalid_manifest") from None
    if mpd.tag != "{" + _NS["d"] + "}MPD" or mpd.get("type") != "static":
        raise RemuxError("invalid_manifest")
    for element in mpd.iter():
        if element.tag.split("}")[-1] in {
            "UTCTiming",
            "Location",
            "PatchLocation",
            "SegmentURL",
            "SegmentList",
            "SegmentBase",
        }:
            raise RemuxError("invalid_manifest")
        if any(
            "href" in k.lower() or ("://" in v and not k.endswith("}schemaLocation")) or "\\" in v
            for k, v in element.attrib.items()
        ):
            raise RemuxError("invalid_manifest")
    reps = mpd.findall(".//d:Representation", _NS)
    periods = mpd.findall("d:Period", _NS)
    if (
        len(reps) != len(ordered)
        or len(periods) != 1
        or _duration(periods[0].get("start", "PT0S")) != 0
        or len(mpd.findall(".//d:AdaptationSet", _NS)) != len(ordered)
    ):
        raise RemuxError("track_inventory")
    expected_duration = source.duration_ms / 1000
    duration = _duration(mpd.get("mediaPresentationDuration"))
    if abs(duration - expected_duration) > TIMELINE_TOLERANCE:
        raise RemuxError("incomplete_timeline")
    if (
        periods[0].get("duration") is not None
        and abs(_duration(periods[0].get("duration")) - expected_duration) > TIMELINE_TOLERANCE
    ):
        raise RemuxError("incomplete_timeline")
    resources = {"manifest.mpd"}
    mapping = []
    endpoints = []
    ids = set()
    adaptations = mpd.findall(".//d:AdaptationSet", _NS)
    for output_index, (rep, stream) in enumerate(zip(reps, ordered)):
        packets = (
            next(
                (
                    item
                    for item in packet_evidence["streams"]
                    if item["representation_id"] == str(output_index)
                ),
                None,
            )
            if packet_evidence
            else None
        )
        expected_end = (
            packets["end_seconds"]
            if packets and stream.kind == "audio" and stream.codec == "opus"
            else expected_duration
        )
        language = (
            mapped_tracks[output_index]["language"]
            if mapped_tracks is not None
            else stream.language
        )
        codec = rep.get("codecs", "")
        prefixes = {
            "hevc": ("hev1", "hvc1"),
            "h264": ("avc1", "avc3"),
            "av1": ("av01",),
            "aac": ("mp4a",),
            "opus": ("opus", "Opus"),
            "ac3": ("ac-3",),
            "eac3": ("ec-3",),
        }
        if not codec.startswith(prefixes[stream.codec]):
            raise RemuxError("track_inventory")
        rid = rep.get("id", "")
        if not re.fullmatch("[0-9]+", rid) or rid in ids or rid != str(output_index):
            raise RemuxError("invalid_manifest")
        ids.add(rid)
        adaptation = adaptations[output_index]
        if adaptation.findall("d:Representation", _NS) != [rep] or (
            recipe is not None and _language(adaptation.get("lang")) != _language(language)
        ):
            raise RemuxError("track_inventory")
        template = rep.find("d:SegmentTemplate", _NS)
        if template is None:
            raise RemuxError("invalid_manifest")
        if (
            template.get("initialization") != "init-$RepresentationID$.mp4"
            or template.get("media") != "chunk-$RepresentationID$-$Number%05d$.m4s"
        ):
            raise RemuxError("invalid_manifest")
        if set(template.attrib) - {
            "initialization",
            "media",
            "timescale",
            "startNumber",
            "presentationTimeOffset",
        }:
            raise RemuxError("invalid_manifest")
        resources.add(f"init-{rid}.mp4")
        timeline = template.find("d:SegmentTimeline", _NS)
        if timeline is None:
            raise RemuxError("invalid_manifest")
        try:
            scale = int(template.get("timescale", "1"))
            number = int(template.get("startNumber", "1"))
            end = 0
            first_start = None
            if scale <= 0 or number != 1 or int(template.get("presentationTimeOffset", "0")) != 0:
                raise ValueError
            for segment in timeline:
                if segment.tag != "{" + _NS["d"] + "}S" or set(segment.attrib) - {"t", "d", "r"}:
                    raise ValueError
                d = int(segment.attrib["d"])
                repeat = int(segment.get("r", "0"))
                start = int(segment.get("t", str(end)))
                if (
                    d <= 0
                    or not 0 <= repeat < FILE_LIMIT
                    or start < 0
                    or (end and start != end)
                    or (number == 1 and start / scale > TIMELINE_TOLERANCE)
                ):
                    raise ValueError
                if first_start is None:
                    first_start = start / scale
                for _ in range(repeat + 1):
                    resources.add(f"chunk-{rid}-{number:05d}.m4s")
                    number += 1
                    if len(resources) > FILE_LIMIT:
                        raise ValueError
                end = start + d * (repeat + 1)
            if number == 1 or abs(end / scale - expected_end) > TIMELINE_TOLERANCE:
                raise ValueError
            if packets and (
                abs(end / scale - packets["end_seconds"]) > TIMELINE_TOLERANCE
                or abs(first_start - packets["start_seconds"]) > TIMELINE_TOLERANCE
            ):
                raise ValueError
            if (
                expected_end == expected_duration
                and abs(end / scale - duration) > TIMELINE_TOLERANCE
            ):
                raise ValueError
            endpoints.append((first_start, end / scale - expected_end))
        except (ValueError, KeyError, OverflowError):
            raise RemuxError("invalid_manifest") from None
        mapping.append(
            {
                "representation_id": rid,
                "plex_stream_id": stream.stream_id,
                "kind": stream.kind,
                "codec": stream.codec,
                "language": language,
                "container_index": (
                    next(
                        t["index"]
                        for t in [recipe["video"]] + recipe["audio"]
                        if t["id"] == stream.stream_id
                    )
                    if recipe
                    else getattr(stream, "container_index", None)
                ),
                "default": stream.default,
                "forced": stream.forced,
                "selected": stream.selected,
            }
        )
        if packets:
            mapping[-1]["packet_evidence"] = packets
    if (
        max(v[0] for v in endpoints) - min(v[0] for v in endpoints) > TIMELINE_TOLERANCE
        or max(v[1] for v in endpoints) - min(v[1] for v in endpoints) > TIMELINE_TOLERANCE
    ):
        raise RemuxError("incomplete_timeline")
    subtitles = []
    fonts = []
    for kind in ("subtitles", "fonts"):
        total = 0
        for item in (recipe or {}).get(kind, []):
            name = f"subtitle-{item['id']}.ass" if kind == "subtitles" else f"font-{item}.bin"
            path = folder / name
            if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 8 * MIB:
                raise RemuxError("invalid_auxiliary_resource")
            total += path.stat().st_size
            if total > 32 * MIB:
                raise RemuxError("auxiliary_resource_bound")
            data = path.read_bytes()
            if kind == "fonts":
                if data[:4] not in {b"\x00\x01\x00\x00", b"OTTO"}:
                    raise RemuxError("invalid_font")
                fonts.append(
                    {
                        "filename": name,
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "bytes": len(data),
                    }
                )
            else:
                if b"[Script Info]" not in data[:65536] or b"[Events]" not in data:
                    raise RemuxError("invalid_ass")
                stream = by_id[item["id"]]
                subtitles.append(
                    {
                        "plex_stream_id": stream.stream_id,
                        "filename": name,
                        "codec": stream.codec,
                        "language": stream.language,
                        "default": stream.default,
                        "forced": stream.forced,
                        "selected": stream.selected,
                        "container_index": item["index"],
                    }
                )
            resources.add(name)
    actual = {p.name for p in folder.iterdir() if p.name not in {".omakase-remux", PACKET_FILE}}
    if actual != resources or any(not _NAME.fullmatch(n) for n in actual):
        raise RemuxError("invalid_resources")
    for name in resources:
        path = folder / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
            raise RemuxError("invalid_resources")
        path.chmod(0o400)
    if packet_evidence:
        (folder / PACKET_FILE).chmod(0o400)
    return {
        "manifest_path": "manifest.mpd",
        "resources": sorted(resources),
        "track_map": mapping,
        "duration": duration,
        "subtitles": subtitles,
        "fonts": fonts,
        "packet_evidence": packet_evidence,
    }


def get_resource(db, job_id, filename, *, cache_root, held_versions=lambda: ()):
    """Read-only READY lookup; a freshly authorized active hold survives cache TTL.

    Caller validates the absolute session lifetime and current source on every
    request. A hold permits continued reading; it never renews that authorization.
    """
    if not isinstance(filename, str) or not _NAME.fullmatch(filename):
        raise RemuxError("unknown_resource")
    row = db.execute(
        "SELECT artifact_json,expires FROM web_playback_remux WHERE job_id=? AND state='READY'",
        (job_id,),
    ).fetchone()
    if row is None:
        raise RemuxError("expired_resource")
    artifact = json.loads(row[0])
    if row[1] <= time.time() and artifact["media_version"] not in set(held_versions()):
        raise RemuxError("expired_resource")
    if filename not in artifact["resources"]:
        raise RemuxError("unknown_resource")
    path = _directory(cache_root, job_id) / filename
    if (
        path.is_symlink()
        or not path.is_file()
        or path.resolve().parent != _directory(cache_root, job_id).resolve()
    ):
        raise RemuxError("unknown_resource")
    return path


async def _launch(folder, recipe):
    if sys.platform != "linux":
        raise RemuxError("linux_worker_required")
    launcher = Path(__file__).with_name("web_playback_exec.py")
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        str(launcher),
        str(os.getpid()),
        json.dumps(recipe, separators=(",", ":")),
        cwd=folder,
        env={"PATH": "/usr/bin:/bin", "LANG": "C"},
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE
        if recipe["mode"] in {"probe", "observe"}
        else asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        close_fds=True,
    )


async def _stop(process):
    def send(sig):
        try:
            expected = getattr(process, "_omakase_identity", None)
            identity = _identity(process.pid)
            if identity is None:
                return
            if expected != identity or os.getpgid(process.pid) != process.pid:
                raise RemuxError("process_ownership_unknown")
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass

    if process.returncode is None:
        send(signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), 1)
        except asyncio.TimeoutError:
            send(signal.SIGKILL)
    await process.wait()


async def _probe_source(
    source, folder, *, transport, authorize, launcher, register, seconds, cpu_check=lambda: 0
):
    process = await launcher(folder, {"mode": "probe", "seconds": seconds})
    register(process)
    tasks = []

    async def bounded(reader, limit, retain=False):
        chunks = []
        count = 0
        while chunk := await reader.read(65536):
            count += len(chunk)
            if count > limit:
                raise RemuxError("probe_output_bound")
            if retain:
                chunks.append(chunk)
        return b"".join(chunks)

    async def watch():
        while True:
            cpu_check()
            await _authorize(authorize)
            _usage(folder)
            await asyncio.sleep(1)

    async def feed():
        destination = await pin_destination(source.base_url, transport=transport)
        length = min(source.part_size, 16 * MIB)
        count = 0
        early = False
        async with httpx.AsyncClient(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(15, connect=5),
        ) as client:
            async with client.stream(
                "GET",
                destination.base_url + source.part_key,
                headers={
                    "X-Plex-Token": source.token,
                    "Accept-Encoding": "identity",
                    "Host": destination.host_header,
                    "Range": f"bytes=0-{length - 1}",
                },
                extensions={"sni_hostname": destination.sni_hostname},
            ) as response:
                if (
                    response.status_code != 206
                    or response.headers.get("content-length") != str(length)
                    or response.headers.get("content-range")
                    != f"bytes 0-{length - 1}/{source.part_size}"
                    or response.headers.get("content-encoding", "identity") != "identity"
                ):
                    raise RemuxError("invalid_probe_response")
                try:
                    async for chunk in response.aiter_raw(chunk_size=65536):
                        count += len(chunk)
                        if count > length:
                            raise RemuxError("probe_input_bound")
                        process.stdin.write(chunk)
                        await process.stdin.drain()
                        if process.returncode is not None:
                            early = True
                            break
                except (BrokenPipeError, ConnectionResetError):
                    early = True
        process.stdin.close()
        try:
            await process.stdin.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            pass
        if (not early and count != length) or await process.wait() != 0:
            raise RemuxError("probe_failed")

    tasks = [
        asyncio.create_task(watch()),
        asyncio.create_task(bounded(process.stdout, 512 * 1024, True)),
        asyncio.create_task(bounded(process.stderr, 65536)),
        asyncio.create_task(feed()),
    ]
    try:
        while not all(t.done() for t in tasks[1:]):
            done, _ = await asyncio.wait(
                [t for t in tasks if not t.done()], return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                if task.exception():
                    raise task.exception()
        try:
            document = json.loads(tasks[1].result())
        except (ValueError, RecursionError):
            raise RemuxError("invalid_probe") from None
        return qualify_tracks(source, document)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if process.returncode is None:
            await asyncio.shield(_stop(process))


async def prepare_remux(
    db,
    *,
    source,
    authorize,
    cache_root,
    transport=None,
    cache_admission=admit_cache,
    held_versions=lambda: (),
    launcher=_launch,
    deadline_seconds=45,
    cpu_reader=_cpu_usage,
):
    """Build once or return fallback error. No retries, queue or encoding.

    ``authorize`` must recheck login, scope and source; ``held_versions`` protects
    active sessions. Tests may inject a fake launcher and quota admission only.
    The caller must acquire a generation hold before calling and retain it while
    serving. Every await is covered by the monotonic 45-second build deadline.
    """
    started = time.monotonic()
    _inventory(source)
    if not 0 < deadline_seconds <= 45:
        raise RemuxError("invalid_deadline")
    try:
        await asyncio.wait_for(_authorize(authorize), deadline_seconds)
    except TimeoutError:
        raise RemuxError("deadline_exceeded") from None
    root = Path(cache_root)
    if root.is_symlink() or not root.is_dir():
        raise RemuxError("unsafe_cache")
    cleanup(db, root, held_versions=held_versions)
    cached = db.execute(
        "SELECT job_id,artifact_json FROM web_playback_remux WHERE state='READY' AND media_version=? AND actor_user_id=? ORDER BY created DESC LIMIT 1",
        (source.media_version, source.actor_user_id),
    ).fetchone()
    if cached is not None:
        artifact = json.loads(cached[1])
        resources = artifact.get("resources", [])
        if (
            artifact.get("job_id") != cached[0]
            or artifact.get("media_version") != source.media_version
            or not isinstance(resources, list)
            or not 1 <= len(resources) <= FILE_LIMIT
        ):
            raise RemuxError("invalid_resources")
        for filename in resources:
            get_resource(db, cached[0], filename, cache_root=root, held_versions=held_versions)
        try:
            await asyncio.wait_for(
                _authorize(authorize), max(0.001, deadline_seconds - (time.monotonic() - started))
            )
        except TimeoutError:
            raise RemuxError("deadline_exceeded") from None
        if time.monotonic() - started >= deadline_seconds:
            raise RemuxError("deadline_exceeded")
        # Explicit prepare/Play may refresh idle retention. Resource GET never does.
        db.execute(
            "UPDATE web_playback_remux SET expires=? WHERE job_id=? AND state='READY'",
            (time.time() + 3600, cached[0]),
        )
        db.commit()
        selected = {s.stream_id: s.selected for s in source.streams}
        for track in artifact.get("track_map", []) + artifact.get("subtitles", []):
            if track.get("plex_stream_id") in selected:
                track["selected"] = selected[track["plex_stream_id"]]
        return artifact
    cpu_start = cpu_reader()
    _admit_with_eviction(db, root, source.part_size, held_versions, cache_admission)

    def cpu_check():
        delta = cpu_reader() - cpu_start
        if delta < 0:
            raise RemuxError("cpu_accounting_changed")
        if delta > 30_000_000:
            raise RemuxError("sampled_cpu_budget_exceeded")
        return delta / 1_000_000

    if time.monotonic() - started >= deadline_seconds:
        raise RemuxError("deadline_exceeded")
    job = secrets.token_hex(16)
    owner = secrets.token_hex(32)
    coordinator_pid = os.getpid()
    coordinator_identity = (
        _identity(coordinator_pid) if sys.platform == "linux" else ("test", "test")
    )
    if coordinator_identity is None:
        raise RemuxError("coordinator_identity_unknown")
    now = time.time()
    # Persist a lease only after its private ownership marker exists. A bootstrap
    # crash therefore never leaves a committed no-PID lease without scan authority.
    folder = _directory(root, job)
    folder.mkdir(mode=0o700)
    (folder / ".omakase-remux").write_text(job)
    try:
        # Single SQLite statement + partial unique index: no transaction across awaits.
        db.execute(
            "INSERT INTO web_playback_remux(job_id,media_version,actor_user_id,state,created,expires,deadline,coordinator_pid,coordinator_start_ticks,coordinator_boot_id,coordinator_token) SELECT ?,?,?,'RUNNING',?,?,?,?,?,?,? WHERE (SELECT COUNT(*) FROM web_playback_remux WHERE state IN ('RUNNING','READY')) < 2",
            (
                job,
                source.media_version,
                source.actor_user_id,
                now,
                now + 3600,
                now + deadline_seconds - (time.monotonic() - started),
                coordinator_pid,
                *coordinator_identity,
                owner,
            ),
        )
        if db.execute("SELECT changes()").fetchone()[0] != 1:
            raise RemuxError("cache_busy")
        db.commit()
    except (sqlite3.IntegrityError, RemuxError) as error:
        db.rollback()
        # No child has launched and the insertion did not reserve this directory.
        (folder / ".omakase-remux").unlink()
        folder.rmdir()
        if isinstance(error, sqlite3.IntegrityError):
            raise RemuxError("worker_busy") from None
        raise
    children = []
    tasks = []
    try:
        async with asyncio.timeout(max(0.001, deadline_seconds - (time.monotonic() - started))):

            def register(child, phase, primary=True):
                children.append(child)
                identity = _identity(child.pid) if sys.platform == "linux" else ("test", "test")
                if identity is None:
                    raise RemuxError("worker_exited")
                child._omakase_identity = identity
                if primary:
                    _lease_update(
                        db,
                        job,
                        owner,
                        "pid=?,start_ticks=?,boot_id=?,phase=?",
                        (child.pid, *identity, phase),
                    )
                else:
                    _lease_update(db, job, owner, "phase=?", (phase,))

            _lease_update(db, job, owner, "phase='probe'")
            recipe = await _probe_source(
                source,
                folder,
                transport=transport,
                authorize=authorize,
                launcher=launcher,
                register=lambda child: register(child, "probe"),
                seconds=deadline_seconds - (time.monotonic() - started),
                cpu_check=cpu_check,
            )
            cpu_check()
            _lease_update(db, job, owner, "phase='handoff',pid=NULL,start_ticks=NULL,boot_id=NULL")
            await _authorize(authorize)
            recipe["seconds"] = deadline_seconds - (time.monotonic() - started)
            _lease_update(db, job, owner, "phase='handoff'")
            mux = await launcher(folder, recipe)
            register(mux, "remux")
            observer = await launcher(
                folder,
                {"mode": "observe", "seconds": deadline_seconds - (time.monotonic() - started)},
            )
            register(observer, "remux", False)

            async def monitor():
                while True:
                    cpu_check()
                    await _authorize(authorize)
                    _usage(folder)
                    await asyncio.sleep(1)

            async def stderr(child):
                count = 0
                while chunk := await child.stderr.read(65536):
                    count += len(chunk)
                    if count > 65536:
                        raise RemuxError("stderr_bound")

            async def transfer():
                destination = await pin_destination(source.base_url, transport=transport)
                digest = hashlib.sha256()
                count = 0
                async with httpx.AsyncClient(
                    transport=transport,
                    trust_env=False,
                    follow_redirects=False,
                    timeout=httpx.Timeout(15, connect=5),
                ) as client:
                    async with client.stream(
                        "GET",
                        destination.base_url + source.part_key,
                        headers={
                            "X-Plex-Token": source.token,
                            "Accept-Encoding": "identity",
                            "Host": destination.host_header,
                        },
                        extensions={"sni_hostname": destination.sni_hostname},
                    ) as response:
                        if (
                            response.status_code != 200
                            or response.headers.get("content-encoding", "identity") != "identity"
                            or response.headers.get("content-length") != str(source.part_size)
                            or "content-range" in response.headers
                        ):
                            raise RemuxError("invalid_source_response")
                        async for chunk in response.aiter_raw(chunk_size=65536):
                            count += len(chunk)
                            if count > source.part_size:
                                raise RemuxError("source_size_changed")
                            digest.update(chunk)
                            mux.stdin.write(chunk)
                            observer.stdin.write(chunk)
                            await asyncio.gather(mux.stdin.drain(), observer.stdin.drain())
                if count != source.part_size:
                    raise RemuxError("source_size_changed")
                mux.stdin.close()
                observer.stdin.close()
                await asyncio.gather(mux.stdin.wait_closed(), observer.stdin.wait_closed())
                exits = await asyncio.gather(mux.wait(), observer.wait())
                if exits != [0, 0]:
                    raise RemuxError("worker_failed")
                return digest.hexdigest()

            tasks = [
                asyncio.create_task(monitor()),
                asyncio.create_task(stderr(mux)),
                asyncio.create_task(stderr(observer)),
                asyncio.create_task(_observe_packets(observer.stdout, source, recipe)),
                asyncio.create_task(transfer()),
            ]
            while not all(task.done() for task in tasks[1:]):
                done, _ = await asyncio.wait(
                    [task for task in tasks if not task.done()], return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    if task.exception():
                        raise task.exception()
            input_hash = tasks[4].result()
            cpu_check()  # Cgroup cumulative accounting retains exited/reaped child CPU.
            summary = tasks[3].result()
            data = json.dumps(summary, separators=(",", ":")).encode()
            if len(data) > PACKET_BYTES:
                raise RemuxError("packet_evidence_bound")
            (folder / PACKET_FILE).write_bytes(data)
            _lease_update(
                db, job, owner, "phase='validating',pid=NULL,start_ticks=NULL,boot_id=NULL"
            )
            await _authorize(authorize)
            try:
                normalize_partial_manifest(
                    db, folder, source, recipe, job_id=job, owner_token=owner
                )
            except CodecMetadataError:
                raise RemuxError("invalid_codec_metadata") from None
            artifact = validate_artifact(folder, source, recipe, require_packet_evidence=True)
            await _authorize(authorize)
            sampled_cpu = cpu_check()
            if time.monotonic() - started >= deadline_seconds:
                raise RemuxError("deadline_exceeded")
            artifact.update(
                job_id=job,
                media_version=source.media_version,
                input_sha256=input_hash,
                sampled_cpu={
                    "seconds": sampled_cpu,
                    "limit_seconds": 30,
                    "sample_interval_seconds": 1,
                    "scope": "cgroup-container-conservative"
                    if cpu_reader is _cpu_usage
                    else "qualified-test-reader",
                },
            )
            _lease_update(
                db,
                job,
                owner,
                "state='READY',phase='complete',expires=?,artifact_json=?",
                (time.time() + 3600, json.dumps(artifact)),
            )
            return artifact
    except BaseException as error:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for child in children:
            if child.stdin is not None:
                child.stdin.close()
        stoppers = [asyncio.create_task(_stop(child)) for child in children]
        joined = asyncio.gather(*stoppers, return_exceptions=True)
        try:
            results = await asyncio.shield(joined)
        except asyncio.CancelledError:
            results = await joined
        if any(isinstance(result, BaseException) for result in results):
            raise RemuxError("process_ownership_unknown") from None
        db.execute(
            "UPDATE web_playback_remux SET state='ERROR',phase='complete' WHERE job_id=? AND state='RUNNING' AND coordinator_token=?",
            (job, owner),
        )
        db.commit()
        cleanup(db, root, held_versions=held_versions)
        if isinstance(error, (asyncio.CancelledError, RemuxError)):
            raise
        raise RemuxError(
            "deadline_exceeded" if isinstance(error, TimeoutError) else "remux_failed"
        ) from None
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
