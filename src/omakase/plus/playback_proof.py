"""Bounded personal-Plex readback and immutable movie proof chains.

Only explicit async verification performs HTTP. Selection/cache reads are SQLite
plus current saved configuration; no tokens, provider URLs or paths are returned.
"""

from __future__ import annotations

import json
import re
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from omakase.plus import connections
from omakase.plus.households import resolve_scope
from omakase.plus.language_source import digest, refresh_source_projection, require_current_source
from omakase.plus.outbound import pin_destination


class PlaybackProofError(ValueError):
    pass


def _require(condition, message="Playback proof is unavailable or changed."):
    if not condition:
        raise PlaybackProofError(message)


def _now(now=None):
    value = now or datetime.now(timezone.utc)
    _require(isinstance(value, datetime) and value.tzinfo is not None)
    return value.astimezone(timezone.utc)


def _iso(value):
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _valid_until(value, now):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) > now
    except (ValueError, TypeError, AttributeError):
        return False


async def _get(base, path, token, *, transport=None, byte_range=False, expected_size=None):
    _require(bool(token))
    destination = await pin_destination(base, transport=transport)
    headers = {
        "X-Plex-Token": token,
        "X-Plex-Product": "Omakase Plus",
        "X-Plex-Client-Identifier": "omakase-plus-language-proof",
        "Accept": "application/json" if base == "https://plex.tv" else "application/xml",
        "Accept-Encoding": "identity",
        "Host": destination.host_header,
    }
    if byte_range:
        headers["Range"] = "bytes=0-65535"
    try:
        async with httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(10, connect=5),
            trust_env=False,
            follow_redirects=False,
        ) as client:
            async with client.stream(
                "GET",
                destination.base_url + path,
                headers=headers,
                extensions={"sni_hostname": destination.sni_hostname},
            ) as response:
                _require(
                    response.status_code == (206 if byte_range else 200),
                    "Plex did not confirm authenticated access.",
                )
                _require(
                    response.headers.get("content-encoding", "identity").lower() in {"identity", ""}
                )
                if byte_range:
                    match = re.fullmatch(
                        r"bytes 0-65535/([0-9]+)", response.headers.get("content-range", "")
                    )
                    _require(
                        match is not None
                        and int(match[1]) >= 65536
                        and (expected_size is None or int(match[1]) == expected_size),
                        "Plex did not return the exact requested byte range.",
                    )
                body = bytearray()
                limit = 65536 if byte_range else 256 * 1024
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    _require(
                        len(body) + len(chunk) <= limit,
                        "Plex response exceeded the proof size limit.",
                    )
                    body.extend(chunk)
                if byte_range:
                    _require(len(body) == 65536, "Plex returned an incomplete byte range.")
                return bytes(body)
    except httpx.HTTPError:
        raise PlaybackProofError("Plex readback could not be confirmed.") from None


def _xml(body):
    _require(
        b"<!DOCTYPE" not in body.upper() and b"<!ENTITY" not in body.upper() and b"\x00" not in body
    )
    try:
        return ET.fromstring(body)
    except ET.ParseError:
        raise PlaybackProofError("Plex metadata could not be confirmed.") from None


async def _account(token, transport):
    body = await _get("https://plex.tv", "/api/v2/user", token, transport=transport)
    try:
        account = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        raise PlaybackProofError("Plex account identity could not be confirmed.") from None
    identifier = account.get("id") if isinstance(account, dict) else None
    _require(
        type(identifier) in {str, int} and bool(re.fullmatch(r"[1-9][0-9]{0,19}", str(identifier))),
        "Plex account identity could not be confirmed.",
    )
    return str(identifier)


def _resource(db, scope, resource_id):
    resource = db.execute("SELECT * FROM library_resources WHERE id=?", (resource_id,)).fetchone()
    _require(
        resource is not None
        and resource["domain"] == "movie"
        and resource["owner_user_id"] == scope.operational_owner_id
    )
    _require(
        db.execute(
            "SELECT 1 FROM movie_catalog WHERE user_id=? AND imdb_id=? AND identity_verified=1",
            (resource["owner_user_id"], resource["canonical_id"]),
        ).fetchone()
        is not None
    )
    _require(
        db.execute(
            "SELECT 1 FROM library_items WHERE library_id=? AND resource_id=? AND state='active'",
            (scope.library_id, resource_id),
        ).fetchone()
        is not None
    )
    return resource


async def _personal_binding(db, *, actor_user_id, scope, resource_id, transport=None, now=None):
    _require(scope.viewer_id == actor_user_id)
    resource = _resource(db, scope, resource_id)
    source = require_current_source(db, scope=scope, resource=resource)
    base, owner_token = connections._values(
        db, resource["owner_user_id"], connections._spec("plex")
    )
    token = connections._read(db, actor_user_id, "plex_token")
    _require(
        bool(token) and (actor_user_id == resource["owner_user_id"] or token != owner_token),
        "A separately authenticated personal Plex connection is required.",
    )
    account = await _account(token, transport)
    if actor_user_id != resource["owner_user_id"]:
        _require(
            account != await _account(owner_token, transport),
            "The owner's Plex identity cannot represent another viewer.",
        )
    identity = _xml(await _get(base, "/identity", token, transport=transport))
    server = identity.get("machineIdentifier", "")
    _require(
        bool(re.fullmatch(r"[A-Za-z0-9_-]{1,128}", server))
        and server == connections._read(db, resource["owner_user_id"], "plex_machine_id"),
        "Plex server identity changed or is unconfirmed.",
    )
    sections = _xml(await _get(base, "/library/sections", token, transport=transport))
    configured_section = connections._read(db, resource["owner_user_id"], "plex_movies_section_id")
    _require(
        len(
            [
                r
                for r in sections.findall("Directory")
                if r.get("key") == configured_section
                and r.get("type") == "movie"
                and r.get("title") == "Movies"
            ]
        )
        == 1,
        "This personal Plex identity cannot access the configured movie library.",
    )
    _require(
        db.execute(
            "SELECT 1 FROM viewer_playback_bindings WHERE external_account_id=? AND server_id=? AND actor_user_id!=?",
            (account, server, actor_user_id),
        ).fetchone()
        is None,
        "This external identity is already bound to a different local viewer.",
    )
    current = require_current_source(db, scope=scope, resource=resource)
    _require(current == source and token == connections._read(db, actor_user_id, "plex_token"))
    clock = _now(now)
    return (
        actor_user_id,
        account,
        digest(token),
        server,
        source.source_generation,
        _iso(clock),
        _iso(clock + timedelta(minutes=15)),
    )


def invalidate_personal_playback(db, *, user_id):
    """Connection writer hook; preserves immutable facts, safe before034 exists."""
    if db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='viewer_current_playback_credentials'"
    ).fetchone():
        db.execute(
            "DELETE FROM viewer_current_playback_credentials WHERE actor_user_id=?", (user_id,)
        )


def _insert_binding(db, values):
    db.execute(
        "INSERT INTO viewer_current_playback_credentials(actor_user_id,server_id,credential_generation) VALUES(?,?,?) ON CONFLICT(actor_user_id,server_id) DO UPDATE SET credential_generation=excluded.credential_generation",
        (values[0], values[3], values[2]),
    )
    return db.execute(
        "INSERT INTO viewer_playback_bindings(actor_user_id,external_account_id,credential_generation,server_id,source_generation,verified_at,expires_at) VALUES(?,?,?,?,?,?,?)",
        values,
    ).lastrowid


async def verify_personal_plex_binding(db, **kwargs):
    _require(
        not db.in_transaction, "Commit request preparation before authenticated HTTP verification."
    )
    return _insert_binding(db, await _personal_binding(db, **kwargs))


@dataclass(frozen=True, slots=True)
class PlaybackSelection:
    observation_id: int
    actor_user_id: int
    file_fingerprint: str
    selected_audio_id: str | None
    selected_subtitle_id: str | None


def resolve_selection(db, *, actor_user_id, request_id, observation_id, selection_id, now=None):
    from omakase.plus.language_store import load_request

    snapshot, _, _ = load_request(db, actor_user_id=actor_user_id, request_id=request_id)
    scope = resolve_scope(db, viewer_id=actor_user_id, library_id=snapshot.library_id)
    binding = db.execute(
        "SELECT resource_id FROM language_request_bindings WHERE request_id=?", (request_id,)
    ).fetchone()
    _require(binding is not None)
    resource = _resource(db, scope, binding[0])
    current = require_current_source(db, scope=scope, resource=resource)
    row = db.execute(
        "SELECT s.*, b.actor_user_id,b.credential_generation,b.server_id,b.source_generation AS binding_generation,b.expires_at AS binding_expires, f.resource_id,f.imdb_id,f.server_id AS file_server,f.source_generation,f.physical_fingerprint,f.language_observation_id,f.legacy_fingerprint,f.rating_key,f.expires_at AS file_expires, y.file_observation_id AS byte_file,y.http_status,y.bytes_read,y.expires_at AS byte_expires FROM viewer_selection_observations s JOIN viewer_playback_bindings b ON b.id=s.binding_id JOIN movie_file_observations f ON f.id=s.file_observation_id JOIN movie_byte_observations y ON y.id=s.byte_observation_id WHERE s.id=?",
        (selection_id,),
    ).fetchone()
    clock = _now(now)
    policy = db.execute(
        "SELECT membership_revision,library_policy_revision FROM language_request_policies WHERE id=?",
        (request_id,),
    ).fetchone()
    _require(
        policy[0] == scope.membership_revision and policy[1] == scope.policy_revision,
        "Request authorization changed; resolve a fresh request.",
    )
    _require(row is not None)
    current_check = db.execute(
        "SELECT selection_id FROM viewer_current_playback_checks WHERE actor_user_id=? AND resource_id=?",
        (actor_user_id, resource["id"]),
    ).fetchone()
    _require(
        current_check is not None and current_check[0] == selection_id,
        "The latest explicit playback check is not confirmed; verify it again.",
    )
    _require(
        db.execute(
            "SELECT 1 FROM viewer_selection_observations newer "
            "JOIN viewer_playback_bindings nb ON nb.id=newer.binding_id "
            "JOIN movie_file_observations nf ON nf.id=newer.file_observation_id "
            "WHERE newer.id>? AND nb.actor_user_id=? AND nb.server_id=? AND nf.resource_id=? LIMIT 1",
            (row["id"], actor_user_id, row["server_id"], resource["id"]),
        ).fetchone()
        is None,
        "A newer personal playback observation supersedes this proof.",
    )
    credential = db.execute(
        "SELECT credential_generation FROM viewer_current_playback_credentials WHERE actor_user_id=? AND server_id=?",
        (actor_user_id, row["server_id"]),
    ).fetchone()
    _require(
        credential is not None and credential[0] == row["credential_generation"],
        "Personal playback connection changed; verify it again.",
    )
    _require(
        row is not None
        and row["actor_user_id"] == actor_user_id
        and row["resource_id"] == resource["id"]
        and row["imdb_id"] == resource["canonical_id"]
        and row["language_observation_id"] == observation_id
        and row["byte_file"] == row["file_observation_id"]
        and row["server_id"] == row["file_server"]
        and row["source_generation"] == row["binding_generation"] == current.source_generation
        and row["credential_generation"]
        == digest(connections._read(db, actor_user_id, "plex_token"))
        and row["server_id"] == connections._read(db, resource["owner_user_id"], "plex_machine_id")
        and row["http_status"] == 206
        and row["bytes_read"] == 65536
    )
    _require(
        all(
            _valid_until(row[key], clock)
            for key in ("expires_at", "binding_expires", "file_expires", "byte_expires")
        ),
        "Playback evidence expired; verify it again.",
    )
    return PlaybackSelection(
        row["id"],
        actor_user_id,
        row["physical_fingerprint"],
        row["selected_audio_id"],
        row["selected_subtitle_id"],
    )


def _numeric(value):
    _require(isinstance(value, str) and bool(re.fullmatch(r"[1-9][0-9]{0,19}", value)))
    return value


async def observe_movie_playback(
    db, *, actor_user_id, request_id, rating_key, transport=None, now=None
):
    from omakase.plus.language_policy import LanguageEvidence, TrackObservation
    from omakase.plus.language_store import _transaction, load_request, record_observation
    from omakase.plus.movies import MovieService

    _require(
        not db.in_transaction, "Commit request preparation before authenticated HTTP verification."
    )
    snapshot, _, _ = load_request(db, actor_user_id=actor_user_id, request_id=request_id)
    _require(snapshot.domain == "movie")
    scope = resolve_scope(db, viewer_id=actor_user_id, library_id=snapshot.library_id)
    bound = db.execute(
        "SELECT resource_id FROM language_request_bindings WHERE request_id=?", (request_id,)
    ).fetchone()
    _require(bound is not None)
    resource = _resource(db, scope, bound[0])
    source = require_current_source(db, scope=scope, resource=resource)
    key = _numeric(rating_key)
    clock = _now(now)
    # Persist invalidation before HTTP. Failure cannot resurrect historical Ready.
    nonce = uuid.uuid4().hex
    with _transaction(db):
        db.execute(
            "INSERT INTO viewer_current_playback_checks(actor_user_id,resource_id,nonce,selection_id,started_at) VALUES(?,?,?,NULL,?) "
            "ON CONFLICT(actor_user_id,resource_id) DO UPDATE SET nonce=excluded.nonce,selection_id=NULL,started_at=excluded.started_at",
            (actor_user_id, resource["id"], nonce, _iso(clock)),
        )
    # Positive proof rows are written together only after all bounded reads pass.
    personal_binding = await _personal_binding(
        db,
        actor_user_id=actor_user_id,
        scope=scope,
        resource_id=resource["id"],
        transport=transport,
        now=clock,
    )
    token = connections._read(db, actor_user_id, "plex_token")
    base, _ = connections._values(db, resource["owner_user_id"], connections._spec("plex"))

    async def metadata():
        root = _xml(
            await _get(
                base, "/library/metadata/" + key + "?includeGuids=1", token, transport=transport
            )
        )
        videos = root.findall("Video")
        _require(len(videos) == 1)
        return videos[0]

    video = await metadata()
    section = connections._read(db, resource["owner_user_id"], "plex_movies_section_id")
    _numeric(section)
    _require(
        video.get("librarySectionID") == section
        and video.get("ratingKey") == key
        and video.get("type") == "movie"
    )
    _require(
        {g.get("id") for g in video.findall("Guid") if (g.get("id") or "").startswith("imdb://")}
        == {"imdb://" + resource["canonical_id"]}
    )
    medias = video.findall("Media")
    _require(
        len(medias) == 1, "Multiple Plex media versions need an explicit qualified version adapter."
    )
    media = medias[0]
    parts = media.findall("Part")
    _require(len(parts) == 1)
    part = parts[0]
    for element in (media, part):
        _numeric(element.get("id", ""))
    part_key = part.get("key", "")
    _require(
        bool(
            re.fullmatch(
                r"/library/parts/"
                + re.escape(part.get("id"))
                + r"/(?:[A-Za-z0-9_~.-]+/){0,2}[A-Za-z0-9_~.-]+",
                part_key,
            )
        )
        and not any(p in {".", ".."} for p in part_key.split("/"))
    )
    streams = part.findall("Stream")
    _require(len(streams) <= 128)
    # Physical identity excludes viewer selection/default flags and token identity.
    physical = digest(
        {
            "resource": resource["id"],
            "server": connections._read(db, resource["owner_user_id"], "plex_machine_id"),
            "section": section,
            "key": key,
            "media": {k: v for k, v in media.attrib.items() if k not in {"selected", "default"}},
            "part": {k: v for k, v in part.attrib.items() if k not in {"selected", "default"}},
            "streams": [
                {k: v for k, v in s.attrib.items() if k not in {"selected", "default"}}
                for s in streams
            ],
        }
    )
    tracks = []
    selected_audio = []
    selected_subtitle = []
    for stream in streams:
        kind = {"2": "audio", "3": "subtitle"}.get(stream.get("streamType"))
        if not kind:
            continue
        stream_id = _numeric(stream.get("id", ""))
        title = (stream.get("title", "") + " " + stream.get("displayTitle", "")).lower()
        role = (
            "forced"
            if stream.get("forced") == "1"
            else "signs"
            if re.search(r"\bsigns?\b", title)
            else "sdh"
            if stream.get("hearingImpaired") == "1" or re.search(r"\b(sdh|cc)\b", title)
            else "commentary"
            if "commentary" in title
            else "full_dialogue"
        )
        raw = stream.get("languageCode") or stream.get("languageTag")
        tracks.append(
            TrackObservation(
                stream_id,
                kind,
                physical,
                raw,
                role=role,
                source="external"
                if stream.get("key") or stream.get("external") == "1"
                else "embedded",
            )
        )
        if stream.get("selected") == "1":
            (selected_audio if kind == "audio" else selected_subtitle).append(stream_id)
    _require(len(selected_audio) <= 1 and len(selected_subtitle) <= 1)
    evidence = LanguageEvidence(physical, tuple(tracks), True)
    catalog = db.execute(
        "SELECT * FROM movie_catalog WHERE user_id=? AND imdb_id=?",
        (resource["owner_user_id"], resource["canonical_id"]),
    ).fetchone()
    _require(catalog is not None and catalog["identity_verified"] == 1)
    # Reuse the legacy exact identity/edition/resolution parser and fingerprint.
    service = MovieService(
        db, connections.movie_connections(db, resource["owner_user_id"]), transport=transport
    )
    requested = db.execute(
        "SELECT requested_edition FROM movie_requests WHERE user_id=? AND imdb_id=?",
        (resource["owner_user_id"], resource["canonical_id"]),
    ).fetchone()
    requested_edition = requested[0] if requested else None
    old = service._file(video, int(section), dict(catalog), requested_edition)
    _require(
        not requested_edition
        or (old.edition and old.edition.casefold() == requested_edition.casefold()),
        "The requested movie edition does not match this file.",
    )
    _require(
        old.resolution is not None
        and media.get("videoCodec", "").lower()
        in {"h264", "hevc", "h265", "av1", "vp9", "mpeg4", "mpeg2video", "vc1"}
    )
    await _get(
        base,
        part_key,
        token,
        transport=transport,
        byte_range=True,
        expected_size=int(part.get("size")),
    )
    after = await metadata()
    _require(
        ET.tostring(after) == ET.tostring(video),
        "Movie file or viewer selection changed during the byte check.",
    )
    _require(require_current_source(db, scope=scope, resource=resource) == source)
    _require(
        token == connections._read(db, actor_user_id, "plex_token"),
        "Personal playback credentials changed during verification.",
    )
    with _transaction(db):
        latest = db.execute(
            "SELECT nonce FROM viewer_current_playback_checks WHERE actor_user_id=? AND resource_id=?",
            (actor_user_id, resource["id"]),
        ).fetchone()
        _require(
            latest is not None and latest[0] == nonce,
            "A newer explicit playback check superseded this attempt.",
        )
        binding_id = _insert_binding(db, personal_binding)
        refresh_source_projection(db, scope=scope, resource=resource)
        observation = record_observation(
            db,
            observer_user_id=resource["owner_user_id"],
            resource_id=resource["id"],
            evidence=evidence,
            source_generation=source.source_generation,
            library_id=scope.library_id,
            observed_at=_iso(clock),
        )
        expires = _iso(clock + timedelta(minutes=15))
        file_id = db.execute(
            "INSERT INTO movie_file_observations(resource_id,imdb_id,server_id,section_id,rating_key,media_id,part_id,physical_fingerprint,legacy_fingerprint,resolution,edition,video_codec,source_generation,language_observation_id,observed_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                resource["id"],
                resource["canonical_id"],
                connections._read(db, resource["owner_user_id"], "plex_machine_id"),
                section,
                key,
                media.get("id"),
                part.get("id"),
                physical,
                old.fingerprint,
                old.resolution,
                old.edition,
                media.get("videoCodec", "").lower(),
                source.source_generation,
                observation,
                _iso(clock),
                expires,
            ),
        ).lastrowid
        byte_id = db.execute(
            "INSERT INTO movie_byte_observations(file_observation_id,http_status,bytes_read,observed_at,expires_at) VALUES(?,206,65536,?,?)",
            (file_id, _iso(clock), expires),
        ).lastrowid
        selection_id = db.execute(
            "INSERT INTO viewer_selection_observations(binding_id,file_observation_id,byte_observation_id,selected_audio_id,selected_subtitle_id,observed_at,expires_at) VALUES(?,?,?,?,?,?,?)",
            (
                binding_id,
                file_id,
                byte_id,
                selected_audio[0] if selected_audio else None,
                selected_subtitle[0] if selected_subtitle else None,
                _iso(clock),
                expires,
            ),
        ).lastrowid
        updated = db.execute(
            "UPDATE viewer_current_playback_checks SET selection_id=? WHERE actor_user_id=? AND resource_id=? AND nonce=?",
            (selection_id, actor_user_id, resource["id"], nonce),
        )
        _require(updated.rowcount == 1)
        return selection_id


def receipt_kwargs(db, *, actor_user_id, request_id, evaluation_id, now=None):
    from omakase.plus.language_store import load_request

    load_request(db, actor_user_id=actor_user_id, request_id=request_id)
    evaluation = db.execute(
        "SELECT * FROM language_evaluations WHERE id=? AND request_id=?",
        (evaluation_id, request_id),
    ).fetchone()
    _require(
        evaluation is not None
        and evaluation["physical_compatible"] == 1
        and evaluation["selection_confirmed"] == 1
        and evaluation["playback_selection_id"] is not None
    )
    selection = resolve_selection(
        db,
        actor_user_id=actor_user_id,
        request_id=request_id,
        observation_id=evaluation["observation_id"],
        selection_id=evaluation["playback_selection_id"],
        now=now,
    )
    row = db.execute(
        "SELECT f.*, s.byte_observation_id FROM viewer_selection_observations s JOIN movie_file_observations f ON f.id=s.file_observation_id WHERE s.id=?",
        (selection.observation_id,),
    ).fetchone()
    from omakase.plus.language_policy import LANGUAGES

    physical = db.execute(
        "SELECT evidence_json FROM asset_language_observations WHERE id=?",
        (evaluation["observation_id"],),
    ).fetchone()
    selected = next(
        (
            t
            for t in json.loads(physical[0])["tracks"]
            if t["stream_id"] == selection.selected_audio_id
        ),
        None,
    )
    _require(selected is not None)
    return {
        "selected_audio": LANGUAGES.get(selected["language"], "Unknown"),
        "resolution": row["resolution"],
        "edition": row["edition"],
        "proof_schema": 2,
        "language_evaluation_id": evaluation_id,
        "physical_fingerprint": row["physical_fingerprint"],
        "fingerprint": row["legacy_fingerprint"],
        "plex_key": row["rating_key"],
        "selected_stream_id": selection.selected_audio_id,
        "selected_subtitle_id": selection.selected_subtitle_id,
        "playback_selection_id": selection.observation_id,
        "movie_file_observation_id": row["id"],
        "movie_byte_observation_id": row["byte_observation_id"],
        "source_generation": row["source_generation"],
        "playback_context": "selection:" + str(selection.observation_id),
        "http_status": 206,
        "bytes_read": 65536,
    }
