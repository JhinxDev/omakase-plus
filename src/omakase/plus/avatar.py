"""Private account pictures, decoded locally and retained as bounded clean PNGs."""

from __future__ import annotations

import hashlib
import io
import os
import warnings
from dataclasses import dataclass, field

from PIL import Image, ImageOps, UnidentifiedImageError

from omakase.plus.secrets import read_secret

MAX_INPUT_BYTES = 8 * 1024 * 1024
MAX_STORED_BYTES = 256 * 1024
MAX_PIXELS = 16_000_000
MAX_DIMENSION = 8192
AVATAR_SIZE = 192
_DETAILS = {
    "image_invalid": "Choose a valid JPEG, PNG or WebP picture.",
    "image_format": "Use a JPEG, PNG or WebP picture. SVG and GIF are not supported.",
    "image_animated": "Choose a still picture. Animated images are not supported.",
    "image_too_large": "Choose a picture smaller than 8 MiB.",
    "image_dimensions": "Choose a picture with at most 16 million pixels and sides no larger than 8192 pixels.",
    "private_required": "Profile pictures are available only in a private Plus deployment.",
}


class AvatarInputError(ValueError):
    """A controlled message that never includes metadata or a submitted filename."""

    def __init__(self, code: str):
        self.code = code if code in _DETAILS else "image_invalid"
        super().__init__(_DETAILS[self.code])


@dataclass(frozen=True, slots=True)
class Avatar:
    image_bytes: bytes = field(repr=False)
    revision: str
    content_type: str = "image/png"


def private_avatar_enabled() -> bool:
    return os.getenv("OMAKASE_PLUS_PRIVATE", "false").strip().lower() == "true"


def _guard() -> None:
    if not private_avatar_enabled():
        raise AvatarInputError("private_required")


def normalize_avatar(source: bytes) -> bytes:
    """Decode an allowed still image and discard all source metadata and trailing bytes."""
    if not isinstance(source, bytes) or not source:
        raise AvatarInputError("image_invalid")
    if len(source) > MAX_INPUT_BYTES:
        raise AvatarInputError("image_too_large")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(source)) as image:
                if image.format not in {"JPEG", "PNG", "WEBP"}:
                    raise AvatarInputError("image_format")
                width, height = image.size
                if (
                    width <= 0
                    or height <= 0
                    or max(width, height) > MAX_DIMENSION
                    or width * height > MAX_PIXELS
                ):
                    raise AvatarInputError("image_dimensions")
                if getattr(image, "is_animated", False) or getattr(image, "n_frames", 1) != 1:
                    raise AvatarInputError("image_animated")
                image.load()
                with ImageOps.exif_transpose(image) as oriented:
                    with oriented.convert("RGBA") as rgba:
                        with ImageOps.fit(
                            rgba, (AVATAR_SIZE, AVATAR_SIZE), method=Image.Resampling.LANCZOS
                        ) as thumbnail:
                            # A fresh image has no EXIF, PNG text, ICC, XMP or filename.
                            with Image.new("RGBA", thumbnail.size) as clean:
                                clean.paste(thumbnail)
                                output = io.BytesIO()
                                clean.save(output, format="PNG", optimize=True)
                                result = output.getvalue()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise AvatarInputError("image_dimensions") from None
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as error:
        if isinstance(error, AvatarInputError):
            raise
        raise AvatarInputError("image_invalid") from None
    if len(result) > MAX_STORED_BYTES:
        raise AvatarInputError("image_too_large")
    return result


def avatar_context(db, user_id: int) -> dict:
    """Return shell presentation facts without loading image bytes."""
    row = db.execute("SELECT revision FROM user_avatars WHERE user_id=?", (user_id,)).fetchone()
    return {
        "avatar_available": row is not None,
        "avatar_revision": row["revision"] if row else "",
        "display_name": read_secret(db, user_id, "display_name") or "",
    }


def get_avatar(db, user_id: int) -> Avatar | None:
    row = db.execute(
        "SELECT image_bytes,revision FROM user_avatars WHERE user_id=?", (user_id,)
    ).fetchone()
    return Avatar(bytes(row["image_bytes"]), row["revision"]) if row else None


def save_avatar(db, user_id: int, source: bytes) -> dict:
    """Replace only this user's picture after successful bounded decoding."""
    _guard()
    image = normalize_avatar(source)
    revision = hashlib.sha256(image).hexdigest()
    with db:
        db.execute(
            "INSERT INTO user_avatars(user_id,image_bytes,revision) VALUES (?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET image_bytes=excluded.image_bytes,revision=excluded.revision,updated_at=datetime('now')",
            (user_id, image, revision),
        )
    return avatar_context(db, user_id)


def remove_avatar(db, user_id: int) -> None:
    _guard()
    with db:
        db.execute("DELETE FROM user_avatars WHERE user_id=?", (user_id,))
