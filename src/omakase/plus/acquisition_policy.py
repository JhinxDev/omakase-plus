"""Validated, owner-editable torrent selection criteria for Omakase Plus."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from omakase.plus.secrets import read_secret

_RESOLUTIONS = ("2160p", "1080p", "720p")
_STORAGE_KEY = "acquisition_policy"
_FIELDS = {
    "allowed_resolutions",
    "minimum_seeders",
    "preferred_seeders",
    "max_release_size_gib",
    "allow_hardsub_fallback",
    "prefer_trusted",
    "prefer_batches",
    "schema_version",
}


@dataclass(frozen=True, slots=True)
class AcquisitionPolicy:
    """Small executable policy shared by selection, settings, and receipts."""

    allowed_resolutions: tuple[str, ...] = ("1080p", "2160p", "720p")
    minimum_seeders: int = 1
    preferred_seeders: int = 5
    max_release_size_gib: int = 150
    allow_hardsub_fallback: bool = True
    prefer_trusted: bool = True
    prefer_batches: bool = True
    schema_version: int = field(default=1, init=False)

    def __post_init__(self) -> None:
        resolutions = tuple(self.allowed_resolutions)
        if any(not isinstance(value, str) for value in resolutions):
            raise ValueError("Resolution choices are invalid.")
        if not resolutions or len(resolutions) != len(set(resolutions)):
            raise ValueError("Choose each resolution at most once.")
        if any(value not in _RESOLUTIONS for value in resolutions):
            raise ValueError("Choose only 2160p, 1080p, or 720p.")
        if any(
            type(value) is not int
            for value in (
                self.minimum_seeders,
                self.preferred_seeders,
                self.max_release_size_gib,
            )
        ):
            raise ValueError("Seeder and size limits must be whole numbers.")
        if any(
            type(value) is not bool
            for value in (
                self.allow_hardsub_fallback,
                self.prefer_trusted,
                self.prefer_batches,
            )
        ):
            raise ValueError("Download preference switches must be true or false.")
        if not 1 <= self.minimum_seeders <= 1_000:
            raise ValueError("Minimum seeders must be between 1 and 1,000.")
        if not self.minimum_seeders <= self.preferred_seeders <= 10_000:
            raise ValueError("Preferred seeders must be at least the minimum.")
        if not 1 <= self.max_release_size_gib <= 300:
            raise ValueError("Maximum release size must be between 1 and 300 GiB.")
        object.__setattr__(self, "allowed_resolutions", resolutions)

    @property
    def revision(self) -> str:
        payload = json.dumps(
            asdict(self),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @property
    def max_release_size_bytes(self) -> int:
        return self.max_release_size_gib * 1024**3

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    def settings_view(self) -> dict[str, Any]:
        seeder_label = "seeder" if self.minimum_seeders == 1 else "seeders"
        return {
            "revision": self.revision,
            "resolution_order": " → ".join(self.allowed_resolutions),
            "availability": (
                f"At least {self.minimum_seeders} {seeder_label}; "
                f"{self.preferred_seeders}+ preferred"
            ),
            "subtitles": (
                "Selectable subtitles preferred; clearly labelled burned-in fallback allowed"
                if self.allow_hardsub_fallback
                else "Selectable subtitles required"
            ),
            "source_safety": ("Reject RAW/no-subtitle, CAM, sample, trailer, and extras releases"),
            "maximum_size": f"{self.max_release_size_gib} GiB",
            "batch_behavior": (
                "Prefer complete season batches when coverage matches"
                if self.prefer_batches
                else "Prefer individual episode releases"
            ),
            "trusted_behavior": (
                "Prefer trusted and known release groups"
                if self.prefer_trusted
                else "Do not add a trusted-uploader preference"
            ),
        }


def policy_from_mapping(values: Mapping[str, object]) -> AcquisitionPolicy:
    """Parse a bounded persisted mapping without accepting unknown controls."""
    if any(key not in _FIELDS for key in values):
        raise ValueError("Download criteria contain an unknown setting.")
    normalized = dict(values)
    schema_version = normalized.pop("schema_version", 1)
    if schema_version != 1:
        raise ValueError("Download criteria use an unsupported version.")
    resolutions = normalized.get("allowed_resolutions")
    if isinstance(resolutions, list):
        normalized["allowed_resolutions"] = tuple(resolutions)
    elif resolutions is not None and not isinstance(resolutions, tuple):
        raise ValueError("Resolution choices are invalid.")
    try:
        return AcquisitionPolicy(**normalized)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Download criteria are invalid: {exc}") from exc


def policy_from_json(value: str) -> AcquisitionPolicy:
    """Return defaults for an empty value and reject malformed saved criteria."""
    if not value:
        return AcquisitionPolicy()
    if len(value) > 16_384:
        raise ValueError("Download criteria are too large.")
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("Download criteria are not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise ValueError("Download criteria must be a JSON object.")
    return policy_from_mapping(payload)


def load_user_acquisition_policy(db, user_id: int) -> AcquisitionPolicy:
    """Load one owner's encrypted policy, falling back only when none is saved."""
    if db is None:
        return AcquisitionPolicy()
    return policy_from_json(read_secret(db, user_id, _STORAGE_KEY) or "")


def policy_from_form(form: Mapping[str, object]) -> AcquisitionPolicy:
    """Parse the friendly Settings form into one validated policy."""
    resolutions = tuple(
        str(value).strip()
        for key in ("resolution_1", "resolution_2", "resolution_3")
        if (value := form.get(key, "")) and str(value).strip()
    )
    try:
        minimum_seeders = int(str(form.get("minimum_seeders", "")).strip())
        preferred_seeders = int(str(form.get("preferred_seeders", "")).strip())
        max_release_size_gib = int(str(form.get("max_release_size_gib", "")).strip())
    except ValueError as exc:
        raise ValueError("Seeder and size limits must be whole numbers.") from exc
    return AcquisitionPolicy(
        allowed_resolutions=resolutions,
        minimum_seeders=minimum_seeders,
        preferred_seeders=preferred_seeders,
        max_release_size_gib=max_release_size_gib,
        allow_hardsub_fallback=bool(form.get("allow_hardsub_fallback")),
        prefer_trusted=bool(form.get("prefer_trusted")),
        prefer_batches=bool(form.get("prefer_batches")),
    )


ACQUISITION_POLICY_STORAGE_KEY = _STORAGE_KEY
SUPPORTED_RESOLUTIONS = _RESOLUTIONS
