"""Read indexed Matroska text captions without transferring the video."""

from __future__ import annotations

import asyncio
import html
import re
from dataclasses import dataclass

from starlette.requests import Request

from omakase.plus.web_playback_delivery import DeliveryError, open_part
from omakase.plus.web_playback_source import PlexStream

_HEADER_BYTES = 64 * 1024
_SECTION_BYTES = 2 * 1024**2
_GROUP_BYTES = 4096
_MAX_TIME = 4 * 60 * 60 * 1000


@dataclass(frozen=True, slots=True)
class Cue:
    start: int
    end: int
    cluster: int
    relative: int


@dataclass(frozen=True, slots=True)
class Track:
    stream: PlexStream
    number: int
    cues: tuple[Cue, ...]


async def read_part_range(source, offset, size, *, authorize, transport):
    if not (0 <= offset < offset + size <= source.part_size and size <= _SECTION_BYTES):
        raise ValueError("Invalid caption source range")
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "headers": [(b"range", f"bytes={offset}-{offset + size - 1}".encode("ascii"))],
        }
    )
    async with asyncio.timeout(4):
        response = await open_part(
            source, request, authorize=authorize, transport=transport, internal_matroska=True
        )
        if response.status_code != 206:
            raise DeliveryError("Caption source range unavailable.")
        try:
            data = b"".join([chunk async for chunk in response.body_iterator])
        finally:
            await response.close()
    if len(data) != size:
        raise DeliveryError("Incomplete caption source range.")
    return data


def _vint(data, position, *, element_id=False):
    if position >= len(data) or not data[position]:
        raise ValueError("Invalid Matroska variable integer")
    width = 9 - data[position].bit_length()
    stop = position + width
    if stop > len(data) or (element_id and width > 4):
        raise ValueError("Truncated Matroska variable integer")
    value = int.from_bytes(data[position:stop], "big")
    mask = (1 << (7 * width)) - 1
    return (value if element_id else value & mask), stop, mask


def _header(data, position=0):
    tag, position, _ = _vint(data, position, element_id=True)
    length, start, mask = _vint(data, position)
    return tag, start, None if length == mask else start + length


def _entries(data):
    view = memoryview(data)
    cursor = 0
    while cursor < len(data):
        tag, start, end = _header(data, cursor)
        if end is None or end > len(data):
            raise ValueError("Incomplete Matroska element")
        yield tag, view[start:end]
        cursor = end


def _integer(value):
    if not 1 <= len(value) <= 8:
        raise ValueError("Invalid Matroska integer")
    return int.from_bytes(value, "big")


async def _section(read, prefix, file_end, offset, tag):
    if not 0 <= offset < file_end:
        raise ValueError("Invalid Matroska index offset")
    header_size = min(12, file_end - offset)
    header = (
        prefix[offset : offset + header_size]
        if offset + header_size <= len(prefix)
        else await read(offset, header_size)
    )
    actual, start, end = _header(header)
    if actual != tag or end is None or not (start < end <= start + _SECTION_BYTES):
        raise ValueError("Invalid Matroska index section")
    if offset + end > file_end:
        raise ValueError("Matroska index exceeds the file")
    if offset + end <= len(prefix):
        return memoryview(prefix)[offset + start : offset + end]
    return await read(offset + start, end - start)


def _seek_index(prefix, size):
    tag, start, end = _header(prefix)
    if (
        tag != 0x1A45DFA3
        or end is None
        or end > len(prefix)
        or dict(_entries(prefix[start:end])).get(0x4282) != b"matroska"
    ):
        raise ValueError("Missing Matroska document header")
    tag, origin, segment_end = _header(prefix, end)
    if tag != 0x18538067 or (segment_end is not None and segment_end > size):
        raise ValueError("Missing Matroska Segment")
    positions = {}
    cursor = origin
    while cursor < len(prefix):
        tag, start, end = _header(prefix, cursor)
        if end is None or end > len(prefix) or tag == 0x1F43B675:
            break
        if tag == 0x114D9B74:
            for key, data in _entries(prefix[start:end]):
                if key == 0x4DBB:
                    seek = dict(_entries(data))
                    if 0x53AB in seek and 0x53AC in seek:
                        positions.setdefault(
                            _integer(seek[0x53AB]), origin + _integer(seek[0x53AC])
                        )
        if all(tag in positions for tag in (0x1549A966, 0x1654AE6B, 0x1C53BB6B)):
            return origin, segment_end if segment_end is not None else size, positions
        cursor = end
    raise ValueError("Missing Matroska subtitle index")


def _text_tracks(data, streams):
    candidates = {
        track.container_index: track
        for track in streams
        if track.kind == "subtitle" and track.codec in {"srt", "subrip"}
    }
    result = {}
    for index, entry in enumerate(data for tag, data in _entries(data) if tag == 0xAE):
        if index not in candidates:
            continue
        fields = dict(_entries(entry))
        if (
            fields.get(0x86) == b"S_TEXT/UTF8"
            and _integer(fields.get(0x83, b"\x00")) == 17
            and not fields.keys() & {0x6D80, 0x23314F, 0x537F, 0x56AA}
        ):
            number = _integer(fields.get(0xD7, b"\x00"))
            if not number or number in result:
                raise ValueError("Ambiguous Matroska subtitle track")
            result[number] = candidates[index]
    return result


def _indexed_cues(data, tracks, origin, size):
    cues = {number: [] for number in tracks}
    invalid = set()
    for tag, entry in _entries(data):
        if tag != 0xBB:
            continue
        fields = list(_entries(entry))
        time = _integer(next((body for key, body in fields if key == 0xB3), b"\x00"))
        for key, body in fields:
            if key != 0xB7:
                continue
            position = dict(_entries(body))
            number = _integer(position.get(0xF7, b"\x00"))
            if number not in tracks or number in invalid:
                continue
            cluster = origin + _integer(position.get(0xF1, b"\x00"))
            relative = _integer(position.get(0xF0, b"\x00"))
            duration = _integer(position.get(0xB2, b"\x00"))
            if (
                not {0xF0, 0xF1, 0xB2} <= position.keys()
                or not 0 <= time < time + duration <= _MAX_TIME
                or not origin < cluster <= cluster + relative < size
            ):
                invalid.add(number)
                continue
            cues[number].append(Cue(time, time + duration, cluster, relative))
    return tuple(
        Track(
            stream,
            number,
            tuple(sorted(set(cues[number]), key=lambda cue: (cue.start, cue.relative))),
        )
        for number, stream in tracks.items()
        if number not in invalid and 0 < len(cues[number]) <= 20_000
    )


async def indexed_tracks(read, size, streams):
    prefix = await read(0, min(size, _HEADER_BYTES))
    origin, file_end, positions = _seek_index(prefix, size)
    info, tracks, cues = 0x1549A966, 0x1654AE6B, 0x1C53BB6B
    fields = dict(_entries(await _section(read, prefix, file_end, positions[info], info)))
    if _integer(fields.get(0x2AD7B1, b"\x0f\x42\x40")) != 1_000_000:
        return ()
    known = _text_tracks(await _section(read, prefix, file_end, positions[tracks], tracks), streams)
    if not known:
        return ()
    data = await _section(read, prefix, file_end, positions[cues], cues)
    return _indexed_cues(data, known, origin, file_end)


async def _cue_text(read, size, number, cue):
    data = await read(cue.cluster, min(256, size - cue.cluster))
    tag, start, end = _header(data)
    if tag != 0x1F43B675 or end is None or cue.cluster + end > size:
        raise ValueError("Invalid Matroska cue Cluster")
    timestamp = next((v for k, v in _entries(data[start : min(end, len(data))]) if k == 0xE7), b"")
    position = cue.cluster + start + cue.relative
    limit = cue.cluster + end - position
    if not timestamp or limit <= 0:
        raise ValueError("Invalid Matroska cue position")
    data = await read(position, min(512, limit))
    tag, start, end = _header(data)
    if tag != 0xA0 or end is None or not start < end <= min(limit, _GROUP_BYTES):
        raise ValueError("Invalid Matroska text BlockGroup")
    if end > len(data):
        data += await read(position + len(data), end - len(data))
    fields = dict(_entries(data[start:end]))
    block = fields.get(0xA1, b"")
    track, start, _ = _vint(block, 0)
    if track != number or start + 3 >= len(block) or block[start + 2] & 0x06:
        raise ValueError("Invalid Matroska text Block")
    offset = int.from_bytes(block[start : start + 2], "big", signed=True)
    if (
        _integer(timestamp) + offset != cue.start
        or _integer(fields.get(0x9B, b"\x00")) != cue.end - cue.start
    ):
        raise ValueError("Matroska cue timestamp mismatch")
    text = bytes(block[start + 3 :]).decode("utf-8")
    text = "\n".join(
        line.strip()
        for line in re.sub(r"[\x00-\x08\x0b-\x0c\x0e-\x1f]", "", text).splitlines()
        if line.strip()
    )
    return re.sub(r"&lt;(/?[biu])&gt;", r"<\1>", html.escape(text, quote=False))


def _clock(ms):
    seconds, fraction = divmod(ms, 1000)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}.{fraction:03}"


async def fragment(read, size, track, start, end, mpegts):
    cues = [cue for cue in track.cues if cue.start < end and start < cue.end]
    if len(cues) > 32:
        raise ValueError("Too many indexed cues in one segment")
    rows = ["WEBVTT", f"X-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:{mpegts}", ""]
    for cue in cues:
        text = await _cue_text(read, size, track.number, cue)
        if text:
            rows.extend((f"{_clock(cue.start)} --> {_clock(cue.end)}", text, ""))
    return ("\n".join(rows) + "\n").encode("utf-8")
