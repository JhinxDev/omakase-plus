"""Exact HEVC RFC 6381 metadata from bounded, owned MP4 initialization files.

FFmpeg 7.1 DASH emits a bare sample-entry tag for HEVC. Profile metadata comes
from that entry's hvcC, never a browser guess or a hev1-to-hvc1 substitution.
"""

from __future__ import annotations

import os
import re
import sqlite3
import stat
import xml.etree.ElementTree as ET
from pathlib import Path

LIMIT = 1024**2
NS = "urn:mpeg:dash:schema:mpd:2011"
Q = "{" + NS + "}"


class CodecMetadataError(ValueError):
    def __init__(self):
        super().__init__("invalid_codec_metadata")


def _require(ok):
    if not ok:
        raise CodecMetadataError()


def _boxes(data, start, end, depth, counter):
    _require(depth <= 8 and 0 <= start <= end <= len(data))
    result = []
    while start < end:
        _require(end - start >= 8)
        size, kind = int.from_bytes(data[start : start + 4], "big"), data[start + 4 : start + 8]
        header = 8
        if size == 1:
            _require(end - start >= 16)
            size, header = int.from_bytes(data[start + 8 : start + 16], "big"), 16
        _require(size >= header and start + size <= end)  # No open-ended size=0.
        counter[0] += 1
        _require(counter[0] <= 128)
        result.append((kind, start + header, start + size))
        start += size
    _require(start == end)
    return result


def _one(boxes, kind):
    matches = [box for box in boxes if box[0] == kind]
    _require(len(matches) == 1)
    return matches[0]


def _configuration(data, entry):
    _require(len(data) >= 23 and data[0] == 1)
    _require(data[13] & 0xF0 == 0xF0 and data[15] & 0xFC == 0xFC)
    _require(data[16] & 0xFC == 0xFC and data[17] & 0xF8 == 0xF8 and data[18] & 0xF8 == 0xF8)
    _require(data[21] & 3 in {0, 1, 3} and (data[21] >> 3) & 7)
    profile = data[1]
    compatibility = int.from_bytes(data[2:6], "big")
    reversed_flags = int(f"{compatibility:032b}"[::-1], 2)
    constraints = tuple(data[6:12])
    _require(profile & 31 and data[12] and 1 <= data[22] <= 16)
    offset, types, total = 23, set(), 0
    for _ in range(data[22]):
        _require(offset + 3 <= len(data))
        flags = data[offset]
        kind = flags & 63
        count = int.from_bytes(data[offset + 1 : offset + 3], "big")
        _require(not flags & 0x40 and kind in {32, 33, 34, 39, 40} and kind not in types)
        _require(1 <= count <= 128)
        if entry == "hvc1" and kind in {32, 33, 34}:
            _require(flags & 0x80)
        types.add(kind)
        offset += 3
        total += count
        _require(total <= 256)
        for _ in range(count):
            _require(offset + 2 <= len(data))
            length = int.from_bytes(data[offset : offset + 2], "big")
            offset += 2
            _require(length >= 2 and offset + length <= len(data))
            _require(not data[offset] & 0x80 and (data[offset] >> 1) & 63 == kind)
            _require(data[offset + 1] & 7)
            offset += length
    _require(offset == len(data) and {32, 33, 34} <= types)
    return (
        entry,
        profile >> 6,
        profile & 31,
        reversed_flags,
        "H" if profile & 32 else "L",
        data[12],
        constraints,
    )


def _render(fields):
    entry, space, profile, compatibility, tier, level, constraints = fields
    suffix = list(constraints)
    while suffix and suffix[-1] == 0:
        suffix.pop()
    return (
        f"{entry}.{('', 'A', 'B', 'C')[space]}{profile}.{compatibility:X}.{tier}{level}"
        + "".join(f".{byte:X}" for byte in suffix)
    )


def _init_fields(data):
    _require(isinstance(data, bytes) and 0 < len(data) <= LIMIT)
    counter = [0]
    boxes = _boxes(data, 0, len(data), 0, counter)
    _one(boxes, b"ftyp")
    _, start, end = _one(boxes, b"moov")
    children = _boxes(data, start, end, 1, counter)
    _, start, end = _one(children, b"trak")
    for depth, kind in enumerate((b"mdia", b"minf", b"stbl", b"stsd"), 2):
        children = _boxes(data, start, end, depth, counter)
        if kind == b"minf":
            _, left, right = _one(children, b"hdlr")
            _require(right - left >= 12 and data[left + 8 : left + 12] == b"vide")
        _, start, end = _one(children, kind)
    _require(
        end - start >= 8
        and data[start : start + 4] == bytes(4)
        and int.from_bytes(data[start + 4 : start + 8], "big") == 1
    )
    entries = _boxes(data, start + 8, end, 6, counter)
    _require(len(entries) == 1 and entries[0][0] in {b"hev1", b"hvc1"})
    kind, start, end = entries[0]
    _require(end - start >= 78)
    children = _boxes(data, start + 78, end, 7, counter)
    _, start, end = _one(children, b"hvcC")
    return _configuration(data[start:end], kind.decode("ascii"))


def hevc_codec_from_init(data):
    """Parse one self-contained video init; no media packet decode or network."""
    try:
        return _render(_init_fields(data))
    except (ValueError, IndexError, OverflowError):
        raise CodecMetadataError() from None


def _codec_fields(value):
    _require(isinstance(value, str) and len(value) <= 64)
    parts = value.split(".")
    _require(4 <= len(parts) <= 10 and parts[0] in {"hev1", "hvc1"})
    profile = re.fullmatch(r"([ABC]?)([0-9]{1,2})", parts[1])
    tier = re.fullmatch(r"([LH])([0-9]{1,3})", parts[3])
    _require(
        profile is not None and tier is not None and re.fullmatch(r"[0-9a-fA-F]{1,8}", parts[2])
    )
    _require(int(profile[2]) <= 31 and int(tier[2]) <= 255)
    _require(all(re.fullmatch(r"[0-9a-fA-F]{1,2}", part) for part in parts[4:]))
    constraints = tuple(int(part, 16) for part in parts[4:]) + (0,) * (10 - len(parts))
    return (
        parts[0],
        ("", "A", "B", "C").index(profile[1]),
        int(profile[2]),
        int(parts[2], 16),
        tier[1],
        int(tier[2]),
        constraints,
    )


def _read(folder, name):
    path = folder / name
    _require(not path.is_symlink() and path.is_file())
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    try:
        info = os.fstat(fd)
        _require(stat.S_ISREG(info.st_mode) and 0 < info.st_size <= LIMIT)
        raw = bytearray()
        while block := os.read(fd, min(65536, LIMIT + 1 - len(raw))):
            raw.extend(block)
            _require(len(raw) <= LIMIT)
        _require(len(raw) == info.st_size)
        return bytes(raw)
    finally:
        os.close(fd)


def normalize_partial_manifest(db, folder, source, recipe, *, job_id, owner_token):
    """Normalize only this coordinator's RUNNING/validating owned partial.

    Caller must next run the complete artifact validator before publishing READY.
    An existing READY artifact is never eligible, even when its codec is bare.
    """
    if source.video_codec != "hevc":
        return None
    temp = None
    try:
        _require(re.fullmatch(r"[0-9a-f]{32}", job_id) is not None)
        row = db.execute(
            "SELECT state,phase,coordinator_pid,coordinator_token,media_version,actor_user_id FROM web_playback_remux WHERE job_id=?",
            (job_id,),
        ).fetchone()
        _require(
            row is not None
            and tuple(row)
            == (
                "RUNNING",
                "validating",
                os.getpid(),
                owner_token,
                source.media_version,
                source.actor_user_id,
            )
        )
        folder = Path(folder)
        _require(
            folder.name == job_id
            and not folder.is_symlink()
            and folder.is_dir()
            and folder.absolute() == folder.resolve(strict=True)
        )
        _require(_read(folder, ".omakase-remux") == job_id.encode("ascii"))
        raw = _read(folder, "manifest.mpd")
        _require(
            b"<!DOCTYPE" not in raw.upper() and b"<!ENTITY" not in raw.upper() and b"\0" not in raw
        )
        root = ET.fromstring(raw)
        _require(root.tag == Q + "MPD" and root.get("type") == "static")
        nodes = list(root.iter())
        _require(
            len(nodes) <= 16384
            and all(isinstance(node.tag, str) and node.tag.startswith(Q) for node in nodes)
        )
        _require(
            not any(
                node.tag == Q + "BaseURL" or any("xlink" in key.lower() for key in node.attrib)
                for node in nodes
            )
        )
        pending = [(root, 0)]
        while pending:
            node, depth = pending.pop()
            _require(depth <= 12)
            pending.extend((child, depth + 1) for child in node)
        reps = root.findall(".//" + Q + "Representation")
        _require(recipe.get("mode") == "remux")
        video = [stream for stream in source.streams if stream.kind == "video"]
        audio = [stream for stream in source.streams if stream.kind == "audio"]
        _require(
            len(video) == 1
            and video[0].codec == "hevc"
            and recipe["video"]["id"] == video[0].stream_id
        )
        by_id = {stream.stream_id: stream for stream in video + audio}
        tracks = [recipe["video"]] + recipe["audio"]
        _require(len(by_id) == len(tracks) and {track["id"] for track in tracks} == set(by_id))
        _require(
            all(
                type(track["index"]) is int
                and 0 <= track["index"] <= 127
                and (
                    by_id[track["id"]].container_index is None
                    or by_id[track["id"]].container_index == track["index"]
                )
                for track in tracks
            )
        )
        _require(1 <= len(recipe["audio"]) <= 8 and len(reps) == len(recipe["audio"]) + 1)
        _require([rep.get("id") for rep in reps] == [str(index) for index in range(len(reps))])
        rep = reps[0]
        _require(rep.get("mimeType") == "video/mp4")
        _require(not any(rep.get("codecs", "").startswith(("hev1", "hvc1")) for rep in reps[1:]))
        template = rep.find(Q + "SegmentTemplate")
        _require(
            template is not None and template.get("initialization") == "init-$RepresentationID$.mp4"
        )
        fields = _init_fields(_read(folder, "init-0.mp4"))
        codec = _render(fields)
        old = rep.get("codecs", "")
        if old not in {"hev1", "hvc1"}:
            _require(_codec_fields(old) == fields)
            return codec  # A correct qualified string is retained byte-for-byte.
        _require(old == fields[0])
        rep.set("codecs", codec)
        # Preserve a default MPD namespace without mutating ElementTree's global
        # namespace registry (which is shared with concurrent app operations).
        for node in nodes:
            node.tag = node.tag[len(Q) :]
        root.set("xmlns", NS)
        updated = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        _require(len(updated) <= LIMIT)
        temp = folder / ".manifest-codecs.tmp"
        fd = os.open(
            temp,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_BINARY", 0),
            0o600,
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(updated)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, folder / "manifest.mpd")
        finally:
            if temp.exists():
                temp.unlink()
        return codec
    except (
        OSError,
        ValueError,
        IndexError,
        KeyError,
        TypeError,
        AttributeError,
        ET.ParseError,
        sqlite3.Error,
    ):
        raise CodecMetadataError() from None
