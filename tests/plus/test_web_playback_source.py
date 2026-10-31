"""Synthetic exact-source security checks; no provider access."""

import asyncio
import json
import re
import sqlite3

import httpx
import pytest

from omakase.plus import web_playback_source as source


@pytest.fixture
def fixture(monkeypatch):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("""
    CREATE TABLE libraries(id,kind,owner_user_id,name,state,membership_revision,policy_revision,members_can_add,acquisition_requests);
    INSERT INTO libraries VALUES('library','household',1,'Synthetic','active',1,1,1,0);
    CREATE TABLE household_memberships(library_id,user_id,role,state);
    INSERT INTO household_memberships VALUES('library',2,'member','active');
    CREATE TABLE library_items(id,state,library_id,domain,canonical_id,season_number,resource_id);
    CREATE TABLE library_resources(id,owner_user_id,domain,canonical_id,season_number,legacy_id);
    INSERT INTO library_resources VALUES('resource',1,'anime','123',1,'1');
    INSERT INTO library_items VALUES('item','active','library','anime','123',1,'resource');
    CREATE TABLE media_monitors(id INTEGER,user_id INTEGER,anilist_id INTEGER,season_number INTEGER);
    INSERT INTO media_monitors VALUES(1,1,123,1);
    CREATE TABLE media_assets(id,monitor_id,torrent_hash,status,inventory_verified_at,inventory_fingerprint);
    INSERT INTO media_assets VALUES(7,1,'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA','downloaded','now','inventory');
    CREATE TABLE media_curator_receipt_latest(id,receipt_json,receipt_hash);
    CREATE TABLE viewer_playback_bindings(actor_user_id,external_account_id,server_id);
    """)
    file = dict(
        anilist_id=123,
        season_number=1,
        torrent_hash="A" * 40,
        source="Folder/episode.mkv",
        episodes=[1],
        stat={"size": 100000},
        selected=True,
        inventory_generation="inventory",
        source_generation="generation",
    )
    receipt = dict(
        schema_version="curator-receipt-v4",
        mode="live",
        topology_converged=True,
        selection_applied=True,
        outcome="success",
        publication_files=[file],
    )
    db.execute(
        "INSERT INTO media_curator_receipt_latest VALUES(1,?,?)", (json.dumps(receipt), "receipt")
    )
    db.commit()
    manifest = {
        "titles": [
            {
                "anilist_id": 123,
                "canonical_title": "Synthetic",
                "assets": [{"torrent_hash": "A" * 40, "language_contract": None}],
            }
        ]
    }
    monkeypatch.setattr(
        source.quality_publication, "validate_current_selection", lambda *a: manifest
    )
    monkeypatch.setattr(source.quality_publication, "source_generation", lambda *a: "generation")
    values = {
        (1, "plex_token"): "owner-token",
        (2, "plex_token"): "personal-token",
        (1, "plex_machine_id"): "fixture-server",
    }
    monkeypatch.setattr(
        source.connections, "_read", lambda db, user, key: values.get((user, key), "")
    )
    monkeypatch.setattr(
        source.connections, "_values", lambda *a: ("https://plex.fixture.invalid", "owner-token")
    )
    monkeypatch.setenv("OMAKASE_PLUS_PLEX_MANAGED_ROOT", "/managed")
    return db, values, receipt, manifest


def transport(
    *,
    path="/managed/Anime/Synthetic/Season 01/Synthetic - S01E01.mkv",
    episode=1,
    duplicate=False,
    status=200,
    callback=None,
    envelope_section=False,
    detail_extra=False,
    part_change=False,
    thin_listing=False,
    selected_flags=False,
    stream_index=None,
    duplicate_index=False,
    server="fixture-server",
    section_id="3",
    stream_codec="aac",
    metadata_status=None,
    metadata_missing_part=False,
):
    def handle(req):
        if callback:
            callback(req)
        assert "X-Plex-Token" not in req.url.params
        if req.url.host == "plex.tv":
            return httpx.Response(
                200, json={"id": 1 if req.headers["X-Plex-Token"] == "owner-token" else 2}
            )
        assert req.headers["X-Plex-Token"] == "personal-token"
        if req.url.path == "/identity":
            body = f'<MediaContainer machineIdentifier="{server}"/>'
        elif req.url.path == "/library/sections":
            body = '<MediaContainer><Directory key="3" type="show" title="Anime"><Location path="/managed/Anime"/></Directory></MediaContainer>'
        else:
            video = f'<Video ratingKey="20" grandparentRatingKey="10" librarySectionID="3" type="episode" parentIndex="1" index="{episode}" duration="60000"><Media id="30" container="mkv" videoCodec="h264" audioCodec="aac"><Part id="40" key="/library/parts/40/file.mkv" file="{path}" size="100000" duration="60000"><Stream id="50" streamType="2" codec="aac" languageCode="jpn" default="1"/></Part></Media></Video>'
            body = (
                '<MediaContainer size="1" totalSize="1">'
                + video * (2 if duplicate else 1)
                + "</MediaContainer>"
            )
        if envelope_section and "<Video " in body:
            body = body.replace(' librarySectionID="3"', "").replace(
                "<MediaContainer ", '<MediaContainer librarySectionID="3" '
            )
        if thin_listing and req.url.path == "/library/sections/3/all":
            body = body.replace(' container="mkv" videoCodec="h264" audioCodec="aac"', "")
            body = re.sub(r"<Stream [^>]*/>", "", body)
        if req.url.path == "/library/metadata/20":
            if detail_extra:
                body = body.replace(
                    "</Video>", '<Guid id="synthetic://123"/><Role tag="Synthetic"/></Video>'
                )
            if part_change:
                body = body.replace('id="40"', 'id="41"').replace("/parts/40/", "/parts/41/")
        if selected_flags:
            body = body.replace("<Media id=", '<Media selected="1" default="1" id=')
            body = body.replace("<Part id=", '<Part selected="1" default="1" id=')
            body = body.replace('default="1"/>', 'default="0" selected="1"/>')
        if stream_index is not None:
            body = body.replace('<Stream id="50"', f'<Stream index="{stream_index}" id="50"')
        if duplicate_index:
            body = body.replace(
                "</Part>", f'<Stream id="51" streamType="3" index="{stream_index}"/></Part>'
            )
        body = body.replace('librarySectionID="3"', f'librarySectionID="{section_id}"')
        body = body.replace(' codec="aac"', f' codec="{stream_codec}"')
        response_status = status
        if req.url.path == "/library/metadata/20":
            if metadata_status is not None:
                response_status = metadata_status
            if metadata_missing_part:
                body = re.sub(r"<Part .*?</Part>", "", body, flags=re.S)
        return httpx.Response(response_status, content=body)

    return httpx.MockTransport(handle)


def run(fixture, **kw):
    return asyncio.run(
        source.resolve_anime_source(
            fixture[0],
            actor_user_id=2,
            library_id="library",
            anilist_id=123,
            season_number=1,
            episode_number=1,
            transport=transport(**kw),
        )
    )


def test_exact_source_and_private_projection(fixture):
    result = run(fixture)
    assert result.part_size == 100000 and result.metadata_key == "20"
    assert result.streams[0].stream_id == "50"
    public = json.dumps(source.public_projection(result))
    assert "token" not in public and "/managed" not in public and "fixture.invalid" not in public
    assert "personal-token" not in repr(result)
    source.revalidate_source(fixture[0], result)


def _count_validations(monkeypatch):
    calls = []
    original = source.quality_publication.validate_current_selection

    def validate(*args):
        calls.append(1)
        return original(*args)

    monkeypatch.setattr(source.quality_publication, "validate_current_selection", validate)
    return calls


def test_remote_callback_reuses_only_publication_with_fresh_guards(fixture, monkeypatch):
    resolved = run(fixture)
    calls = _count_validations(monkeypatch)
    snapshots = []
    original = source.revalidate_scope

    def scope(*args):
        snapshots.append(1)
        return original(*args)

    monkeypatch.setattr(source, "revalidate_scope", scope)
    source.revalidate_source(fixture[0], resolved)
    asyncio.run(source.revalidate_remote_source(fixture[0], resolved, transport=transport()))
    source.revalidate_source(fixture[0], resolved)
    assert len(calls) == 1 and len(snapshots) == 8
    fixture[1][2, "plex_token"] = "rotated"
    with pytest.raises(source.PlaybackSourceError):
        source.revalidate_source(fixture[0], resolved)
    assert len(calls) == 1  # Credentials are always read, independent of memoization.


def test_start_atomic_binding_uses_real_source_guard_in_fresh_write_transaction(fixture):
    """Start rechecks the actual source inside its owned BEGIN IMMEDIATE."""
    db = fixture[0]
    resolved = run(fixture)
    source.revalidate_source(db, resolved)
    assert not db.in_transaction
    with source.fresh_source_transaction(db):
        assert db.in_transaction
        # Match Start's guard-before-binding ordering; no injected permissive guard.
        assert source.revalidate_source(db, resolved) is resolved
        db.execute(
            "INSERT INTO viewer_playback_bindings VALUES(?,?,?)",
            (resolved.actor_user_id, resolved.account_id, resolved.server_id),
        )
    assert not db.in_transaction
    assert db.execute("SELECT COUNT(*) FROM viewer_playback_bindings").fetchone()[0] == 1


def test_fresh_source_transaction_never_reuses_or_publishes_memo(fixture, monkeypatch):
    db = fixture[0]
    resolved = run(fixture)
    assert resolved._publication_cache
    calls = _count_validations(monkeypatch)
    with source.fresh_source_transaction(db):
        source.revalidate_source(db, resolved)
        assert resolved._publication_cache == {}
        source.revalidate_source(db, resolved)
        assert resolved._publication_cache == {}
    assert len(calls) == 2
    source.revalidate_source(db, resolved)
    assert len(calls) == 3 and resolved._publication_cache


def test_fresh_source_transaction_marker_is_connection_scoped_and_reset(fixture):
    db = fixture[0]
    resolved = run(fixture)
    other = sqlite3.connect(":memory:")
    other.row_factory = sqlite3.Row
    db.backup(other)
    try:
        with source.fresh_source_transaction(db):
            other.execute("BEGIN IMMEDIATE")
            with pytest.raises(source.PlaybackSourceError):
                source.revalidate_source(other, resolved)
            other.rollback()
            with pytest.raises(source.PlaybackSourceError):
                with source.fresh_source_transaction(db):
                    pytest.fail("Nested scope must not acquire trust")
            assert source.revalidate_source(db, resolved) is resolved
        assert source._fresh_write_connection.get() is None
        db.execute("BEGIN IMMEDIATE")
        with pytest.raises(source.PlaybackSourceError):
            source.revalidate_source(db, resolved)
        db.rollback()
    finally:
        other.close()


def test_fresh_source_transaction_rolls_back_revocation_and_resets_marker(fixture):
    db = fixture[0]
    resolved = run(fixture)
    with pytest.raises(source.PlaybackSourceError):
        with source.fresh_source_transaction(db):
            db.execute("UPDATE media_assets SET inventory_fingerprint='revoked'")
            source.revalidate_source(db, resolved)
    assert not db.in_transaction and source._fresh_write_connection.get() is None
    assert resolved._publication_cache == {}
    assert db.execute("SELECT inventory_fingerprint FROM media_assets").fetchone()[0] == "inventory"
    source.revalidate_source(db, resolved)
    db.execute("UPDATE media_assets SET inventory_fingerprint='revoked'")
    db.commit()
    with pytest.raises(source.PlaybackSourceError):
        with source.fresh_source_transaction(db):
            source.revalidate_source(db, resolved)


def test_fresh_source_transaction_rejects_preheld_wal_snapshot(fixture, tmp_path):
    path = tmp_path / "preheld.db"
    db = sqlite3.connect(path)
    fixture[0].backup(db)
    db.execute("PRAGMA journal_mode=WAL")
    other = sqlite3.connect(path)
    try:
        db.execute("BEGIN")
        db.execute("SELECT * FROM media_assets").fetchall()
        other.execute("UPDATE media_assets SET inventory_fingerprint='revoked'")
        other.commit()
        with pytest.raises(source.PlaybackSourceError):
            with source.fresh_source_transaction(db):
                pytest.fail("Old WAL snapshot must not be promoted")
        assert db.in_transaction and source._fresh_write_connection.get() is None
    finally:
        db.rollback()
        other.close()
        db.close()


def test_publication_memo_own_mutation_rollback_savepoint_and_environment(fixture, monkeypatch):
    db = fixture[0]
    resolved = run(fixture)
    calls = _count_validations(monkeypatch)
    source.revalidate_source(db, resolved)
    db.execute("UPDATE media_assets SET inventory_fingerprint=inventory_fingerprint")
    with pytest.raises(source.PlaybackSourceError):
        source.revalidate_source(db, resolved)
    db.rollback()
    source.revalidate_source(db, resolved)
    db.execute("SAVEPOINT mutation")
    db.execute("UPDATE media_assets SET inventory_fingerprint=inventory_fingerprint")
    db.execute("ROLLBACK TO mutation")
    db.execute("RELEASE mutation")
    source.revalidate_source(db, resolved)
    monkeypatch.setenv("OMAKASE_UNRELATED_OPERATOR_INPUT", "changed")
    source.revalidate_source(db, resolved)
    monkeypatch.setattr(source.quality_publication, "QUALITY_PUBLICATION_QUALIFIED", True)
    source.revalidate_source(db, resolved)
    assert len(calls) == 5
    db.execute("UPDATE media_assets SET inventory_fingerprint='changed'")
    db.commit()
    with pytest.raises(source.PlaybackSourceError):
        source.revalidate_source(db, resolved)


def test_publication_memo_foreign_commit_and_stale_read_transaction(fixture, monkeypatch, tmp_path):
    from dataclasses import replace

    path = tmp_path / "publication.db"
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    fixture[0].backup(db)
    db.execute("PRAGMA journal_mode=WAL")
    other = sqlite3.connect(path)
    resolved = replace(run(fixture), _publication_cache={})
    calls = _count_validations(monkeypatch)
    try:
        source.revalidate_source(db, resolved)
        other.execute("UPDATE media_curator_receipt_latest SET receipt_hash='foreign-refresh'")
        other.commit()
        source.revalidate_source(db, resolved)
        assert len(calls) == 2
        db.execute("BEGIN")
        db.execute("SELECT * FROM media_assets").fetchall()
        other.execute("UPDATE media_assets SET inventory_fingerprint='revoked'")
        other.commit()
        with pytest.raises(source.PlaybackSourceError):
            source.revalidate_source(db, resolved)
        db.rollback()
        with pytest.raises(source.PlaybackSourceError):
            source.revalidate_source(db, resolved)
    finally:
        other.close()
        db.close()


def test_publication_validation_rejects_foreign_commit_during_snapshot(
    fixture, monkeypatch, tmp_path
):
    from dataclasses import replace

    db = sqlite3.connect(tmp_path / "publication.db")
    db.row_factory = sqlite3.Row
    fixture[0].backup(db)
    db.execute("PRAGMA journal_mode=WAL")
    other = sqlite3.connect(tmp_path / "publication.db")
    resolved = replace(run(fixture), _publication_cache={})
    original = source.quality_publication.validate_current_selection

    def validate(*args):
        result = original(*args)
        other.execute("UPDATE media_assets SET inventory_fingerprint='revoked'")
        other.commit()
        return result

    monkeypatch.setattr(source.quality_publication, "validate_current_selection", validate)
    try:
        with pytest.raises(source.PlaybackSourceError):
            source.revalidate_source(db, resolved)
        assert resolved._publication_cache == {} and not db.in_transaction
    finally:
        other.close()
        db.close()


def test_publication_memo_never_reuses_connection_local_counters(fixture, monkeypatch):
    resolved = run(fixture)
    calls = _count_validations(monkeypatch)
    clones = [sqlite3.connect(":memory:") for _ in range(2)]
    try:
        for db in clones:
            db.row_factory = sqlite3.Row
            fixture[0].backup(db)
        assert source._publication_boundary(clones[0]) == source._publication_boundary(clones[1])
        for db in clones:
            source.revalidate_source(db, resolved)
        assert len(calls) == 2
        clones[0].execute("UPDATE media_assets SET inventory_fingerprint='revoked'")
        clones[0].commit()
        with pytest.raises(source.PlaybackSourceError):
            source.revalidate_source(clones[0], resolved)
        assert resolved._publication_cache == {}
        source.revalidate_source(clones[1], resolved)
        assert len(calls) == 4
    finally:
        for db in clones:
            db.close()


def test_bounded_media_free_authorization_profile(fixture):
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts/profile_playback_authorization.py"
    spec = importlib.util.spec_from_file_location("authorization_profile", path)
    profile = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(profile)
    result = asyncio.run(
        profile.profile_authorization(
            fixture[0],
            dict(
                actor_user_id=2,
                library_id="library",
                anilist_id=123,
                season_number=1,
                episode_number=1,
                duration_ms=60000,
            ),
            callbacks=2,
        )
    )
    assert result["status"] == "pass"
    counts = result["profile_counts"]
    assert counts["authorization:omakase.plus.web_playback_source._snapshot"]["calls"] == 16
    assert (
        counts["authorization:omakase.plus.quality_publication.validate_current_selection"]["calls"]
        == 1
    )
    assert result["mock_metadata_requests"] == {"identity": 2, "detail": 2}
    assert result["database_changes"] == result["original_media_reads"] == 0
    assert result["real_provider_requests"] == result["ffmpeg_jobs"] == 0
    assert result["total_cpu_seconds"] < 2 and result["total_wall_seconds"] < 3


@pytest.mark.parametrize(
    "kwargs",
    [{"path": "/unmanaged/Synthetic.mkv"}, {"episode": 2}, {"duplicate": True}, {"status": 302}],
)
def test_wrong_identity_ambiguity_and_redirect_fail_closed(fixture, kwargs):
    with pytest.raises(source.PlaybackSourceError):
        run(fixture, **kwargs)


def test_owner_credentials_cannot_masquerade(fixture):
    fixture[1][2, "plex_token"] = "owner-token"
    with pytest.raises(source.PlaybackSourceError):
        run(fixture)


def test_source_and_credentials_revalidate(fixture):
    result = run(fixture)
    fixture[1][2, "plex_token"] = "rotated"
    with pytest.raises(source.PlaybackSourceError):
        source.revalidate_source(fixture[0], result)


def test_revocation_during_http_fails(fixture):
    def revoke(req):
        fixture[0].execute("UPDATE library_items SET state='removed'")

    with pytest.raises(source.PlaybackSourceError):
        run(fixture, callback=revoke)


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE library_items SET resource_id=NULL",
        "UPDATE library_resources SET owner_user_id=2",
        "UPDATE library_resources SET canonical_id='456'",
        "UPDATE library_resources SET season_number=2",
        "UPDATE library_resources SET legacy_id='2'",
        "UPDATE media_assets SET monitor_id=2",
    ],
)
def test_catalog_and_managed_resource_binding_fail_closed(fixture, sql):
    fixture[0].execute(sql)
    with pytest.raises(source.PlaybackSourceError):
        run(fixture)


def test_resource_removal_invalidates_already_resolved_source(fixture):
    result = run(fixture)
    fixture[0].execute("UPDATE library_items SET resource_id=NULL")
    with pytest.raises(source.PlaybackSourceError):
        source.revalidate_source(fixture[0], result)


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE household_memberships SET state='removed'",
        "UPDATE libraries SET membership_revision=2",
        "UPDATE libraries SET policy_revision=2",
        "UPDATE libraries SET state='closed'",
        "UPDATE media_assets SET inventory_fingerprint='changed'",
    ],
)
def test_revocation_invalidates_source_snapshot(fixture, sql):
    result = run(fixture)
    fixture[0].execute(sql)
    with pytest.raises(source.PlaybackSourceError):
        source.revalidate_source(fixture[0], result)


def test_current_publication_validator_failure_is_closed(fixture, monkeypatch):
    def reject(*args):
        raise ValueError("synthetic current manifest mismatch")

    monkeypatch.setattr(source.quality_publication, "validate_current_selection", reject)
    with pytest.raises(source.PlaybackSourceError):
        run(fixture)


@pytest.mark.parametrize(
    "second",
    [
        '<MediaContainer size="1" totalSize="2" offset="1"><Video ratingKey="2"/></MediaContainer>',
        '<MediaContainer size="1" totalSize="3" offset="1"><Video ratingKey="2"/></MediaContainer>',
        '<MediaContainer size="1" totalSize="2" offset="0"><Video ratingKey="2"/></MediaContainer>',
        '<MediaContainer size="1" totalSize="2" offset="1"><Video ratingKey="1"/></MediaContainer>',
        '<MediaContainer size="0" totalSize="2" offset="1"/>',
    ],
)
def test_pagination_completeness_and_consistency(second):
    from xml.etree.ElementTree import fromstring

    pages = [
        '<MediaContainer librarySectionID="3" size="1" totalSize="2" offset="0"><Video ratingKey="1"/></MediaContainer>',
        second,
    ]

    async def xml(path):
        assert "X-Plex-Container-Size=40" in path
        assert "&index=" not in path and "&parentIndex=" not in path
        return fromstring(
            pages.pop(0).replace(
                "<MediaContainer size=", '<MediaContainer librarySectionID="3" size='
            )
        )

    if 'totalSize="2" offset="1"><Video ratingKey="2"' in second:
        assert len(asyncio.run(source._episode_pages(xml, "3", 1, 1))) == 2
    else:
        with pytest.raises(source.PlaybackSourceError):
            asyncio.run(source._episode_pages(xml, "3", 1, 1))


def test_plex_section_identity_in_envelope_without_video_attribute(fixture):
    result = run(fixture, envelope_section=True)
    assert result.part_size == 100000


def test_detailed_metadata_enrichment_preserves_exact_physical_source(fixture):
    assert run(fixture, detail_extra=True).metadata_key == "20"


def test_part_substitution_during_discovery_is_rejected(fixture):
    with pytest.raises(source.PlaybackSourceError):
        run(fixture, part_change=True)


def test_detail_hydrates_streams_and_codecs_omitted_from_listing(fixture):
    result = run(fixture, thin_listing=True)
    assert result.container == "mkv" and result.video_codec == "h264"
    assert result.streams[0].stream_id == "50"


def test_contradictory_envelope_and_video_section_rejected():
    from xml.etree.ElementTree import fromstring

    async def xml(path):
        return fromstring(
            '<MediaContainer librarySectionID="3" size="1" totalSize="1"><Video ratingKey="1" librarySectionID="4"/></MediaContainer>'
        )

    with pytest.raises(source.PlaybackSourceError):
        asyncio.run(source._episode_pages(xml, "3", 1, 1))


def test_noop_receipt_refresh_and_unrelated_title_preserve_exact_source_version(fixture):
    before = run(fixture)
    receipt = fixture[2]
    receipt.update(run_id="new-run", completed_at="new-time")
    receipt["publication_files"].append(dict(receipt["publication_files"][0], anilist_id=456))
    fixture[0].execute(
        "UPDATE media_curator_receipt_latest SET receipt_hash='refreshed',receipt_json=?",
        (json.dumps(receipt),),
    )
    fixture[0].commit()
    source.revalidate_source(fixture[0], before)
    after = run(fixture)
    assert before.source_identity == after.source_identity
    assert before.media_version == after.media_version


@pytest.mark.parametrize(
    "field,value",
    [
        ("source", "Folder/new.mkv"),
        ("source_generation", "new-generation"),
        ("inventory_generation", "new-inventory"),
        ("stat", {"size": 100000, "mtime_ns": 999}),
    ],
)
def test_genuine_selected_file_change_invalidates_source(fixture, field, value):
    before = run(fixture)
    fixture[2]["publication_files"][0][field] = value
    fixture[0].execute(
        "UPDATE media_curator_receipt_latest SET receipt_json=?", (json.dumps(fixture[2]),)
    )
    with pytest.raises(source.PlaybackSourceError):
        source.revalidate_source(fixture[0], before)


def test_changed_publication_policy_invalidates_source(fixture):
    before = run(fixture)
    fixture[3]["titles"][0]["assets"][0]["language_contract"] = {"policy_revision": 2}
    # This mocked manifest stands in for a persisted policy change.
    fixture[0].execute("UPDATE media_curator_receipt_latest SET receipt_hash='policy-change'")
    fixture[0].commit()
    with pytest.raises(source.PlaybackSourceError):
        source.revalidate_source(fixture[0], before)


def test_personal_selection_flags_do_not_change_media_version(fixture):
    before = run(fixture)
    after = run(fixture, selected_flags=True)
    assert before.media_version == after.media_version


@pytest.mark.parametrize("index", ["0", "1", "127", "001"])
def test_detailed_stream_container_index_is_separate_from_plex_id(fixture, index):
    result = run(fixture, stream_index=index)
    assert result.streams[0].stream_id == "50"
    assert result.streams[0].container_index == int(index)
    assert "container_index" not in json.dumps(source.public_projection(result))


def test_missing_container_index_does_not_infer_from_opaque_stream_id(fixture):
    result = run(fixture)
    assert result.streams[0].stream_id == "50"
    assert result.streams[0].container_index is None


@pytest.mark.parametrize("index", ["", "-1", "128", "1.5", "+1", " 1", "one", "9999"])
def test_malformed_container_index_fails_closed(fixture, index):
    with pytest.raises(source.PlaybackSourceError):
        run(fixture, stream_index=index)


def test_duplicate_present_container_indices_fail_closed_despite_distinct_plex_ids(fixture):
    with pytest.raises(source.PlaybackSourceError):
        run(fixture, stream_index="1", duplicate_index=True)


def remote(fixture, resolved, **kwargs):
    return asyncio.run(
        source.revalidate_remote_source(fixture[0], resolved, transport=transport(**kwargs))
    )


def test_remote_guard_personal_access_only_two_bounded_server_reads(fixture):
    resolved = run(fixture)
    seen = []

    def check(req):
        assert req.url.host == "plex.fixture.invalid"
        assert req.headers["X-Plex-Token"] == "personal-token"
        seen.append(req.url.path)

    assert remote(fixture, resolved, callback=check) is resolved
    assert seen == ["/identity", "/library/metadata/20"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"status": 401},
        {"status": 403},
        {"status": 302},
        {"server": "different"},
        {"path": "/unmanaged/wrong.mkv"},
        {"section_id": "4"},
        {"episode": 2},
        {"part_change": True},
        {"stream_codec": "opus"},
    ],
)
def test_remote_revocation_and_exact_source_changes_fail_closed(fixture, kwargs):
    resolved = run(fixture)
    with pytest.raises(source.PlaybackSourceError):
        remote(fixture, resolved, **kwargs)


def test_remote_scope_revocation_during_metadata_request_is_rejected(fixture):
    resolved = run(fixture)

    def revoke(req):
        if req.url.path == "/library/metadata/20":
            fixture[0].execute("UPDATE household_memberships SET state='removed'")

    with pytest.raises(source.PlaybackSourceError):
        remote(fixture, resolved, callback=revoke)


def test_remote_noop_receipt_and_personal_selection_changes_preserve_authority(fixture):
    resolved = run(fixture)
    fixture[0].execute("UPDATE media_curator_receipt_latest SET receipt_hash='refreshed'")
    fixture[0].commit()
    assert remote(fixture, resolved, selected_flags=True, detail_extra=True) is resolved
    assert resolved.streams[0].selected is False


@pytest.mark.parametrize(
    "kwargs",
    [
        {"metadata_status": 401},
        {"metadata_status": 403},
        {"metadata_status": 404},
        {"metadata_missing_part": True},
    ],
)
def test_remote_exact_metadata_revocation_or_missing_part_fails_closed(fixture, kwargs):
    resolved = run(fixture)
    with pytest.raises(source.PlaybackSourceError):
        remote(fixture, resolved, **kwargs)
