"""Explicit notification-only public release metadata checks for Plus hosts."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime

import httpx

PRODUCT_VERSION = "2026.10.28.4"
FEED_URL = "https://jhinx.dev/updates/omakase-plus.json"
NOTES_URL = "https://jhinx.dev/work/omakase-plus"
MAX_BYTES = 32 * 1024
MIN_INTERVAL = 15 * 60
MAX_RETRY_AFTER = 24 * 60 * 60


def checks_enabled() -> bool:
    return os.getenv("OMAKASE_UPDATE_CHECKS", "true").strip().lower() != "false"


def version_key(value: str) -> tuple[int, int, int, int]:
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{4}\.[0-9]{2}\.[0-9]{2}\.[1-9][0-9]{0,5}", value
    ):
        raise ValueError("Invalid release version")
    year, month, day, revision = map(int, value.split("."))
    date(year, month, day)
    return year, month, day, revision


@dataclass(frozen=True)
class Release:
    version: str
    released_at: str
    summary: str
    notes_url: str

    @property
    def newer(self) -> bool:
        return version_key(self.version) > version_key(PRODUCT_VERSION)


def parse_release(body: bytes) -> Release:
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate metadata field")
            result[key] = value
        return result

    data = json.loads(body.decode("utf-8"), object_pairs_hook=unique_object)
    if not isinstance(data, dict) or set(data) != {
        "schema_version",
        "product",
        "channel",
        "version",
        "released_at",
        "notes_url",
        "summary",
    }:
        raise ValueError("Invalid release metadata")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ValueError("Unsupported release metadata")
    if data["product"] != "omakase-plus" or data["channel"] != "stable":
        raise ValueError("Wrong product or channel")
    version_key(data["version"])
    stamp = data["released_at"]
    if not isinstance(stamp, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", stamp
    ):
        raise ValueError("Invalid release timestamp")
    datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
    summary = data["summary"]
    if (
        not isinstance(summary, str)
        or not 1 <= len(summary.strip()) <= 1000
        or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in summary)
    ):
        raise ValueError("Invalid release summary")
    if data["notes_url"] != NOTES_URL:
        raise ValueError("Invalid release notes URL")
    return Release(data["version"], stamp, summary.strip(), NOTES_URL)


def retry_seconds(value: str) -> int:
    try:
        seconds = (
            int(value)
            if value.isascii() and value.isdigit()
            else math.ceil((parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
        )
    except (ValueError, TypeError, OverflowError):
        return MIN_INTERVAL
    return min(MAX_RETRY_AFTER, max(MIN_INTERVAL, seconds))


@dataclass(frozen=True)
class UpdateStatus:
    release: Release | None = None
    checked_at: str = ""
    error: str = ""


class UpdateChecker:
    """Process-local cache shared by host accounts; never receives user data."""

    def __init__(self):
        self.status = UpdateStatus()
        self.next_check = 0.0
        self._lock = asyncio.Lock()

    def retry_after(self) -> int:
        return max(0, math.ceil(self.next_check - time.monotonic()))

    async def check(self) -> tuple[UpdateStatus, int]:
        if not checks_enabled():
            return self.status, 0
        async with self._lock:
            if remaining := self.retry_after():
                return self.status, remaining
            delay = MIN_INTERVAL
            release = self.status.release
            error = ""
            try:
                # Fixed URL, no proxy, auth, cookies, account or installed version.
                async with asyncio.timeout(10):
                    async with httpx.AsyncClient(
                        timeout=5.0,
                        follow_redirects=False,
                        trust_env=False,
                        headers={
                            "Accept": "application/json",
                            "Accept-Encoding": "identity",
                            "User-Agent": "Omakase-Plus-Update-Check",
                        },
                    ) as client:
                        async with client.stream("GET", FEED_URL) as response:
                            if response.status_code in {429, 503}:
                                delay = retry_seconds(response.headers.get("retry-after", ""))
                            if response.status_code != 200:
                                raise ValueError("Release endpoint unavailable")
                            if (
                                response.headers.get("content-encoding", "identity").lower()
                                != "identity"
                            ):
                                raise ValueError("Unsupported release encoding")
                            if (
                                response.headers.get("content-type", "")
                                .split(";", 1)[0]
                                .strip()
                                .lower()
                                != "application/json"
                            ):
                                raise ValueError("Invalid release response")
                            body = bytearray()
                            async for chunk in response.aiter_bytes(chunk_size=4096):
                                if len(body) + len(chunk) > MAX_BYTES:
                                    raise ValueError("Release response too large")
                                body.extend(chunk)
                            release = parse_release(bytes(body))
            except (httpx.HTTPError, ValueError, TypeError, RecursionError, TimeoutError):
                error = "Update check unavailable. Your installed version is unchanged. Try again later."
            self.status = UpdateStatus(
                release, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), error
            )
            self.next_check = time.monotonic() + delay
            return self.status, 0


checker = UpdateChecker()
