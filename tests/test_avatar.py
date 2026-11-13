"""Bounded local profile-picture decoding and account-isolated storage."""

from __future__ import annotations

import io
import sqlite3
import struct
import zlib

import pytest
from PIL import Image, PngImagePlugin

from omakase.plus import avatar
from omakase.plus.db import run_migrations


def picture(format="PNG", *, color=(35, 80, 130), size=(400, 300)):
    output = io.BytesIO()
    image = Image.new("RGB", size, color)
    if format == "PNG":
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("private-location", "synthetic-secret-location")
        image.save(output, format, pnginfo=metadata)
    else:
        exif = Image.Exif()
        exif[270] = "synthetic-secret-location"
        image.save(output, format, exif=exif)
    image.close()
    return output.getvalue()


@pytest.mark.parametrize("format", ["JPEG", "PNG", "WEBP"])
def test_supported_images_are_decoded_reencoded_and_stripped_of_metadata(format):
    source = picture(format)
    result = avatar.normalize_avatar(source)
    assert result != source
    assert result.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(result) <= 256 * 1024
    assert b"synthetic-secret-location" not in result
    with Image.open(io.BytesIO(result)) as decoded:
        decoded.load()
        assert decoded.size == (192, 192)
        assert decoded.format == "PNG"
        assert dict(decoded.getexif()) == {}
        assert not decoded.info


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "true")
    conn = sqlite3.connect(tmp_path / "avatar.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    run_migrations(conn)
    for user_id in (1, 2):
        conn.execute(
            "INSERT INTO users(id,email,password_hash) VALUES (?,?,?)",
            (user_id, f"avatar{user_id}@example.invalid", "synthetic"),
        )
    conn.commit()
    yield conn
    conn.close()


def test_avatar_storage_is_per_user_and_context_contains_no_bytes_or_paths(db):
    avatar.save_avatar(db, 1, picture())
    stored = avatar.get_avatar(db, 1)
    assert stored.content_type == "image/png"
    assert len(stored.revision) == 64
    assert avatar.avatar_context(db, 1) == {
        "avatar_available": True,
        "avatar_revision": stored.revision,
        "display_name": "",
    }
    assert avatar.avatar_context(db, 2) == {
        "avatar_available": False,
        "avatar_revision": "",
        "display_name": "",
    }
    assert avatar.get_avatar(db, 2) is None
    assert "image_bytes" not in repr(stored)


def test_replacing_or_removing_avatar_preserves_other_accounts(db):
    avatar.save_avatar(db, 1, picture(color=(255, 0, 0)))
    avatar.save_avatar(db, 2, picture(color=(0, 255, 0)))
    before = avatar.get_avatar(db, 1).revision
    other = avatar.get_avatar(db, 2).revision
    avatar.save_avatar(db, 1, picture(color=(0, 0, 255)))
    assert avatar.get_avatar(db, 1).revision != before
    avatar.remove_avatar(db, 1)
    assert avatar.get_avatar(db, 1) is None
    assert avatar.get_avatar(db, 2).revision == other


@pytest.mark.parametrize(
    "source",
    [
        b"",
        b"not an image",
        b'<svg xmlns="http://www.w3.org/2000/svg"></svg>',
        b"https://external.example.invalid/face.png",
    ],
)
def test_non_images_and_external_urls_are_rejected(source):
    with pytest.raises(avatar.AvatarInputError) as error:
        avatar.normalize_avatar(source)
    assert error.value.code == "image_invalid"
    assert "external.example.invalid" not in str(error.value)


def test_gif_is_rejected_even_when_it_contains_one_frame():
    with pytest.raises(avatar.AvatarInputError) as error:
        avatar.normalize_avatar(picture("GIF"))
    assert error.value.code == "image_format"


@pytest.mark.parametrize("format", ["PNG", "WEBP"])
def test_animated_images_are_rejected(format):
    output = io.BytesIO()
    first = Image.new("RGB", (50, 50), "red")
    second = Image.new("RGB", (50, 50), "blue")
    first.save(output, format=format, save_all=True, append_images=[second], duration=100, loop=0)
    first.close()
    second.close()
    with pytest.raises(avatar.AvatarInputError) as error:
        avatar.normalize_avatar(output.getvalue())
    assert error.value.code == "image_animated"


def test_input_bytes_are_bounded_before_decode():
    with pytest.raises(avatar.AvatarInputError) as error:
        avatar.normalize_avatar(b"x" * (avatar.MAX_INPUT_BYTES + 1))
    assert error.value.code == "image_too_large"


def test_declared_pixels_are_bounded_before_image_data_is_decoded():
    def chunk(name, body):
        return (
            struct.pack(">I", len(body)) + name + body + struct.pack(">I", zlib.crc32(name + body))
        )

    source = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 5000, 4000, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00"))
        + chunk(b"IEND", b"")
    )
    with pytest.raises(avatar.AvatarInputError) as error:
        avatar.normalize_avatar(source)
    assert error.value.code == "image_dimensions"


def test_invalid_replacement_preserves_current_picture(db):
    avatar.save_avatar(db, 1, picture())
    before = avatar.get_avatar(db, 1).revision
    with pytest.raises(avatar.AvatarInputError):
        avatar.save_avatar(db, 1, b"invalid")
    assert avatar.get_avatar(db, 1).revision == before


def test_private_deployment_is_required_for_profile_mutations(db, monkeypatch):
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "false")
    with pytest.raises(avatar.AvatarInputError):
        avatar.save_avatar(db, 1, picture())
    with pytest.raises(avatar.AvatarInputError):
        avatar.remove_avatar(db, 1)
