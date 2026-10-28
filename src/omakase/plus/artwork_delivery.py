"""Small in-memory display derivatives; upstream artwork remains unchanged."""

from __future__ import annotations

import hashlib
import io
import threading
import warnings
from collections import OrderedDict

from PIL import Image, ImageOps, UnidentifiedImageError

from omakase.plus.posters import PosterUnavailable

MAX_SOURCE_BYTES = 6 * 1024 * 1024
MAX_DISPLAY_BYTES = 6 * 1024 * 1024
MAX_PIXELS = 16_000_000
MAX_DIMENSION = 8192
MAX_CACHE_BYTES = 8 * 1024 * 1024
_MAX_CACHE_ENTRIES = 128
_BOUNDS = {"poster": (480, 720), "banner": (2560, 1440)}
_CONTENT_TYPE = "image/webp"
_DERIVATIVE_VERSION = b"hero-2560-q88-v2:"


class _DerivativeCache:
    """LRU of digest/kind keys and derived bytes only, never source payloads."""

    def __init__(self, *, max_bytes: int = MAX_CACHE_BYTES):
        self._max_bytes = min(max_bytes, MAX_CACHE_BYTES)
        self._items: OrderedDict[tuple[bytes, str], bytes] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def get(self, key: tuple[bytes, str]) -> bytes | None:
        with self._lock:
            value = self._items.get(key)
            if value is not None:
                self._items.move_to_end(key)
            return value

    def put(self, key: tuple[bytes, str], value: bytes) -> None:
        if len(value) > self._max_bytes:
            return
        with self._lock:
            previous = self._items.pop(key, None)
            if previous is not None:
                self._bytes -= len(previous)
            self._items[key] = value
            self._bytes += len(value)
            while self._bytes > self._max_bytes or len(self._items) > _MAX_CACHE_ENTRIES:
                _, evicted = self._items.popitem(last=False)
                self._bytes -= len(evicted)


_CACHE = _DerivativeCache()
# Bound decoded-image memory across threadpool misses and coalesce same-key work.
_DERIVE_LOCK = threading.Lock()


def _derive(payload: bytes, *, kind: str) -> bytes:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(payload)) as image:
                if image.format not in {"JPEG", "PNG", "WEBP"}:
                    raise PosterUnavailable("Artwork was not a supported image")
                width, height = image.size
                if (
                    width <= 0
                    or height <= 0
                    or max(width, height) > MAX_DIMENSION
                    or width * height > MAX_PIXELS
                ):
                    raise PosterUnavailable("Artwork exceeds the display limits")
                if getattr(image, "is_animated", False) or getattr(image, "n_frames", 1) != 1:
                    raise PosterUnavailable("Animated artwork is not supported")
                image.load()
                ImageOps.exif_transpose(image, in_place=True)
                mode = "RGBA" if "A" in image.getbands() or "transparency" in image.info else "RGB"
                with image.convert(mode) as pixels:
                    pixels.thumbnail(_BOUNDS[kind], resample=Image.Resampling.LANCZOS)
                    # Fresh pixel storage discards EXIF, XMP, ICC, comments and filenames.
                    with Image.new(mode, pixels.size) as clean:
                        clean.paste(pixels)
                        output = io.BytesIO()
                        clean.save(
                            output, format="WEBP", quality=88 if kind == "banner" else 80, method=4
                        )
                        result = output.getvalue()
                        if len(result) > MAX_DISPLAY_BYTES:
                            raise PosterUnavailable("Artwork exceeds the display limits")
                        return result
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise PosterUnavailable("Artwork exceeds the display limits") from None
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, EOFError):
        raise PosterUnavailable("Artwork was not a valid still image") from None


def optimized_artwork(payload: bytes, *, kind: str) -> tuple[bytes, str]:
    """Return a clean WebP poster/banner; call from a threadpool, not the event loop.

    No source bytes, files or upstream cache entries are changed. Inputs are still
    JPEG/PNG/WebP up to 6 MiB, 16 million pixels and 8192 pixels per side. Derivatives
    fit 480x720 (poster) or 2560x1440 (banner), preserving aspect without upscaling.
    """
    if not isinstance(kind, str) or kind not in _BOUNDS:
        raise PosterUnavailable("Artwork display kind is invalid")
    if not isinstance(payload, bytes) or not payload:
        raise PosterUnavailable("Artwork was not a valid still image")
    if len(payload) > MAX_SOURCE_BYTES:
        raise PosterUnavailable("Artwork exceeds the display limits")
    key = (hashlib.sha256(_DERIVATIVE_VERSION + payload).digest(), kind)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached, _CONTENT_TYPE
    with _DERIVE_LOCK:
        cached = _CACHE.get(key)
        if cached is None:
            cached = _derive(payload, kind=kind)
            _CACHE.put(key, cached)
        return cached, _CONTENT_TYPE
