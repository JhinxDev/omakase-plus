"""Bounded display derivatives leave source artwork intact and private in memory."""

from __future__ import annotations

import hashlib
import io
import struct
import zlib
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest
from PIL import Image, PngImagePlugin

from omakase.plus import artwork_delivery
from omakase.plus.posters import PosterUnavailable


def image_bytes(size=(1200, 1800), *, image_format="PNG", mode="RGB", color=None, **kwargs):
    if color is None:
        color = (40, 100, 170, 80) if mode == "RGBA" else (40, 100, 170)
    with Image.new(mode, size, color) as image:
        output = io.BytesIO()
        image.save(output, format=image_format, **kwargs)
        return output.getvalue()


def png_header(width, height):
    """A real PNG header whose impossible dimensions must fail before pixel loading."""

    def chunk(kind, content):
        return (
            struct.pack(">I", len(content))
            + kind
            + content
            + struct.pack(">I", zlib.crc32(kind + content))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b""))
        + chunk(b"IEND", b"")
    )


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    monkeypatch.setattr(artwork_delivery, "_CACHE", artwork_delivery._DerivativeCache())


@pytest.mark.parametrize("image_format", ["JPEG", "PNG", "WEBP"])
def test_original_bytes_unchanged_and_display_bytes_are_a_bounded_webp(image_format):
    payload = image_bytes(image_format=image_format)
    original = bytes(bytearray(payload))
    original_digest = hashlib.sha256(payload).digest()

    derivative, content_type = artwork_delivery.optimized_artwork(payload, kind="poster")

    assert payload == original
    assert hashlib.sha256(payload).digest() == original_digest
    assert content_type == "image/webp"
    assert derivative != payload
    with Image.open(io.BytesIO(derivative)) as displayed:
        displayed.load()
        assert displayed.format == "WEBP"
        assert displayed.size == (480, 720)
        assert displayed.mode == "RGB"


@pytest.mark.parametrize(
    ("source_size", "kind", "expected_size"),
    [
        ((4000, 2000), "poster", (480, 240)),
        ((4000, 2000), "banner", (2560, 1280)),
        ((800, 2400), "poster", (240, 720)),
        ((800, 2400), "banner", (480, 1440)),
        ((200, 100), "poster", (200, 100)),
        ((200, 100), "banner", (200, 100)),
    ],
)
def test_dimensions_preserve_aspect_and_never_upscale(source_size, kind, expected_size):
    result, _ = artwork_delivery.optimized_artwork(image_bytes(source_size), kind=kind)

    with Image.open(io.BytesIO(result)) as displayed:
        assert displayed.size == expected_size
        assert displayed.width * source_size[1] == displayed.height * source_size[0]


def test_metadata_is_removed_while_exif_orientation_is_respected():
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("private-note", "private-location-do-not-deliver")
    exif = Image.Exif()
    exif[270] = "private-image-description"
    exif[274] = 6
    payload = image_bytes(
        (600, 1000),
        pnginfo=metadata,
        exif=exif,
        icc_profile=b"private-color-profile",
    )

    result, _ = artwork_delivery.optimized_artwork(payload, kind="poster")

    with Image.open(io.BytesIO(result)) as displayed:
        displayed.load()
        assert displayed.size == (480, 288)
        assert not displayed.getexif()
        assert not {"exif", "icc_profile", "xmp", "private-note"} & displayed.info.keys()
    assert b"private-" not in result


@pytest.mark.parametrize("palette", [False, True])
def test_transparency_is_preserved_for_rgba_and_palette_sources(palette):
    if palette:
        with Image.new("P", (60, 90), 0) as image:
            image.putpalette([40, 100, 170] + [0] * 765)
            output = io.BytesIO()
            image.save(output, format="PNG", transparency=0)
            payload = output.getvalue()
        expected_alpha = 0
    else:
        payload = image_bytes((60, 90), mode="RGBA")
        expected_alpha = 80

    result, _ = artwork_delivery.optimized_artwork(payload, kind="poster")

    with Image.open(io.BytesIO(result)) as displayed:
        displayed.load()
        assert displayed.mode == "RGBA"
        assert displayed.getpixel((20, 20))[3] == expected_alpha


@pytest.mark.parametrize("image_format", ["PNG", "WEBP", "GIF"])
def test_animated_sources_are_rejected(image_format):
    with Image.new("RGB", (16, 24), "red") as first:
        with Image.new("RGB", (16, 24), "blue") as second:
            output = io.BytesIO()
            first.save(
                output,
                format=image_format,
                save_all=True,
                append_images=[second],
                duration=100,
                loop=0,
            )
    with pytest.raises(PosterUnavailable):
        artwork_delivery.optimized_artwork(output.getvalue(), kind="poster")


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"not an image",
        b"<svg xmlns='http://www.w3.org/2000/svg'></svg>",
        b"\xff\xd8\xff\xe0truncated-jpeg",
        b"\x89PNG\r\n\x1a\ntruncated-png",
        b"RIFF\x10\x00\x00\x00WEBPbroken",
    ],
)
def test_invalid_or_truncated_sources_are_rejected(payload):
    with pytest.raises(PosterUnavailable):
        artwork_delivery.optimized_artwork(payload, kind="poster")


@pytest.mark.parametrize("image_format", ["GIF", "BMP", "TIFF"])
def test_decodable_but_unsupported_formats_are_rejected(image_format):
    with pytest.raises(PosterUnavailable):
        artwork_delivery.optimized_artwork(
            image_bytes((16, 24), image_format=image_format), kind="poster"
        )


def test_source_byte_limit_is_checked_before_decoding_and_exact_limit_is_allowed():
    limit = 6 * 1024 * 1024
    with patch.object(Image, "open", side_effect=AssertionError("must not decode")):
        with pytest.raises(PosterUnavailable):
            artwork_delivery.optimized_artwork(b"x" * (limit + 1), kind="poster")
    original = image_bytes((16, 24))
    result, _ = artwork_delivery.optimized_artwork(
        original + b"\0" * (limit - len(original)), kind="poster"
    )
    with Image.open(io.BytesIO(result)) as displayed:
        assert displayed.size == (16, 24)


@pytest.mark.parametrize("payload", [None, "not bytes", bytearray(b"not bytes")])
def test_non_byte_payload_is_rejected(payload):
    with pytest.raises(PosterUnavailable):
        artwork_delivery.optimized_artwork(payload, kind="poster")


@pytest.mark.parametrize("dimensions", [(5000, 4000), (8193, 1), (1, 8193), (100_000, 100_000)])
def test_oversized_dimensions_fail_before_decoded_pixel_allocation(dimensions):
    with patch.object(Image.Image, "load", side_effect=AssertionError("must not load pixels")):
        with pytest.raises(PosterUnavailable):
            artwork_delivery.optimized_artwork(png_header(*dimensions), kind="banner")


@pytest.mark.parametrize("kind", ["", "avatar", "POSTER", None, 42])
def test_unknown_derivative_kind_is_rejected(kind):
    with pytest.raises(PosterUnavailable):
        artwork_delivery.optimized_artwork(image_bytes((16, 24)), kind=kind)


def test_repeat_uses_cache_but_a_different_source_or_kind_does_not():
    first_source = image_bytes((60, 90))
    second_source = image_bytes((60, 90), color=(170, 40, 100))
    with patch.object(Image, "open", wraps=Image.open) as decode:
        first = artwork_delivery.optimized_artwork(first_source, kind="poster")
        repeated = artwork_delivery.optimized_artwork(first_source, kind="poster")
        artwork_delivery.optimized_artwork(first_source, kind="banner")
        changed = artwork_delivery.optimized_artwork(second_source, kind="poster")

    assert decode.call_count == 3
    assert repeated[0] is first[0]
    assert changed[0] != first[0]
    assert set(artwork_delivery._CACHE._items) == {
        (hashlib.sha256(artwork_delivery._DERIVATIVE_VERSION + first_source).digest(), "poster"),
        (hashlib.sha256(artwork_delivery._DERIVATIVE_VERSION + first_source).digest(), "banner"),
        (hashlib.sha256(artwork_delivery._DERIVATIVE_VERSION + second_source).digest(), "poster"),
    }
    assert first_source not in artwork_delivery._CACHE._items.values()
    assert second_source not in artwork_delivery._CACHE._items.values()


def test_cache_evicts_least_recently_used_derived_bytes_at_its_byte_bound(monkeypatch):
    sources = [image_bytes((60, 90), color=color) for color in ("red", "green", "blue")]
    results = [artwork_delivery.optimized_artwork(source, kind="poster")[0] for source in sources]
    limit = max(map(len, results)) * 2
    cache = artwork_delivery._DerivativeCache(max_bytes=limit)
    monkeypatch.setattr(artwork_delivery, "_CACHE", cache)
    artwork_delivery.optimized_artwork(sources[0], kind="poster")
    artwork_delivery.optimized_artwork(sources[1], kind="poster")
    artwork_delivery.optimized_artwork(sources[0], kind="poster")
    artwork_delivery.optimized_artwork(sources[2], kind="poster")

    assert sum(map(len, cache._items.values())) <= limit <= 8 * 1024 * 1024
    assert (
        hashlib.sha256(artwork_delivery._DERIVATIVE_VERSION + sources[0]).digest(),
        "poster",
    ) in cache._items
    assert (
        hashlib.sha256(artwork_delivery._DERIVATIVE_VERSION + sources[1]).digest(),
        "poster",
    ) not in cache._items
    assert (
        hashlib.sha256(artwork_delivery._DERIVATIVE_VERSION + sources[2]).digest(),
        "poster",
    ) in cache._items


def test_concurrent_repeated_requests_share_one_safe_cached_derivative():
    source = image_bytes((60, 90))
    with patch.object(Image, "open", wraps=Image.open) as decode:
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(
                executor.map(
                    lambda _: artwork_delivery.optimized_artwork(source, kind="poster"),
                    range(24),
                )
            )
    assert decode.call_count == 1
    assert all(result[0] is results[0][0] for result in results)
    assert sum(map(len, artwork_delivery._CACHE._items.values())) <= 8 * 1024 * 1024


def test_derivative_exceeding_cache_budget_is_delivered_without_retention(monkeypatch):
    cache = artwork_delivery._DerivativeCache(max_bytes=1)
    monkeypatch.setattr(artwork_delivery, "_CACHE", cache)

    result, content_type = artwork_delivery.optimized_artwork(image_bytes((60, 90)), kind="poster")

    assert content_type == "image/webp"
    with Image.open(io.BytesIO(result)) as displayed:
        assert displayed.size == (60, 90)
    assert not cache._items
    assert cache._bytes == 0


def test_wide_hero_retains_available_pixels_without_changing_poster_budget():
    payload = image_bytes((3840, 2160))
    banner, _ = artwork_delivery.optimized_artwork(payload, kind="banner")
    poster, _ = artwork_delivery.optimized_artwork(payload, kind="poster")
    with Image.open(io.BytesIO(banner)) as displayed:
        assert displayed.size == (2560, 1440)
    with Image.open(io.BytesIO(poster)) as displayed:
        assert displayed.size == (480, 270)
    assert len(banner) < artwork_delivery.MAX_SOURCE_BYTES


def test_banner_retains_original_small_resolution_for_client_quality_decision():
    payload = image_bytes((900, 300))
    banner, _ = artwork_delivery.optimized_artwork(payload, kind="banner")
    with Image.open(io.BytesIO(banner)) as displayed:
        assert displayed.size == (900, 300)


def test_derivative_recipe_change_cannot_reuse_previous_cached_pixels(monkeypatch):
    payload = image_bytes((60, 90))
    original, _ = artwork_delivery.optimized_artwork(payload, kind="banner")
    monkeypatch.setattr(artwork_delivery, "_DERIVATIVE_VERSION", b"next-recipe:")
    with patch.object(Image, "open", wraps=Image.open) as decode:
        updated, _ = artwork_delivery.optimized_artwork(payload, kind="banner")
    assert decode.call_count == 1 and original == updated
    assert len(artwork_delivery._CACHE._items) == 2


def test_output_byte_budget_is_enforced_before_retention(monkeypatch):
    monkeypatch.setattr(artwork_delivery, "MAX_DISPLAY_BYTES", 1)
    with pytest.raises(PosterUnavailable, match="display limits"):
        artwork_delivery.optimized_artwork(image_bytes((60, 90)), kind="banner")
    assert not artwork_delivery._CACHE._items
