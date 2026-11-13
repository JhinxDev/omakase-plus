"""Language dispatch integration with real SQLite contracts and controlled providers."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from omakase.plus import households as hh
from omakase.plus import media_sync as sync
from omakase.plus.acquisition_policy import AcquisitionPolicy
from omakase.plus.catalog import rank_candidates
from omakase.plus.language_policy import DomainLanguagePolicies, LanguageRule
from omakase.plus.language_store import load_request, record_request
from omakase.plus.media_types import MediaCandidate, MediaSyncResult
from tests.plus import test_language_store as helpers
from tests.plus.test_language_store import STAMP, current_generation, record_a_probe, snapshot


@pytest.fixture
def setup(tmp_path):
    yield from helpers.setup.__wrapped__(tmp_path)


class ForbiddenProvider:
    def __getattr__(self, name):
        raise AssertionError("No provider method may be accessed: " + name)


def monitor_scope(data):
    db = data[0]
    db.execute(
        "UPDATE media_monitors SET release_status='FINISHED',expected_episodes=1,aired_episodes=1 WHERE id=1"
    )
    return db.execute("SELECT * FROM media_monitors WHERE id=1").fetchone()


def request(data, name, rule, user=2):
    snap = snapshot(data, user, name, rule)
    record_request(data[0], snap, required_episodes=(1,), intent="acquire")
    return snap


def dispatch(data, snap, **kwargs):
    return asyncio.run(
        sync.reconcile_monitor(
            data[0],
            monitor_id=1,
            rd=ForbiddenProvider(),
            now=STAMP,
            language_request_id=snap.request_id,
            language_actor_user_id=snap.actor_user_id,
            language_source_generation=current_generation(data),
            **kwargs,
        )
    )


def test_actual_monitor_path_reuses_existing_multitrack_file_without_provider_or_selection(setup):
    db = setup[0]
    monitor_scope(setup)
    record_a_probe(setup)
    snap = request(
        setup,
        "reuse-fr",
        LanguageRule(audio_required=("fr",), subtitle_required=("fr",), subtitles_mode="required"),
    )
    before = [tuple(r) for r in db.execute("SELECT * FROM media_assets")]
    result = dispatch(setup, snap)
    assert result.outcome == "language_reused" and result.covered == frozenset({1})
    assert not result.accepted_assets
    assert [tuple(r) for r in db.execute("SELECT * FROM media_assets")] == before


def test_actual_monitor_path_preserves_incompatible_existing_copy(setup):
    db = setup[0]
    monitor_scope(setup)
    record_a_probe(setup)
    snap = request(setup, "missing-de", LanguageRule(audio_required=("de",)))
    before = [tuple(r) for r in db.execute("SELECT * FROM media_assets")]
    result = dispatch(setup, snap, language_contract_version="language-policy-v1")
    assert result.outcome == "language_unavailable"
    assert [tuple(r) for r in db.execute("SELECT * FROM media_assets")] == before


def test_missing_snapshot_or_uncapable_consumer_stops_before_provider(setup, monkeypatch):
    db = setup[0]
    monitor = monitor_scope(setup)
    policy = AcquisitionPolicy(languages=DomainLanguagePolicies.new_defaults())
    monkeypatch.setattr(sync, "load_user_acquisition_policy", lambda *args: policy)
    result = asyncio.run(
        sync.reconcile_monitor(db, monitor_id=1, rd=ForbiddenProvider(), now=STAMP)
    )
    assert result.outcome == "language_request_required"
    result = asyncio.run(
        sync.reconcile_candidates(
            db, monitor, (), rd=ForbiddenProvider(), searched_aliases=(), now=STAMP, policy=policy
        )
    )
    assert result.outcome == "language_pipeline_unsupported"
    db.execute("DELETE FROM media_assets")
    snap = request(setup, "no-publisher", LanguageRule(audio_required=("fr",)))
    assert dispatch(setup, snap).outcome == "language_pipeline_unsupported"


def test_only_marked_legacy_monitor_keeps_pre_language_english_job(setup, monkeypatch):
    db = setup[0]
    monitor_scope(setup)
    db.execute("INSERT INTO legacy_language_monitors(monitor_id) VALUES(1)")
    policy = AcquisitionPolicy(languages=DomainLanguagePolicies.new_defaults())
    monkeypatch.setattr(sync, "load_user_acquisition_policy", lambda *args: policy)
    monkeypatch.setattr(sync, "_refresh_existing_assets", AsyncMock())
    monkeypatch.setattr(sync, "configured_catalogs", lambda *args: SimpleNamespace(sources=()))
    monkeypatch.setattr(sync, "capabilities", lambda *args: SimpleNamespace(kind="realdebrid"))
    monkeypatch.setattr(sync, "search_alias_windows", AsyncMock(return_value=()))
    monkeypatch.setattr(sync, "list_items", AsyncMock(return_value=()))
    result = MediaSyncResult(1, 0, "covered", frozenset({1}), frozenset({1}), (), "existing")
    worker = AsyncMock(return_value=result)
    monkeypatch.setattr(sync, "_reconcile_candidates_active", worker)
    returned = asyncio.run(
        sync.reconcile_monitor(db, monitor_id=1, rd=ForbiddenProvider(), now=STAMP)
    )
    assert worker.call_args.kwargs["policy"].languages is None
    assert "legacy English publication" in returned.detail
    assert policy.languages is not None


def test_revocation_during_search_stops_before_any_provider_action(setup, monkeypatch):
    db, lib, *_ = setup
    monitor_scope(setup)
    db.execute("DELETE FROM media_assets")
    snap = request(setup, "revoked-during-query", LanguageRule(audio_required=("fr",)))
    monkeypatch.setattr(sync, "_refresh_existing_assets", AsyncMock())
    monkeypatch.setattr(sync, "configured_catalogs", lambda *args: SimpleNamespace(sources=()))
    monkeypatch.setattr(sync, "capabilities", lambda *args: SimpleNamespace(kind="realdebrid"))

    async def search(*args, **kwargs):
        hh.leave_or_revoke(db, hh.resolve_scope(db, viewer_id=1, library_id=lib), member_id=2)
        return ()

    monkeypatch.setattr(sync, "search_alias_windows", search)
    result = dispatch(setup, snap, language_contract_version="language-policy-v1")
    assert result.outcome == "language_permissions_changed"


def test_only_explicit_advertised_hints_affect_new_language_ranking():
    base = MediaCandidate(
        "a" * 40,
        "Synthetic MULTI 1080p",
        "magnet:?xt=urn:btih:" + "a" * 40,
        10,
        100,
        "1080p",
        1,
        frozenset({1}),
    )
    french = replace(base, torrent_hash="b" * 40, advertised_audio_languages=("fra",))
    english = replace(base, torrent_hash="c" * 40, advertised_audio_languages=("eng",))
    assert base.advertised_audio_languages == ()
    policies = replace(
        DomainLanguagePolicies.new_defaults(), anime=LanguageRule(audio_preferred=("fr", "en"))
    )
    policy = AcquisitionPolicy(languages=policies)
    assert rank_candidates((base, english, french), policy=policy) == (french, english, base)
    assert rank_candidates((base, english, french), policy=AcquisitionPolicy()) == (
        base,
        french,
        english,
    )


def test_supported_dispatch_acquires_then_awaits_exact_file_proof_with_stable_owner_contract(
    setup, monkeypatch
):
    import json

    from omakase.plus import language_store as store
    from omakase.plus.catalog import AliasCatalogWindow
    from omakase.plus.media_probe import persist_probe_result, probe_media_file
    from omakase.plus.realdebrid import RDFile
    from tests.plus.test_language_store import runner
    from tests.plus.test_media_sync import _downloaded, _rd

    db, _, _, _, root, path, fp, _ = setup
    monitor_scope(setup)
    db.execute("DELETE FROM media_assets")
    snap = request(
        setup,
        "acquire-fr",
        LanguageRule(audio_required=("fr",), subtitle_required=("fr",), subtitles_mode="required"),
    )
    rd = _rd()
    rd.add_magnet.return_value = "synthetic-language"
    rd.get_torrent.return_value = _downloaded(
        "synthetic-language", RDFile(1, path.name, 100, True), filename="Exact Anime Season 1"
    )
    candidate = MediaCandidate(
        "c" * 40,
        "Exact Anime S01E01 [1080p][Multi-Subs]",
        "magnet:?xt=urn:btih:" + "c" * 40,
        10,
        100,
        "1080p",
        100,
        frozenset({1}),
        search_alias="Exact Anime",
        advertised_audio_languages=("fra",),
        advertised_subtitle_languages=("fra",),
    )
    monkeypatch.setattr(sync, "_refresh_existing_assets", AsyncMock())
    monkeypatch.setattr(sync, "configured_catalogs", lambda *args: SimpleNamespace(sources=()))
    monkeypatch.setattr(
        sync,
        "search_alias_windows",
        AsyncMock(
            return_value=(
                AliasCatalogWindow(
                    alias="Exact Anime", candidates=(candidate,), provider_complete=True
                ),
            )
        ),
    )
    result = asyncio.run(
        sync.reconcile_monitor(
            db,
            monitor_id=1,
            rd=rd,
            now=STAMP,
            language_request_id=snap.request_id,
            language_actor_user_id=2,
            language_source_generation=current_generation(setup),
            language_contract_version="language-policy-v1",
        )
    )
    assert result.outcome == "language_pending_verification"
    assert len(result.accepted_assets) == 1
    rd.add_magnet.assert_awaited_once()
    rd.delete_torrent.assert_not_awaited()
    asset = result.accepted_assets[0].id
    contract = db.execute("SELECT * FROM publication_language_contracts").fetchone()
    assert json.loads(contract["policy_json"])["subtitle_required"] == ["en"]
    assert json.loads(contract["policy_json"])["audio_required"] == []
    assert db.execute("SELECT COUNT(*) FROM language_request_assets").fetchone()[0] == 1
    assert (
        store.request_language_status(
            db,
            actor_user_id=2,
            request_id=snap.request_id,
            source_generation=current_generation(setup),
        )["language_coverage_complete"]
        is False
    )
    probe = probe_media_file(root, path.name, inventory_fingerprint=fp, runner=runner)
    persist_probe_result(
        db,
        asset_id=asset,
        file_locator=path.name,
        verified_at=STAMP,
        result=probe,
        language_request_id=snap.request_id,
        language_actor_user_id=2,
        observed_episodes=(1,),
        source_generation=current_generation(setup),
    )
    assert (
        store.request_language_status(
            db,
            actor_user_id=2,
            request_id=snap.request_id,
            source_generation=current_generation(setup),
        )["language_coverage_complete"]
        is True
    )
    assert not any(t.selected for t in probe.language_evidence.tracks)
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_direct_candidate_dispatch_refuses_wrong_monitor_before_provider(setup):
    db = setup[0]
    monitor_scope(setup)
    snap = request(setup, "bound-monitor", LanguageRule(audio_required=("fr",)))
    _, policy, _ = load_request(db, actor_user_id=2, request_id=snap.request_id)
    db.execute(
        "INSERT INTO media_monitors(id,user_id,anilist_id,title,expected_episodes,release_status) VALUES(2,1,22,'Other',1,'FINISHED')"
    )
    other = db.execute("SELECT * FROM media_monitors WHERE id=2").fetchone()
    with pytest.raises(ValueError, match="bound to this monitor"):
        asyncio.run(
            sync.reconcile_candidates(
                db,
                other,
                (),
                rd=ForbiddenProvider(),
                searched_aliases=(),
                now=STAMP,
                policy=policy,
                language_dispatch=sync.LanguageDispatch(
                    snap.request_id, 2, current_generation(setup), "language-policy-v1"
                ),
            )
        )
