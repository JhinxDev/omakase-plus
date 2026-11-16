"""Render one on-demand Matroska image subtitle window without writing media."""

from __future__ import annotations

import asyncio
import base64
import io
import re
from contextlib import suppress

from PIL import Image

_PNG = b"\x89PNG\r\n\x1a\n"
_TIMESTAMPS = re.compile(rb"\bpts_time:([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)(?=\s)")
_LIMIT = 2 * 1024 * 1024
_MAX_PIXELS = 64 * 1024 * 1024
_MAX_FRAMES = 128


class BitmapError(RuntimeError):
    """The subtitle window could not be safely rendered."""


async def _ffmpeg(arguments, feed):
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        *arguments,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def write():
        try:
            await feed(process.stdin)
        finally:
            process.stdin.close()

    async def read(pipe, limit):
        data = bytearray()
        while part := await pipe.read(16 * 1024):
            data.extend(part)
            if len(data) > limit:
                raise BitmapError("subtitle window exceeded its output budget")
        return bytes(data)

    tasks = (
        asyncio.create_task(write()),
        asyncio.create_task(read(process.stdout, _LIMIT)),
        asyncio.create_task(read(process.stderr, 256 * 1024)),
    )
    try:
        _, output, log = await asyncio.gather(*tasks)
        if await process.wait():
            raise BitmapError("subtitle decoder failed")
        return output, log
    finally:
        for task in tasks:
            task.cancel()
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
        await asyncio.gather(*tasks, return_exceptions=True)
        await process.wait()


def _png_frames(data):
    position = 0
    count = 0
    while position < len(data):
        start = position
        if data[position : position + 8] != _PNG or count >= _MAX_FRAMES:
            raise BitmapError("invalid subtitle frame stream")
        count += 1
        position += 8
        while position + 12 <= len(data):
            size = int.from_bytes(data[position : position + 4], "big")
            end = position + size + 12
            if end > len(data):
                raise BitmapError("truncated subtitle frame")
            kind = data[position + 4 : position + 8]
            position = end
            if kind == b"IEND":
                yield data[start:end]
                break
        else:
            raise BitmapError("incomplete subtitle frame")


def _validate_sup(data):
    position = 0
    while position < len(data):
        if position + 13 > len(data) or data[position : position + 2] != b"PG":
            raise BitmapError("invalid subtitle packet")
        size = int.from_bytes(data[position + 11 : position + 13], "big")
        end = position + 13 + size
        if end > len(data):
            raise BitmapError("truncated subtitle packet")
        if data[position + 10] == 0x16:
            width = int.from_bytes(data[position + 13 : position + 15], "big")
            height = int.from_bytes(data[position + 15 : position + 17], "big")
            if size < 4 or not (0 < width <= 4096 and 0 < height <= 2160):
                raise BitmapError("subtitle canvas exceeded its pixel budget")
        position = end


def _cues(images, log, origin, start, end):
    times = [float(value) - origin for value in _TIMESTAMPS.findall(log)]
    frames = list(_png_frames(images))
    if len(times) != len(frames) or times != sorted(times):
        raise BitmapError("subtitle timestamps did not match the rendered frames")
    cues = []
    output_bytes = 0
    pixels = 0
    for i, (timestamp, frame) in enumerate(zip(times, frames, strict=True)):
        until = min(times[i + 1] if i + 1 < len(times) else end, end)
        since = max(timestamp, start)
        # FFmpeg emits a previous state immediately before some transitions.
        if until - since < 0.002:
            continue
        with Image.open(io.BytesIO(frame)) as image:
            width, height = image.size
            pixels += width * height
            if width > 4096 or height > 2160 or pixels > _MAX_PIXELS:
                raise BitmapError("subtitle canvas exceeded its pixel budget")
            rgba = image.convert("RGBA")
            box = rgba.getchannel("A").getbbox()
            if box is None:
                continue
            png = io.BytesIO()
            rgba.crop(box).save(png, format="PNG")
            data = base64.b64encode(png.getvalue()).decode("ascii")
        output_bytes += len(data)
        if output_bytes > _LIMIT:
            raise BitmapError("subtitle window exceeded its image budget")
        x, y, right, bottom = box
        cues.append(
            {
                "start": round(since, 6),
                "end": round(until, 6),
                "box": [x / width, y / height, (right - x) / width, (bottom - y) / height],
                "image": data,
            }
        )
    return cues


async def render_window(registry, plan, number, ordinal):
    """Reuse the previous keyframe for a cue spanning this window's first frame."""

    async def feed_media(pipe):
        await registry._feed(plan, number, pipe, from_number=max(0, number - 1))

    try:
        async with asyncio.timeout(10):
            raw, _ = await _ffmpeg(
                (
                    "-loglevel",
                    "error",
                    "-copyts",
                    "-i",
                    "pipe:0",
                    "-map",
                    f"0:s:{ordinal}",
                    "-c:s",
                    "copy",
                    "-f",
                    "sup",
                    "pipe:1",
                ),
                feed_media,
            )
            start, end = plan.timeline.segments[number]
            cues = []
            if raw:
                _validate_sup(raw)

                async def feed_subtitles(pipe):
                    pipe.write(raw)
                    await pipe.drain()

                images, log = await _ffmpeg(
                    (
                        "-loglevel",
                        "info",
                        "-f",
                        "sup",
                        "-copyts",
                        "-i",
                        "pipe:0",
                        "-filter_complex",
                        "[0:s:0]format=rgba,showinfo[rgba]",
                        "-map",
                        "[rgba]",
                        "-an",
                        "-c:v",
                        "png",
                        "-fps_mode",
                        "passthrough",
                        "-f",
                        "image2pipe",
                        "pipe:1",
                    ),
                    feed_subtitles,
                )
                cues = await asyncio.to_thread(_cues, images, log, plan.timeline.origin, start, end)
            return {"start": start, "end": end, "cues": cues}
    except (OSError, ValueError, TimeoutError) as error:
        raise BitmapError("subtitle window could not be opened") from error
