"""Exact mocked Plex readback; no real provider or account access."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from omakase.plus import households as hh
from omakase.plus import language_store as store
from omakase.plus import playback_proof as proof
from omakase.plus.language_policy import (
    LanguageRequestSnapshot,
    LanguageRule,
    resolve_effective_language,
)
from omakase.plus.secrets import store_secret
from tests.plus import test_language_store as helpers


@pytest.fixture
def data(tmp_path):
    fixture = helpers.setup.__wrapped__(tmp_path)
    original = next(fixture)
    db, lib, *_ = original
    db.execute(
        "INSERT INTO movie_catalog(user_id,imdb_id,tmdb_id,title,year,identity_verified,updated_at) VALUES(1,'tt0000001',1,'Synthetic',2026,1,'2026-01-01')"
    )
    owner = hh.resolve_scope(db, viewer_id=1, library_id=lib)
    item = hh.LibraryRepository(db, owner).add(
        domain="movie", canonical_id="tt0000001", title="Synthetic"
    )
    resource = "f" * 32
    db.execute(
        "INSERT INTO library_resources(id,owner_user_id,domain,canonical_id,season_number) VALUES(?,1,'movie','tt0000001',0)",
        (resource,),
    )
    db.execute("UPDATE library_items SET resource_id=? WHERE id=?", (resource, item))
    yield db, lib, item, resource
    try:
        next(fixture)
    except StopIteration:
        pass


def request(data, name="movie-fr", rule=None, user=2):
    db, lib, item, resource = data
    scope = hh.resolve_scope(db, viewer_id=user, library_id=lib)
    default = store.library_defaults(db, scope)[0].movie
    effective = resolve_effective_language(
        default,
        request_override=rule
        or LanguageRule(
            audio_required=("fr",), subtitle_required=("en",), subtitles_mode="required"
        ),
        allow_override=True,
    )
    snapshot = LanguageRequestSnapshot(
        name, user, lib, "movie", "imdb:tt0000001", effective, item, resource
    )
    store.record_request(db, snapshot)
    return snapshot


def xml(*, subtitle="1", part="30", language="fra"):
    return f'''<MediaContainer><Video type="movie" ratingKey="10" librarySectionID="1" librarySectionTitle="Movies" year="2026" title="Synthetic"><Guid id="imdb://tt0000001"/><Guid id="tmdb://1"/><Media id="20" width="1920" height="1080" videoCodec="hevc"><Part id="{part}" key="/library/parts/{part}/file.mkv" file="/synthetic/movie.mkv" size="100000" duration="1000"><Stream id="40" streamType="2" languageCode="{language}" selected="1"/><Stream id="41" streamType="3" languageCode="eng" selected="{subtitle}"/></Part></Media></Video></MediaContainer>'''


def transport(
    *,
    subtitle="1",
    personal_account=2,
    status=206,
    part_change=False,
    redirect=False,
    server="fixture-server",
    size=65536,
    expected_token="synthetic-member",
):
    seen = []
    metadata_count = 0

    def handle(req):
        nonlocal metadata_count
        seen.append((req.method, req.url.host, req.url.path, req.headers.get("X-Plex-Token")))
        assert req.method == "GET" and "X-Plex-Token" not in req.url.params
        token = req.headers["X-Plex-Token"]
        if req.url.host == "plex.tv":
            assert req.url.path == "/api/v2/user"
            return httpx.Response(
                200, json={"id": 1 if token == "synthetic-owner" else personal_account}
            )
        assert req.url.host == "plex.fixture.invalid" and token == expected_token
        if req.url.path == "/library/sections":
            return httpx.Response(
                200,
                content='<MediaContainer><Directory key="1" type="movie" title="Movies"/></MediaContainer>',
            )
        if req.url.path == "/identity":
            return httpx.Response(200, content=f'<MediaContainer machineIdentifier="{server}"/>')
        if req.url.path == "/library/metadata/10":
            metadata_count += 1
            return (
                httpx.Response(302, headers={"Location": "https://untrusted.invalid"})
                if redirect
                else httpx.Response(
                    200,
                    content=xml(
                        subtitle=subtitle, part="31" if part_change and metadata_count > 1 else "30"
                    ),
                )
            )
        assert req.url.path == "/library/parts/30/file.mkv"
        assert req.headers["Range"] == "bytes=0-65535"
        return httpx.Response(
            status, headers={"Content-Range": "bytes 0-65535/100000"}, content=b"x" * size
        )

    return httpx.MockTransport(handle), seen


def observe(data, snap, **kwargs):
    data[0].commit()
    mock, seen = transport(**kwargs)
    value = asyncio.run(
        proof.observe_movie_playback(
            data[0],
            actor_user_id=snap.actor_user_id,
            request_id=snap.request_id,
            rating_key="10",
            transport=mock,
        )
    )
    row = (
        data[0]
        .execute(
            "SELECT f.language_observation_id FROM viewer_selection_observations s JOIN movie_file_observations f ON f.id=s.file_observation_id WHERE s.id=?",
            (value,),
        )
        .fetchone()
    )
    return value, row[0], seen


def test_positive_readback_links_exact_personal_identity_file_bytes_and_selected_subtitle(data):
    db, *_ = data
    snap = request(data)
    selection, observation, seen = observe(data, snap)
    identifier, result = store.evaluate_request(
        db,
        actor_user_id=2,
        request_id=snap.request_id,
        playback_selection_id=selection,
    )
    assert (
        result.selection_confirmed
        and result.selected_audio_confirmed
        and result.selected_subtitle_confirmed
    )
    kwargs = proof.receipt_kwargs(
        db, actor_user_id=2, request_id=snap.request_id, evaluation_id=identifier
    )
    assert (
        kwargs["bytes_read"] == 65536
        and kwargs["plex_key"] == "10"
        and kwargs["selected_stream_id"] == "40"
        and kwargs["selected_subtitle_id"] == "41"
    )
    assert not any(
        word in json.dumps(kwargs)
        for word in ("synthetic-member", "synthetic-owner", "/synthetic/", "plex.fixture")
    )
    evidence = json.loads(
        db.execute("SELECT evidence_json FROM asset_language_observations").fetchone()[0]
    )
    assert evidence["selection_context"] is None and not any(
        t["selected"] for t in evidence["tracks"]
    )
    assert len(seen) == 7


def test_required_subtitles_unselected_keeps_inventory_but_requires_selection(data):
    db, *_ = data
    snap = request(data)
    selection, observation, _ = observe(data, snap, subtitle="0")
    identifier, result = store.evaluate_request(
        db,
        actor_user_id=2,
        request_id=snap.request_id,
        observation_id=observation,
        playback_selection_id=selection,
    )
    assert (
        result.physical_compatible
        and result.state == "selection_required"
        and not result.selection_confirmed
    )
    assert result.selected_audio_confirmed and not result.selected_subtitle_confirmed
    with pytest.raises(ValueError):
        proof.receipt_kwargs(
            db, actor_user_id=2, request_id=snap.request_id, evaluation_id=identifier
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"personal_account": 1},
        {"server": "different"},
        {"status": 200},
        {"part_change": True},
        {"redirect": True},
        {"size": 65535},
        {"size": 65537},
    ],
)
def test_failed_readback_never_leaves_a_partial_proof(data, kwargs):
    db, *_ = data
    snap = request(data)
    with pytest.raises(ValueError):
        observe(data, snap, **kwargs)
    for table in (
        "viewer_playback_bindings",
        "movie_file_observations",
        "movie_byte_observations",
        "viewer_selection_observations",
        "asset_language_observations",
    ):
        assert db.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0


def test_actual_connection_change_cannot_renew_old_observation_with_new_request(data):
    db, lib, *_ = data
    snap = request(data)
    selection, observation, _ = observe(data, snap)
    old = db.execute(
        "SELECT source_generation FROM asset_language_observations WHERE id=?", (observation,)
    ).fetchone()[0]
    hh.connection_generation_changed(db, owner_user_id=1, integration="plex")
    fresh = request(data, "fresh-after-change")
    with pytest.raises(ValueError, match="generation"):
        store.evaluate_request(
            db,
            actor_user_id=2,
            request_id=fresh.request_id,
            observation_id=observation,
            source_generation=old,
            playback_selection_id=selection,
        )
    with pytest.raises(ValueError, match="generation"):
        store.evaluate_request(
            db,
            actor_user_id=2,
            request_id=fresh.request_id,
            observation_id=observation,
            playback_selection_id=selection,
        )


def test_personal_credential_change_expiry_and_revocation_reject_cached_ready_chain(data):
    db, lib, *_ = data
    snap = request(data)
    selection, observation, _ = observe(data, snap)
    identifier, _ = store.evaluate_request(
        db,
        actor_user_id=2,
        request_id=snap.request_id,
        observation_id=observation,
        playback_selection_id=selection,
    )
    with pytest.raises(ValueError):
        proof.receipt_kwargs(
            db, actor_user_id=3, request_id=snap.request_id, evaluation_id=identifier
        )
    with pytest.raises(ValueError, match="expired"):
        proof.receipt_kwargs(
            db,
            actor_user_id=2,
            request_id=snap.request_id,
            evaluation_id=identifier,
            now=datetime.now(timezone.utc) + timedelta(hours=1),
        )
    store_secret(db, 2, "plex_token", "changed-synthetic-token")
    with pytest.raises(ValueError):
        proof.receipt_kwargs(
            db, actor_user_id=2, request_id=snap.request_id, evaluation_id=identifier
        )
    hh.leave_or_revoke(db, hh.resolve_scope(db, viewer_id=1, library_id=lib), member_id=2)
    with pytest.raises(ValueError):
        proof.receipt_kwargs(
            db, actor_user_id=2, request_id=snap.request_id, evaluation_id=identifier
        )


def test_physical_inventory_cannot_forge_a_members_selection(data):
    db, lib, _, resource = data
    snap = request(data)
    selection, observation, _ = observe(data, snap)
    _, result = store.evaluate_request(
        db,
        actor_user_id=2,
        request_id=snap.request_id,
        observation_id=observation,
        playback_context="plex-user:2:forged",
    )
    assert result.physical_compatible and result.state == "selection_unconfirmed"
    from omakase.plus.language_policy import evidence_from_mapping

    evidence = evidence_from_mapping(
        json.loads(
            db.execute(
                "SELECT evidence_json FROM asset_language_observations WHERE id=?", (observation,)
            ).fetchone()[0]
        )
    )
    with pytest.raises(ValueError, match="Physical inventory"):
        store.record_observation(
            db,
            observer_user_id=1,
            resource_id=resource,
            evidence=replace(evidence, tracks=(replace(evidence.tracks[0], selected=True),)),
            source_generation=None,
            library_id=lib,
        )


def insert_ready(db, values):
    row = {
        "user_id": 2,
        "imdb_id": "tt0000001",
        "selected_audio": "French",
        "state": "ready",
        "code": "ready",
        "detail": "synthetic",
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        **values,
    }
    names = ",".join(row)
    db.execute(
        "INSERT INTO movie_verifications(" + names + ") VALUES(" + ",".join("?" for _ in row) + ")",
        tuple(row.values()),
    )


def test_036_positive_chain_and_mismatched_file_byte_key_and_stream_are_rejected(data):
    db, *_ = data
    snap = request(data)
    selection, observation, _ = observe(data, snap)
    identifier, _ = store.evaluate_request(
        db,
        actor_user_id=2,
        request_id=snap.request_id,
        observation_id=observation,
        playback_selection_id=selection,
    )
    values = proof.receipt_kwargs(
        db, actor_user_id=2, request_id=snap.request_id, evaluation_id=identifier
    )
    for field, value in (
        ("plex_key", "999999"),
        ("fingerprint", "0" * 64),
        ("physical_fingerprint", "0" * 64),
        ("selected_stream_id", "999"),
        ("selected_subtitle_id", None),
        ("source_generation", "0" * 64),
        ("movie_byte_observation_id", 999999),
        ("playback_context", "plex-user:2:forged"),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            insert_ready(db, {**values, field: value})
    insert_ready(db, values)
    assert db.execute("SELECT proof_schema FROM movie_verifications").fetchone()[0] == 2
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE movie_verifications SET plex_key='999999'")
    hh.connection_generation_changed(db, owner_user_id=1, integration="plex")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE movie_verifications SET detail='retry'")


def test_closed_library_or_removed_item_rejects_cached_proof(data):
    db, lib, item, _ = data
    snap = request(data)
    selection, observation, _ = observe(data, snap)
    identifier, _ = store.evaluate_request(
        db,
        actor_user_id=2,
        request_id=snap.request_id,
        observation_id=observation,
        playback_selection_id=selection,
    )
    db.execute("UPDATE library_items SET state='removed' WHERE id=?", (item,))
    with pytest.raises(ValueError):
        proof.receipt_kwargs(
            db, actor_user_id=2, request_id=snap.request_id, evaluation_id=identifier
        )


def test_actual_saved_owner_configuration_change_rejects_source_without_trusting_projection(data):
    db, *_ = data
    snap = request(data)
    selection, observation, _ = observe(data, snap)
    identifier, _ = store.evaluate_request(
        db,
        actor_user_id=2,
        request_id=snap.request_id,
        observation_id=observation,
        playback_selection_id=selection,
    )
    store_secret(db, 1, "plex_token", "rotated-synthetic-owner")
    with pytest.raises(ValueError, match="generation"):
        proof.receipt_kwargs(
            db, actor_user_id=2, request_id=snap.request_id, evaluation_id=identifier
        )


def test_preference_only_unknown_selected_language_does_not_claim_requirement(data):
    db, *_ = data
    snap = request(data, rule=LanguageRule(audio_preferred=("fr",)))
    mock, seen = transport()
    original = mock.handler

    def changed(req):
        reply = original(req)
        if req.url.path == "/library/metadata/10":
            return httpx.Response(200, content=xml(language="und"))
        return reply

    mock = httpx.MockTransport(changed)
    db.commit()
    selection = asyncio.run(
        proof.observe_movie_playback(
            db, actor_user_id=2, request_id=snap.request_id, rating_key="10", transport=mock
        )
    )
    observation = db.execute(
        "SELECT language_observation_id FROM movie_file_observations"
    ).fetchone()[0]
    _, result = store.evaluate_request(
        db,
        actor_user_id=2,
        request_id=snap.request_id,
        observation_id=observation,
        playback_selection_id=selection,
    )
    assert result.selection_confirmed and not result.audio_required_confirmed


def test_same_file_two_verified_viewers_share_physical_identity_not_personal_selection(data):
    db, *_ = data
    member = request(data)
    member_selection, member_observation, _ = observe(data, member)
    owner = request(data, "owner-fr", LanguageRule(audio_required=("fr",)), user=1)
    owner_selection, owner_observation, _ = observe(
        data, owner, expected_token="synthetic-owner", subtitle="0"
    )
    rows = db.execute(
        "SELECT physical_fingerprint,legacy_fingerprint FROM movie_file_observations ORDER BY id"
    ).fetchall()
    assert rows[0][0] == rows[1][0]
    assert member_selection != owner_selection
    for actor, snap, observation, selection in (
        (2, member, member_observation, member_selection),
        (1, owner, owner_observation, owner_selection),
    ):
        identifier, result = store.evaluate_request(
            db,
            actor_user_id=actor,
            request_id=snap.request_id,
            observation_id=observation,
            playback_selection_id=selection,
        )
        assert result.physical_compatible and result.selection_confirmed
        values = proof.receipt_kwargs(
            db, actor_user_id=actor, request_id=snap.request_id, evaluation_id=identifier
        )
        insert_ready(db, {**values, "user_id": actor})
    with pytest.raises(ValueError):
        store.evaluate_request(
            db,
            actor_user_id=2,
            request_id=member.request_id,
            observation_id=member_observation,
            playback_selection_id=owner_selection,
        )
    assert db.execute("SELECT count(*) FROM media_assets").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM movie_verifications").fetchone()[0] == 2


def test_schema1_catalog_identity_and_original_delete_cascade_are_preserved(data):
    db, *_ = data
    legacy = {
        "fingerprint": "a" * 64,
        "plex_key": "10",
        "selected_audio": "English",
        "http_status": 206,
        "bytes_read": 65536,
    }
    with pytest.raises(sqlite3.IntegrityError):
        insert_ready(db, legacy)
    insert_ready(db, {**legacy, "user_id": 1})
    assert db.execute("SELECT proof_schema FROM movie_verifications").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE movie_verifications SET user_id=2")
    db.execute("DELETE FROM movie_catalog WHERE user_id=1 AND imdb_id='tt0000001'")
    assert db.execute("SELECT count(*) FROM movie_verifications").fetchone()[0] == 0


def test_connection_writer_invalidation_blocks_sql_ready_without_deleting_history(data):
    db, *_ = data
    snap = request(data)
    selection, observation, _ = observe(data, snap)
    identifier, _ = store.evaluate_request(
        db,
        actor_user_id=2,
        request_id=snap.request_id,
        observation_id=observation,
        playback_selection_id=selection,
    )
    values = proof.receipt_kwargs(
        db, actor_user_id=2, request_id=snap.request_id, evaluation_id=identifier
    )
    proof.invalidate_personal_playback(db, user_id=2)
    with pytest.raises(sqlite3.IntegrityError):
        insert_ready(db, values)
    with pytest.raises(ValueError):
        proof.receipt_kwargs(
            db, actor_user_id=2, request_id=snap.request_id, evaluation_id=identifier
        )
    assert db.execute("SELECT count(*) FROM viewer_selection_observations").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM language_evaluations").fetchone()[0] == 1


def test_invalidation_hook_is_safe_before_034_exists():
    db = sqlite3.connect(":memory:")
    proof.invalidate_personal_playback(db, user_id=1)
    assert db.execute("SELECT name FROM sqlite_master").fetchall() == []


def test_verifier_does_not_commit_callers_transaction_or_start_http_under_write_lock(data):
    db, *_ = data
    snap = request(data)
    mock, seen = transport()
    assert db.in_transaction
    with pytest.raises(ValueError, match="Commit request preparation"):
        asyncio.run(
            proof.observe_movie_playback(
                db, actor_user_id=2, request_id=snap.request_id, rating_key="10", transport=mock
            )
        )
    assert db.in_transaction and not seen


def test_byte_range_total_must_match_exact_observed_part_size(data):
    db, *_ = data
    snap = request(data)
    db.commit()
    mock, seen = transport()
    original = mock.handler

    def changed(req):
        response = original(req)
        if req.url.path == "/library/parts/30/file.mkv":
            return httpx.Response(
                206, headers={"Content-Range": "bytes 0-65535/999999"}, content=b"x" * 65536
            )
        return response

    with pytest.raises(ValueError):
        asyncio.run(
            proof.observe_movie_playback(
                db,
                actor_user_id=2,
                request_id=snap.request_id,
                rating_key="10",
                transport=httpx.MockTransport(changed),
            )
        )
    assert db.execute("SELECT count(*) FROM movie_byte_observations").fetchone()[0] == 0


def test_saved_machine_binding_change_invalidates_the_actual_source_digest(data):
    db, *_ = data
    snap = request(data)
    selection, observation, _ = observe(data, snap)
    identifier, _ = store.evaluate_request(
        db, actor_user_id=2, request_id=snap.request_id, playback_selection_id=selection
    )
    store_secret(db, 1, "plex_machine_id", "new-verified-server")
    with pytest.raises(ValueError, match="generation"):
        proof.receipt_kwargs(
            db, actor_user_id=2, request_id=snap.request_id, evaluation_id=identifier
        )
