"""Portable per-user media connections and bounded, secret-free health receipts."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import math
import os
import re
import socket
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx
from cryptography.exceptions import InvalidTag

from omakase.plus.movies import MovieConnections, MovieError
from omakase.plus.outbound import (
    OutboundError,
    blocked_address,
    loopback_destinations_allowed,
    pin_destination,
)
from omakase.plus.prowlarr import private_catalog_urls_allowed
from omakase.plus.secrets import encrypt, read_secret

_MAX_BYTES = 256 * 1024
_DETAILS = {
    "untested": "Saved. Test this connection before using it.",
    "needs_configuration": "Add the server address and a key to connect.",
    "disconnected": "This service is not connected.",
    "connected": "The service accepted a read-only authenticated check.",
    "indexer_cooldowns": "Prowlarr is connected. Some indexers are temporarily unavailable.",
    "indexer_status_unavailable": "Prowlarr is connected. Individual indexer status could not be checked.",
    "authentication_failed": "The service rejected its saved key. Replace the key and test again.",
    "service_unavailable": "The service is unavailable. Check its address and try later.",
    "transport_error": "The service could not be reached. Check the address and network.",
    "transport_timeout": "The connection check timed out. Try again later.",
    "rate_limited": "The service requested a pause. Testing resumes after the displayed time.",
    "redirect_rejected": "The service redirected this check. Save its final server address instead.",
    "unsafe_destination": "This address is blocked by the deployment's network policy.",
    "invalid_response": "The service returned an unexpected response. Check its address and version.",
    "response_too_large": "The service response exceeded the connection check's size limit.",
}
_LABELS = {
    "connected": "Connected",
    "degraded": "Needs attention",
    "error": "Check failed",
    "rate_limited": "Paused",
    "configured": "Not tested",
    "incomplete": "Finish setup",
    "disconnected": "Not connected",
}


class ConnectionInputError(ValueError):
    """A safe configuration message that contains no submitted values."""


@dataclass(frozen=True, slots=True)
class _Integration:
    id: str
    name: str
    mark: str
    purpose: str
    url_key: str | None
    secret_key: str
    default_url: str = ""


_INTEGRATIONS = (
    _Integration(
        "realdebrid",
        "Real-Debrid",
        "RD",
        "Cloud downloading and cached media access for anime and movies.",
        None,
        "realdebrid_api_key",
        "https://api.real-debrid.com/rest/1.0",
    ),
    _Integration(
        "plex",
        "Plex",
        "PX",
        "Check your library and open verified movies in Plex.",
        "plex_url",
        "plex_token",
    ),
    _Integration(
        "riven",
        "Riven",
        "RV",
        "Request movies and follow acquisition progress.",
        "riven_url",
        "riven_api_key",
    ),
    _Integration(
        "seerr",
        "Seerr",
        "SE",
        "Find movies and resolve their exact metadata.",
        "seerr_url",
        "seerr_api_key",
    ),
    _Integration(
        "prowlarr",
        "Prowlarr",
        "PR",
        "Expand anime search and see individual source availability.",
        "prowlarr_url",
        "prowlarr_api_key",
    ),
)


def private_connections_enabled() -> bool:
    return os.getenv("OMAKASE_PLUS_PRIVATE", "false").strip().lower() == "true"


def _guard() -> None:
    if not private_connections_enabled():
        raise ConnectionInputError("Connections are available only in a private Plus deployment.")


def _spec(integration: str) -> _Integration:
    for spec in _INTEGRATIONS:
        if spec.id == integration:
            return spec
    raise ConnectionInputError("Unknown connection.")


def _read(db, user_id: int, key: str) -> str:
    try:
        return read_secret(db, user_id, key) or ""
    except (InvalidTag, ValueError):
        return ""


def _keys(db, user_id: int, spec: _Integration) -> tuple[str | None, str]:
    # Retain the exact existing pair. Never combine a new URL with a legacy key.
    if (
        spec.id == "seerr"
        and not any(_read(db, user_id, name) for name in ("seerr_url", "seerr_api_key"))
        and any(_read(db, user_id, name) for name in ("overseerr_url", "overseerr_api_key"))
    ):
        return "overseerr_url", "overseerr_api_key"
    return spec.url_key, spec.secret_key


def _values(db, user_id: int, spec: _Integration) -> tuple[str, str]:
    url_key, secret_key = _keys(db, user_id, spec)
    return (_read(db, user_id, url_key) if url_key else spec.default_url), _read(
        db, user_id, secret_key
    )


def _blocked(address, *, allow_private: bool) -> bool:
    return blocked_address(
        address, allow_private=allow_private, allow_loopback=loopback_destinations_allowed()
    )


def normalize_connection_url(value: str) -> str:
    """Validate a service base, preserving reverse-proxy subpaths without queries."""
    if not isinstance(value, str):
        raise ConnectionInputError("Enter an HTTP or HTTPS server address.")
    candidate = value.strip()
    if not candidate or len(candidate) > 1000 or any(ord(c) < 33 for c in candidate):
        raise ConnectionInputError("Enter a valid server address.")
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or (port is not None and not 0 < port < 65536)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or "?" in candidate
            or "#" in candidate
            or "\\" in candidate
            or "%" in candidate
            or "//" in parsed.path
            or any(part in {".", ".."} for part in parsed.path.split("/"))
        ):
            raise ValueError
        hostname = parsed.hostname.casefold().rstrip(".")
        if hostname in {
            "metadata",
            "metadata.google.internal",
            "instance-data",
            "instance-data.ec2.internal",
        }:
            raise ValueError
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            address = None
        if address is not None and _blocked(address, allow_private=private_catalog_urls_allowed()):
            raise ValueError
        normalized = str(httpx.URL(candidate.rstrip("/")))
    except (ValueError, httpx.InvalidURL):
        raise ConnectionInputError(
            "Enter a safe HTTP or HTTPS server address allowed by this deployment."
        ) from None
    # Service API suffixes are appended by the native adapters, not by the form.
    return normalized.removesuffix("/api/v1")


def _origin(value: str) -> tuple[str, str, int]:
    parsed = urlsplit(value)
    return (
        parsed.scheme,
        (parsed.hostname or "").casefold().rstrip("."),
        parsed.port or (443 if parsed.scheme == "https" else 80),
    )


def _fingerprint(url: str, key: str) -> str:
    return hashlib.sha256((url + "\0" + key).encode()).hexdigest()


def _health(
    state: str,
    code: str,
    *,
    checked_at=None,
    http_status=None,
    retry_after=None,
    retry_at=None,
    indexers=None,
    connected=False,
) -> dict:
    return {
        "state": state,
        "code": code,
        "detail": _DETAILS[code],
        "connected": connected,
        "checked_at": checked_at,
        "http_status": http_status,
        "retry_after": retry_after,
        "retry_at": retry_at,
        "indexers": indexers or [],
    }


def _saved_health(db, user_id: int, spec: _Integration, url: str, key: str) -> dict:
    row = db.execute(
        "SELECT fingerprint,health_json FROM user_connection_health WHERE user_id=? AND integration=?",
        (user_id, spec.id),
    ).fetchone()
    if row and row["fingerprint"] == _fingerprint(url, key):
        try:
            result = json.loads(row["health_json"])
            if result.get("code") in _DETAILS and result.get("state") in _LABELS:
                return result
        except (ValueError, TypeError):
            pass
    if not key and (not spec.url_key or not url):
        return _health("disconnected", "disconnected")
    if not url or not key:
        return _health("incomplete", "needs_configuration")
    return _health("configured", "untested")


def connection_cards(db, user_id: int) -> list[dict]:
    """Return only presentation facts. Neither saved addresses nor keys leave here."""
    cards = []
    for spec in _INTEGRATIONS:
        url, key = _values(db, user_id, spec)
        health = _saved_health(db, user_id, spec, url, key)
        cards.append(
            {
                "id": spec.id,
                "name": spec.name,
                "mark": spec.mark,
                "purpose": spec.purpose,
                "has_url": spec.url_key is not None,
                "url_saved": bool(url) if spec.url_key else False,
                "key_saved": bool(key),
                "health": health,
                "label": _LABELS[health["state"]],
            }
        )
    return cards


def save_connection(
    db, user_id: int, integration: str, *, url: str = "", api_key: str = ""
) -> dict:
    """Save one pair atomically; empty fields retain saved values and invalidate proof."""
    _guard()
    spec = _spec(integration)
    if not isinstance(url, str) or not isinstance(api_key, str):
        raise ConnectionInputError("Enter a server address and key as text.")
    url, api_key = url.strip(), api_key.strip()
    if api_key and (len(api_key) > 512 or any(ord(c) < 33 or ord(c) > 126 for c in api_key)):
        raise ConnectionInputError("Enter a valid key without spaces or control characters.")
    previous_url, previous_key = _values(db, user_id, spec)
    if not spec.url_key and url:
        raise ConnectionInputError("This provider uses its fixed official API address.")
    next_url = normalize_connection_url(url or previous_url) if spec.url_key else spec.default_url
    if (
        previous_key
        and not api_key
        and (not previous_url or _origin(next_url) != _origin(previous_url))
    ):
        raise ConnectionInputError("A different server origin requires a replacement key.")
    next_key = api_key or previous_key
    if not next_key:
        raise ConnectionInputError("Enter a key to connect this service.")
    url_key, secret_key = _keys(db, user_id, spec)
    values = [(secret_key, next_key)]
    if url_key:
        values.append((url_key, next_url))
    with db:
        if integration == "plex" and previous_url and _origin(previous_url) != _origin(next_url):
            db.executemany(
                "DELETE FROM user_secrets WHERE user_id=? AND key_name=?",
                [(user_id, name) for name in ("plex_movies_section_id", "plex_machine_id")],
            )
        db.executemany(
            "INSERT INTO user_secrets(user_id,key_name,encrypted_value) VALUES (?,?,?) "
            "ON CONFLICT(user_id,key_name) DO UPDATE SET encrypted_value=excluded.encrypted_value,created_at=datetime('now')",
            [(user_id, name, encrypt(value)) for name, value in values],
        )
        if next_url != previous_url or next_key != previous_key:
            from omakase.plus import households

            if integration == "plex":
                from omakase.plus.playback_proof import invalidate_personal_playback

                invalidate_personal_playback(db, user_id=user_id)
            if households.feature_enabled():
                households.connection_generation_changed(
                    db, owner_user_id=user_id, integration=integration
                )
            db.execute(
                "DELETE FROM user_connection_health WHERE user_id=? AND integration=?",
                (user_id, spec.id),
            )
    return next(card for card in connection_cards(db, user_id) if card["id"] == integration)


def disconnect_connection(db, user_id: int, integration: str) -> None:
    """Forget only this integration. Anime source filters and other accounts remain."""
    _guard()
    spec = _spec(integration)
    names = [spec.secret_key]
    if spec.url_key:
        names.append(spec.url_key)
    if integration == "seerr":
        names.extend(("overseerr_url", "overseerr_api_key"))
    if integration == "plex":
        names.extend(("plex_movies_section_id", "plex_machine_id"))
    changed = (
        db.execute(
            "SELECT 1 FROM user_secrets WHERE user_id=? AND key_name IN ("
            + ",".join("?" for _ in names)
            + ") LIMIT 1",
            (user_id, *names),
        ).fetchone()
        is not None
    )
    with db:
        if changed:
            from omakase.plus import households

            if integration == "plex":
                from omakase.plus.playback_proof import invalidate_personal_playback

                invalidate_personal_playback(db, user_id=user_id)
            if households.feature_enabled():
                households.connection_generation_changed(
                    db, owner_user_id=user_id, integration=integration
                )
        db.executemany(
            "DELETE FROM user_secrets WHERE user_id=? AND key_name=?",
            [(user_id, name) for name in names],
        )
        db.execute(
            "DELETE FROM user_connection_health WHERE user_id=? AND integration=?",
            (user_id, integration),
        )


async def _resolved_ips(hostname: str, port: int) -> tuple:
    rows = await asyncio.wait_for(
        asyncio.to_thread(socket.getaddrinfo, hostname, port, type=socket.SOCK_STREAM), timeout=3
    )
    return tuple(
        sorted({ipaddress.ip_address(row[4][0].split("%", 1)[0]) for row in rows}, key=str)
    )


class _CheckError(Exception):
    def __init__(self, code: str, status: int | None = None, retry_after: int | None = None):
        self.code, self.status, self.retry_after = code, status, retry_after


def _retry_seconds(value: str | None, now: datetime) -> int:
    try:
        if value and re.fullmatch(r"[0-9]{1,9}", value.strip()):
            return max(1, int(value))
        date = parsedate_to_datetime(value or "")
        if date.tzinfo is not None:
            return max(1, math.ceil((date - now).total_seconds()))
    except (ValueError, TypeError, OverflowError):
        pass
    return 60


async def _fetch(
    client, base: str, path: str, headers: dict, *, hostname: str, now: datetime, params=None
) -> bytes:
    async with client.stream(
        "GET", base + path, headers=headers, params=params, extensions={"sni_hostname": hostname}
    ) as response:
        status = response.status_code
        if status == 429:
            raise _CheckError(
                "rate_limited", status, _retry_seconds(response.headers.get("Retry-After"), now)
            )
        if status in {401, 403}:
            raise _CheckError("authentication_failed", status)
        if 300 <= status < 400:
            raise _CheckError("redirect_rejected", status)
        if status != 200:
            raise _CheckError("service_unavailable", status)
        body = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=8192):
            body.extend(chunk)
            if len(body) > _MAX_BYTES:
                raise _CheckError("response_too_large", status)
        return bytes(body)


def _json(body: bytes):
    try:
        return json.loads(body)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise _CheckError("invalid_response") from None


def _iso(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _date(value) -> datetime | None:
    if not isinstance(value, str) or len(value) > 50:
        return None
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return date.astimezone(timezone.utc) if date.tzinfo else None
    except ValueError:
        return None


def _indexers(indexers, statuses, key: str, now: datetime) -> list[dict]:
    if (
        not isinstance(indexers, list)
        or not isinstance(statuses, list)
        or len(indexers) > 100
        or len(statuses) > 100
    ):
        raise _CheckError("invalid_response")
    cooldowns = {}
    for status in statuses:
        if not isinstance(status, dict) or type(status.get("indexerId")) is not int:
            raise _CheckError("invalid_response")
        until = _date(status.get("disabledTill"))
        if status.get("disabledTill") and until is None:
            raise _CheckError("invalid_response")
        if until and until > now:
            cooldowns[status["indexerId"]] = _iso(until)
    result = []
    for row in indexers:
        if not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] <= 0:
            raise _CheckError("invalid_response")
        name = row.get("name", "")
        if (
            not isinstance(name, str)
            or not 1 <= len(name) <= 80
            or any(ord(c) < 32 for c in name)
            or key in name
            or re.search(r"https?://", name)
        ):
            name = f"Indexer {row['id']}"
        until = cooldowns.get(row["id"])
        result.append(
            {
                "id": row["id"],
                "name": name,
                "state": "disabled"
                if row.get("enable") is False
                else ("cooldown" if until else "available"),
                "disabled_till": until,
            }
        )
    return result


async def _native_check(
    spec: _Integration, url: str, key: str, *, transport, now: datetime
) -> dict:
    url = normalize_connection_url(url)
    try:
        destination = await pin_destination(url, transport=transport, resolver=_resolved_ips)
    except OutboundError as error:
        code = (
            error.code
            if error.code in {"unsafe_destination", "transport_error", "transport_timeout"}
            else "unsafe_destination"
        )
        raise _CheckError(code) from None
    pinned = destination.base_url
    headers = {"Accept": "application/json", "Host": destination.host_header}
    if spec.id == "realdebrid":
        headers["Authorization"] = "Bearer " + key
    elif spec.id == "plex":
        headers.update(
            {
                "X-Plex-Token": key,
                "Accept": "application/xml",
                "X-Plex-Product": "Omakase Plus",
                "X-Plex-Client-Identifier": "omakase-plus-connections",
            }
        )
    else:
        headers["X-Api-Key"] = key
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(5, connect=3),
        follow_redirects=False,
        trust_env=False,
        transport=transport,
    ) as client:

        async def fetch(path, **kwargs):
            return await _fetch(
                client, pinned, path, headers, hostname=destination.sni_hostname, now=now, **kwargs
            )

        if spec.id == "plex":
            body = await fetch("/library/sections")
            identity_body = await fetch("/identity")
            try:
                if b"\x00" in body or b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
                    raise ET.ParseError
                root = ET.fromstring(body)
                if root.tag != "MediaContainer":
                    raise ET.ParseError
                if (
                    any(marker in identity_body.upper() for marker in (b"<!DOCTYPE", b"<!ENTITY"))
                    or b"\x00" in identity_body
                ):
                    raise ET.ParseError
                identity = ET.fromstring(identity_body)
                machine = identity.get("machineIdentifier", "")
                if identity.tag != "MediaContainer" or not re.fullmatch(
                    r"[A-Za-z0-9_-]{1,128}", machine
                ):
                    raise ET.ParseError
            except (ET.ParseError, ValueError):
                raise _CheckError("invalid_response") from None
            return {
                **_health("connected", "connected", connected=True),
                "_verified_machine": machine,
            }
        elif spec.id == "prowlarr":
            status = _json(await fetch("/api/v1/system/status"))
            if not isinstance(status, dict) or not isinstance(status.get("version"), str):
                raise _CheckError("invalid_response")
            try:
                indexers = _indexers(
                    _json(await fetch("/api/v1/indexer")),
                    _json(await fetch("/api/v1/indexerstatus")),
                    key,
                    now,
                )
            except _CheckError as error:
                if error.code == "rate_limited":
                    raise
                return _health("degraded", "indexer_status_unavailable", connected=True)
            degraded = any(row["state"] != "available" for row in indexers)
            return _health(
                "degraded" if degraded else "connected",
                "indexer_cooldowns" if degraded else "connected",
                indexers=indexers,
                connected=True,
            )
        else:
            path = {"realdebrid": "/user", "seerr": "/api/v1/auth/me", "riven": "/api/v1/items"}[
                spec.id
            ]
            payload = _json(
                await fetch(
                    path, params={"limit": 1, "type": "movie"} if spec.id == "riven" else None
                )
            )
            if not isinstance(payload, dict):
                raise _CheckError("invalid_response")
            if spec.id == "riven":
                if not isinstance(payload.get("items"), list) or len(payload["items"]) > 100:
                    raise _CheckError("invalid_response")
            elif type(payload.get("id")) is not int or payload["id"] <= 0:
                raise _CheckError("invalid_response")
        return _health("connected", "connected", connected=True)


async def test_connection(
    db, user_id: int, integration: str, *, transport=None, now: datetime | None = None
) -> dict:
    """Check a saved native endpoint only. Never request media or follow redirects."""
    _guard()
    spec = _spec(integration)
    url, key = _values(db, user_id, spec)
    previous = _saved_health(db, user_id, spec, url, key)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    retry_at = _date(previous.get("retry_at"))
    if retry_at and retry_at > now:
        return previous
    if not url or not key:
        raise ConnectionInputError("Save this service's address and key before testing.")
    try:
        result = await asyncio.wait_for(
            _native_check(spec, url, key, transport=transport, now=now), timeout=15
        )
    except ConnectionInputError:
        result = _health("error", "unsafe_destination")
    except _CheckError as error:
        limited = error.code == "rate_limited"
        result = _health(
            "rate_limited" if limited else "error",
            error.code,
            http_status=error.status,
            retry_after=error.retry_after,
            retry_at=_iso(
                datetime.fromtimestamp(math.ceil(now.timestamp() + error.retry_after), timezone.utc)
            )
            if limited
            else None,
        )
    except (TimeoutError, asyncio.TimeoutError, httpx.TimeoutException):
        result = _health("error", "transport_timeout")
    except (httpx.HTTPError, OSError):
        result = _health("error", "transport_error")
    machine = result.pop("_verified_machine", None)
    # A successful response describes only the exact pair used before awaiting.
    # Never attach its identity or health to credentials replaced in another request.
    result["checked_at"] = _iso(now)
    with db:
        if not db.in_transaction:
            db.execute("BEGIN IMMEDIATE")
        if _values(db, user_id, spec) != (url, key):
            return _health("configured", "untested")
        if machine is not None:
            previous_machine = _read(db, user_id, "plex_machine_id")
            if previous_machine and previous_machine != machine:
                from omakase.plus import households
                from omakase.plus.playback_proof import invalidate_personal_playback

                invalidate_personal_playback(db, user_id=user_id)
                if households.feature_enabled():
                    households.connection_generation_changed(
                        db, owner_user_id=user_id, integration="plex"
                    )
                db.execute("DELETE FROM movie_verifications WHERE user_id=?", (user_id,))
            db.execute(
                "INSERT INTO user_secrets(user_id,key_name,encrypted_value) VALUES(?,?,?) "
                "ON CONFLICT(user_id,key_name) DO UPDATE SET encrypted_value=excluded.encrypted_value,created_at=datetime('now')",
                (user_id, "plex_machine_id", encrypt(machine)),
            )
        db.execute(
            "INSERT INTO user_connection_health(user_id,integration,fingerprint,health_json,checked_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(user_id,integration) DO UPDATE SET fingerprint=excluded.fingerprint,health_json=excluded.health_json,checked_at=excluded.checked_at",
            (
                user_id,
                integration,
                _fingerprint(url, key),
                json.dumps(result, separators=(",", ":")),
                result["checked_at"],
            ),
        )
    return result


def movie_connections(db, user_id: int) -> MovieConnections:
    """Build the movie backend from this user's exact saved and tested pairs."""
    values = {}
    passed = private_connections_enabled()
    for integration in ("riven", "seerr", "plex"):
        spec = _spec(integration)
        url, key = _values(db, user_id, spec)
        health = _saved_health(db, user_id, spec, url, key)
        try:
            if url:
                url = normalize_connection_url(url)
        except ConnectionInputError:
            return MovieConnections()
        values[integration + "_url"] = url
        values["plex_token" if integration == "plex" else integration + "_api_key"] = key
        passed = passed and bool(url and key) and health["state"] == "connected"
    try:
        raw_section = _read(db, user_id, "plex_movies_section_id")
        section = int(raw_section) if re.fullmatch(r"[1-9][0-9]{0,8}", raw_section) else None
        machine = _read(db, user_id, "plex_machine_id")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", machine):
            machine = ""
        return MovieConnections(
            enabled=bool(passed), plex_movies_section_id=section, plex_machine_id=machine, **values
        )
    except MovieError:
        return MovieConnections()
