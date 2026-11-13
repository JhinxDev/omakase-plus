from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CURATOR_ROOT = REPO_ROOT / "deploy" / "plex-curator"
sys.path.insert(0, str(CURATOR_ROOT))

from curator_runtime import (  # noqa: E402
    ApprovedRunPlan,
    CollectionDecision,
    CollectionPlan,
    CuratorBusyError,
    PlexScanBusyError,
    RuntimeContractError,
    _apply_collection_plan,
    _build_approved_plan,
    _build_collection_plan,
    _held_title_coverage,
    _live,
    _overlay_scan_targets,
    _probe_plex_paths,
    _receipt,
    _require_idle_plex,
    _require_readable_plex_paths,
    _sync_library_snapshot,
    _title_coverage,
    curator_lock,
    load_runtime_config,
    main,
    read_approved_plan,
    write_approved_plan,
)
from plex_client import (  # noqa: E402
    PlexAnimeItem,
    PlexInventoryChangedError,  # noqa: E402
    PlexLibraryScan,
    PlexSection,
    plan_library_topology,
)
from plex_curator import (  # noqa: E402
    BlockedAsset,
    CuratorConfig,
    CuratorValidationError,
    OverlayApplyResult,
    PublishedTitle,
    build_overlay_plan,
)
from subtitle_support import SubtitleProbe, SubtitleTrack  # noqa: E402


def test_stale_episode_read_propagates_past_removed_title_identity_fallback():
    class Client:
        def inventory_anime(self, section_key):
            return (PlexAnimeItem("14809", "Removed fixture", (), ()),)

        def series_media_paths(self, rating_key):
            raise PlexInventoryChangedError("fresh inventory required")

    with pytest.raises(PlexInventoryChangedError):
        _build_collection_plan(Client(), {"titles": []}, 1, {}, organization={"titles": []})


@pytest.mark.parametrize("stage", ["planning", "final_snapshot"])
def test_stale_plex_inventory_defers_without_false_mutation_or_absence_claim(
    stage, monkeypatch, tmp_path, capsys
):
    from contextlib import nullcontext
    from types import SimpleNamespace

    config = load_runtime_config(CURATOR_ROOT / "config.example.json")
    calls = []
    monkeypatch.setattr("curator_runtime.load_runtime_config", lambda _: config)
    monkeypatch.setattr("curator_runtime._machine_token", lambda: "fixture")
    monkeypatch.setattr("curator_runtime.curator_lock", lambda _: nullcontext())

    def prepare(*args):
        calls.append("planning")
        if stage == "planning":
            raise PlexInventoryChangedError("fresh inventory required")
        return SimpleNamespace(plan=SimpleNamespace(plan_hash="fixture"))

    def live(*args, **kwargs):
        calls.append("live")
        return SimpleNamespace(plan_hash="fixture", changed=True)

    def snapshot(*args):
        calls.append("snapshot")
        raise PlexInventoryChangedError("fresh inventory required")

    monkeypatch.setattr("curator_runtime._prepare_dry_run", prepare)
    monkeypatch.setattr("curator_runtime._live", live)
    monkeypatch.setattr("curator_runtime._sync_library_snapshot", snapshot)
    assert (
        main(
            [
                "auto",
                "--config",
                str(tmp_path / "config.json"),
                "--plan",
                str(tmp_path / "plan.json"),
                "--backup-receipt",
                str(tmp_path / "backup.json"),
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["code"] == "PLEX_INVENTORY_CHANGED" and result["outcome"] == "deferred"
    assert result["retryable"] is True and "changed" not in result
    assert calls == (["planning"] if stage == "planning" else ["planning", "live", "snapshot"])


def test_public_example_does_not_preselect_an_administrators_library_titles():
    config = load_runtime_config(CURATOR_ROOT / "config.example.json")
    entries = config.collection_overrides

    assert entries == {}
    assert config.filesystem.trusted_allowlist == ()
    assert config.filesystem.plex_match_hints == ()


def _manifest() -> dict:
    return {
        "schema_version": "manifest-v2",
        "generated_at": "2026-07-15T18:22:11Z",
        "titles": [
            {
                "anilist_id": 42,
                "canonical_title": "Fixture Anime",
                "title_aliases": [],
                "genres": ["Action"],
                "format": "TV",
                "season_number": 1,
                "expected_episode_count": 1,
                "aired_episode_count": 1,
                "coverage": {
                    "required_episodes": [1],
                    "usable_episodes": [1],
                    "plex_visible_episodes": [],
                    "required_count": 1,
                    "usable_count": 1,
                    "plex_visible_count": 0,
                },
                "assets": [
                    {
                        "torrent_hash": "A" * 40,
                        "source_folder": "Fixture.Batch",
                        "storage_backend": "zurg",
                        "storage_locator": "Fixture.Batch",
                        "source_title": "Fixture Anime",
                        "quality": "1080p",
                        "episode_coverage": [1],
                    }
                ],
            }
        ],
    }


def _approved_plan(tmp_path: Path) -> ApprovedRunPlan:
    source = tmp_path / "zurg" / "Fixture.Batch"
    source.mkdir(parents=True)
    (source / "Fixture Anime S01E01.mkv").write_bytes(b"episode")
    overlay = build_overlay_plan(
        CuratorConfig(source_root=tmp_path / "zurg", overlay_root=tmp_path / "overlay"),
        _manifest(),
        subtitle_probe=lambda _: SubtitleProbe(
            duration_ms=60_000,
            tracks=(SubtitleTrack(index=2, codec="subrip", language="eng"),),
        ),
        require_english_subtitles=True,
    )
    topology = plan_library_topology(
        (
            PlexSection(1, "Anime", ("/old/anime",)),
            PlexSection(2, "Anime Movies", ("/old/movies",)),
            PlexSection(3, "Real-Debrid", ("/zurg/__all__",)),
            PlexSection(5, "Riven TV", ("/library/symlinks/shows",)),
        ),
        current_overlay="/library/symlinks/omakase/current",
    )
    return ApprovedRunPlan.create(
        config_digest="b" * 64,
        manifest_generated_at="2026-07-15T18:22:11Z",
        overlay_plan=overlay,
        topology_plan=topology,
        collection_plan=CollectionPlan.create(
            (
                CollectionDecision(
                    rating_key="42",
                    title="Fixture Anime",
                    existing_collections=(),
                    desired_collection="Action & Adventure Anime",
                ),
            )
        ),
    )


def test_collection_plan_hash_binds_exact_current_and_proposed_state():
    class FakeClient:
        def inventory_anime(self, section_key):
            assert section_key == 1
            return (PlexAnimeItem("42", "Fixture Anime", (), ("Comedy & Couch Anime",)),)

    plan = _build_collection_plan(FakeClient(), _manifest(), 1, {})
    restored = CollectionPlan.from_dict(plan.to_dict())

    assert restored == plan
    assert restored.decisions[0].existing_collections == ("Comedy & Couch Anime",)
    assert restored.decisions[0].desired_collection == "Action & Adventure Anime"


def test_collection_plan_merges_season_identities_when_their_bucket_agrees():
    manifest = _manifest()
    collision = json.loads(json.dumps(manifest["titles"][0]))
    collision["anilist_id"] = 43
    collision["canonical_title"] = "Different Series"
    collision["title_aliases"] = ["Fixture Anime"]
    manifest["titles"].append(collision)

    class FakeClient:
        def inventory_anime(self, section_key):
            return (PlexAnimeItem("42", "Fixture Anime", (), ()),)

    plan = _build_collection_plan(FakeClient(), manifest, 1, {})

    assert plan.decisions[0].desired_collection == "Action & Adventure Anime"


def test_collection_plan_blocks_merged_seasons_with_conflicting_buckets():
    manifest = _manifest()
    collision = json.loads(json.dumps(manifest["titles"][0]))
    collision["anilist_id"] = 43
    collision["canonical_title"] = "Different Series"
    collision["title_aliases"] = ["Fixture Anime"]
    collision["genres"] = ["Comedy"]
    manifest["titles"].append(collision)

    class FakeClient:
        def inventory_anime(self, section_key):
            return (PlexAnimeItem("42", "Fixture Anime", (), ()),)

    with pytest.raises(
        RuntimeContractError,
        match="Fixture Anime.*AniList IDs 42, 43",
    ):
        _build_collection_plan(FakeClient(), manifest, 1, {})


def test_collection_plan_recovers_identity_from_overlay_path_after_bad_plex_match():
    class FakeClient:
        def inventory_anime(self, section_key):
            return (PlexAnimeItem("42", "Unrelated Bad Match", (), ()),)

        def series_media_paths(self, rating_key):
            assert rating_key == "42"
            return (
                "/library/symlinks/omakase/current/Anime/"
                "Fixture Anime/Season 01/Fixture Anime - S01E01.mkv",
            )

    plan = _build_collection_plan(FakeClient(), _manifest(), 1, {})

    assert plan.decisions[0].title == "Unrelated Bad Match"
    assert plan.decisions[0].desired_collection == "Action & Adventure Anime"


def test_collection_plan_preserves_stale_managed_title_until_removal_scan():
    class FakeClient:
        def inventory_anime(self, section_key):
            return (
                PlexAnimeItem(
                    "42",
                    "Removed Anime",
                    (),
                    ("Action & Adventure Anime",),
                ),
            )

        def series_media_paths(self, rating_key):
            assert rating_key == "42"
            return (
                "/library/symlinks/omakase/current/Anime/"
                "Removed Anime/Season 01/Removed Anime - S01E01.mkv",
            )

    plan = _build_collection_plan(FakeClient(), _manifest(), 1, {})

    assert [
        (
            decision.title,
            decision.existing_collections,
            decision.desired_collection,
        )
        for decision in plan.decisions
    ] == [
        (
            "Removed Anime",
            ("Action & Adventure Anime",),
            "Action & Adventure Anime",
        )
    ]


def test_collection_plan_rejects_bad_match_with_mixed_overlay_folders():
    class FakeClient:
        def inventory_anime(self, section_key):
            return (PlexAnimeItem("42", "Unrelated Bad Match", (), ()),)

        def series_media_paths(self, rating_key):
            return (
                "/library/symlinks/omakase/current/Anime/"
                "Fixture Anime/Season 01/Fixture Anime - S01E01.mkv",
                "/library/symlinks/omakase/current/Anime/"
                "Different Anime/Season 01/Different Anime - S01E01.mkv",
            )

    with pytest.raises(RuntimeContractError, match="paths are ambiguous"):
        _build_collection_plan(FakeClient(), _manifest(), 1, {})


def test_collection_plan_blocks_unindexed_manifest_with_unresolved_identity():
    manifest = _manifest()
    unresolved = json.loads(json.dumps(manifest["titles"][0]))
    unresolved["anilist_id"] = 43
    unresolved["canonical_title"] = "Unindexed Future Series"
    unresolved["genres"] = []
    manifest["titles"].append(unresolved)

    class FakeClient:
        def inventory_anime(self, section_key):
            return (PlexAnimeItem("42", "Fixture Anime", (), ()),)

    with pytest.raises(RuntimeContractError, match="manifest collection identity"):
        _build_collection_plan(FakeClient(), manifest, 1, {})


def test_collection_plan_allows_empty_bootstrap_inventory_when_override_matches_manifest():
    class FakeClient:
        def inventory_anime(self, section_key):
            return ()

    plan = _build_collection_plan(
        FakeClient(),
        _manifest(),
        1,
        {"Fixture Anime": "Action & Adventure Anime"},
    )

    assert plan.decisions == ()


def test_collection_plan_ignores_dormant_override_that_matches_no_current_title():
    class FakeClient:
        def inventory_anime(self, section_key):
            return ()

    plan = _build_collection_plan(
        FakeClient(),
        _manifest(),
        1,
        {"Dormant Anime": "Action & Adventure Anime"},
    )

    assert plan.decisions == ()


def test_collection_apply_blocks_concurrent_plex_tag_drift_before_mutation():
    plan = CollectionPlan.create(
        (
            CollectionDecision(
                rating_key="42",
                title="Fixture Anime",
                existing_collections=("Comedy & Couch Anime",),
                desired_collection="Action & Adventure Anime",
            ),
        )
    )

    class FakeClient:
        def __init__(self):
            self.mutated = False

        def inventory_anime(self, section_key):
            return (PlexAnimeItem("42", "Fixture Anime", (), ("Favorites",)),)

        def set_managed_collection(self, **kwargs):
            self.mutated = True

    client = FakeClient()
    with pytest.raises(RuntimeContractError, match="changed"):
        _apply_collection_plan(client, section_key=1, plan=plan)
    assert client.mutated is False


def test_example_config_loads_and_contains_no_secret_fields():
    path = REPO_ROOT / "deploy" / "plex-curator" / "config.example.json"
    config = load_runtime_config(path)
    serialized = json.dumps(json.loads(path.read_text(encoding="utf-8"))).casefold()

    assert config.filesystem.source_root == Path("/srv/media/source")
    assert config.plex.overlay_path == "/library/symlinks/omakase"
    assert config.plex.library_names.anime == "Anime"
    assert config.plex.library_names.anime_movies == "Anime Movies"
    assert config.runtime.receipt_url.endswith("/plus/api/curator-receipts")
    assert config.subtitles.ffprobe_path == "/usr/bin/ffprobe"
    assert config.subtitles.require_english_subtitle is True
    assert config.filesystem.plex_match_hints == ()
    assert config.collection_overrides == {}
    assert "token" not in serialized
    assert "secret" not in serialized


def test_sync_library_snapshot_posts_authenticated_inventory_to_sibling_endpoint(
    monkeypatch,
):
    config = load_runtime_config(CURATOR_ROOT / "config.example.json")
    snapshot = {
        "schema_version": "plex-library-snapshot-v1",
        "generated_at": "2026-08-27T15:00:00Z",
        "items": [],
    }
    observed: dict[str, object] = {}

    class FakeClient:
        def library_snapshot(self, *, library_names, generated_at, overlay_path):
            observed["library_names"] = library_names
            observed["generated_at"] = generated_at
            observed["overlay_path"] = overlay_path
            return snapshot

        def close(self):
            observed["closed"] = True

    class Response:
        def raise_for_status(self):
            return None

    def post(url, *, headers, json, timeout):
        observed.update(url=url, headers=headers, json=json, timeout=timeout)
        return Response()

    monkeypatch.setattr("curator_runtime._plex_client", lambda _: FakeClient())
    monkeypatch.setattr("curator_runtime.httpx.post", post)
    monkeypatch.setattr("curator_runtime._utc_now", lambda: "2026-08-27T15:00:00Z")

    _sync_library_snapshot(config, "machine-secret")

    assert observed["url"] == "https://omakase.example.com/plus/api/plex-library-snapshot"
    assert observed["headers"] == {"Authorization": "Bearer machine-secret"}
    assert observed["json"] == snapshot
    assert observed["timeout"] == 30
    assert observed["library_names"] == config.plex.library_names
    assert observed["overlay_path"] == config.plex.overlay_path
    assert observed["closed"] is True


@pytest.mark.parametrize("command", ["live", "auto"])
def test_mutating_runtime_commands_finish_by_syncing_real_library(
    command,
    monkeypatch,
    tmp_path,
):
    from contextlib import nullcontext
    from types import SimpleNamespace

    config = load_runtime_config(CURATOR_ROOT / "config.example.json")
    synced: list[tuple[object, str]] = []
    monkeypatch.setattr("curator_runtime.load_runtime_config", lambda _: config)
    monkeypatch.setattr("curator_runtime._machine_token", lambda: "machine-secret")
    monkeypatch.setattr("curator_runtime.curator_lock", lambda _: nullcontext())
    monkeypatch.setattr(
        "curator_runtime._prepare_dry_run",
        lambda *_: SimpleNamespace(plan=SimpleNamespace(plan_hash="plan-hash")),
    )
    monkeypatch.setattr(
        "curator_runtime._live",
        lambda *args, **kwargs: SimpleNamespace(plan_hash="plan-hash", changed=False),
    )
    monkeypatch.setattr(
        "curator_runtime._sync_library_snapshot",
        lambda runtime, token: synced.append((runtime, token)),
    )

    result = main(
        [
            command,
            "--config",
            str(tmp_path / "config.json"),
            "--plan",
            str(tmp_path / "plan.json"),
            "--backup-receipt",
            str(tmp_path / "receipt.json"),
        ]
    )

    assert result == 0
    assert synced == [(config, "machine-secret")]


def test_config_loads_reviewed_plex_match_hint(tmp_path: Path):
    source = REPO_ROOT / "deploy" / "plex-curator" / "config.example.json"
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["metadata_match_hints"]["entries"] = [
        {
            "canonical_title": "Hell’s Paradise Season 2",
            "season_number": 2,
            "source": "tvdb",
            "source_id": "402474",
        }
    ]
    path = tmp_path / "curator.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    config = load_runtime_config(path)

    assert [hint.to_dict() for hint in config.filesystem.plex_match_hints] == [
        {
            "canonical_title": "Hell’s Paradise Season 2",
            "season_number": 2,
            "source": "tvdb",
            "source_id": "402474",
        }
    ]


def test_runtime_failure_is_structured_and_actionable(tmp_path: Path, capsys):
    result = main(
        [
            "dry-run",
            "--config",
            str(tmp_path / "missing-config.json"),
            "--plan",
            str(tmp_path / "plan.json"),
        ]
    )

    failure = json.loads(capsys.readouterr().err)
    assert result == 1
    assert failure == {
        "code": "CURATOR_FAILED",
        "detail": "curator config is unavailable or invalid",
        "error_type": "RuntimeContractError",
        "outcome": "failed",
    }


def test_active_plex_scan_blocks_curator_with_actionable_detail():
    class FakeClient:
        def active_library_scans(self):
            return (
                PlexLibraryScan(
                    title="Scanning TV Shows",
                    subtitle="The Office - Season 03",
                    progress="66",
                ),
            )

    with pytest.raises(
        PlexScanBusyError,
        match="Scanning TV Shows.*The Office - Season 03.*66.*retry",
    ):
        _require_idle_plex(FakeClient())


@pytest.fixture
def scan_guard_cli_config(tmp_path, monkeypatch):
    base = load_runtime_config(CURATOR_ROOT / "config.example.json")
    config = replace(base, runtime=replace(base.runtime, lock_path=tmp_path / "curator.lock"))
    monkeypatch.setattr("curator_runtime.load_runtime_config", lambda _: config)
    monkeypatch.setattr("curator_runtime._machine_token", lambda: "fixture-machine-token")
    return config


@pytest.mark.parametrize("command", ["dry-run", "live", "auto"])
def test_active_scan_cli_defers_before_plan_or_publication_changes(
    command, scan_guard_cli_config, tmp_path, monkeypatch, capsys
):
    plan = _approved_plan(tmp_path)
    plan_path = tmp_path / "approved-plan.json"
    write_approved_plan(plan_path, plan)
    config = replace(scan_guard_cli_config, config_digest=plan.config_digest)
    monkeypatch.setattr("curator_runtime.load_runtime_config", lambda _: config)
    config.runtime.lock_path.write_bytes(b"0")
    before = {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    class BusyClient:
        closed = False

        def active_library_scans(self):
            return (PlexLibraryScan("Scanning TV Shows", "Fixture season", "66"),)

        def inventory_sections(self):
            raise AssertionError("Busy scan must defer before inventory planning")

        def close(self):
            self.closed = True

    client = BusyClient()
    monkeypatch.setattr("curator_runtime._plex_client", lambda _: client)
    monkeypatch.setattr("curator_runtime._fetch_manifest", lambda *_: _manifest())
    monkeypatch.setattr("curator_runtime._fetch_organization", lambda *_: {"titles": []})

    def forbid_mutation(*args, **kwargs):
        raise AssertionError("Busy scan must not write plans, apply or report publication")

    for name in (
        "write_approved_plan",
        "_build_collection_plan",
        "_build_approved_plan",
        "apply_overlay_plan",
        "_apply_collection_plan",
        "_submit_receipt",
        "_sync_library_snapshot",
    ):
        monkeypatch.setattr("curator_runtime." + name, forbid_mutation)

    argv = [command, "--config", str(tmp_path / "config.json"), "--plan", str(plan_path)]
    if command != "dry-run":
        argv += ["--backup-receipt", str(tmp_path / "backup-receipt.json")]
    result = main(argv)

    output = capsys.readouterr()
    assert result == 0
    assert json.loads(output.out) == {
        "code": "PLEX_SCAN_ACTIVE",
        "outcome": "deferred",
        "mode": command,
        "changed": False,
        "retryable": True,
        "detail": "Plex is scanning its library; retry after the scan completes.",
    }
    assert output.err == ""
    assert client.closed
    assert {
        str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()
    } == before
    with curator_lock(config.runtime.lock_path):
        pass


@pytest.mark.parametrize("transition", ["scan", "idle", "changed"])
def test_auto_second_scan_guard_stages_writes_and_keeps_plan_validation(
    transition, scan_guard_cli_config, tmp_path, monkeypatch, capsys
):
    plan = _approved_plan(tmp_path)
    plan_path = tmp_path / "approved-plan.json"
    write_approved_plan(plan_path, plan)
    config = replace(
        scan_guard_cli_config,
        config_digest=plan.config_digest,
        filesystem=CuratorConfig(source_root=tmp_path / "zurg", overlay_root=tmp_path / "overlay"),
        runtime=replace(scan_guard_cli_config.runtime, state_root=tmp_path / "state"),
    )
    monkeypatch.setattr("curator_runtime.load_runtime_config", lambda _: config)
    config.runtime.lock_path.write_bytes(b"0")
    config.runtime.state_root.mkdir()
    cache_path = config.runtime.state_root / "subtitle-probe-cache-v2.json"
    cache_path.write_bytes(b"previous cache")
    before = {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    sections = (
        PlexSection(1, "Anime", ("/old/anime",)),
        PlexSection(2, "Anime Movies", ("/old/movies",)),
        PlexSection(5, "TV Shows", ("/library/symlinks/shows",)),
        PlexSection(6, "Movies", ("/library/symlinks/movies",)),
    )
    scan_checks = []
    clients = []
    probes = []
    receipts = []
    publications = []

    class Client:
        closed = False

        def active_library_scans(self):
            scan_checks.append(True)
            if transition == "scan" and len(scan_checks) == 2:
                return (PlexLibraryScan("Scanning", "", ""),)
            return ()

        def inventory_sections(self):
            return sections

        def inventory_anime(self, section_key):
            collections = (
                ("Drama & Suspense Anime",)
                if (transition == "changed" and len(scan_checks) == 2)
                else ()
            )
            return (PlexAnimeItem("42", "Fixture Anime", (), collections),)

        def apply_topology(self, *args, **kwargs):
            return ()

        def close(self):
            self.closed = True

    def make_client(_):
        client = Client()
        clients.append(client)
        return client

    def probe(source, **kwargs):
        probes.append(source)
        return SubtitleProbe(
            duration_ms=60_000,
            tracks=(SubtitleTrack(index=2, codec="subrip", language="eng"),),
        )

    def apply_fixture(filesystem, overlay_plan, *, after_swap, **kwargs):
        assert transition == "idle"
        assert len(scan_checks) == 2
        assert read_approved_plan(plan_path).overlay_plan.plan_hash == overlay_plan.plan_hash
        publications.append("apply")
        result = OverlayApplyResult(
            generation=tmp_path / "generation",
            previous_generation=tmp_path / "generation",
            plan_hash=overlay_plan.plan_hash,
            changed=False,
        )
        after_swap(result)
        return result

    monkeypatch.setattr("curator_runtime._plex_client", make_client)
    monkeypatch.setattr("curator_runtime._fetch_manifest", lambda *_: _manifest())
    monkeypatch.setattr("curator_runtime._fetch_organization", lambda *_: {"titles": []})
    monkeypatch.setattr("curator_runtime.probe_subtitles", probe)
    monkeypatch.setattr("curator_runtime._section_observation", lambda *_: (sections, []))
    monkeypatch.setattr("curator_runtime._title_coverage", lambda *args, **kwargs: [])
    monkeypatch.setattr("curator_runtime._submit_receipt", lambda *args: receipts.append(args[-1]))
    monkeypatch.setattr("curator_runtime.apply_overlay_plan", apply_fixture)
    monkeypatch.setattr("curator_runtime._probe_plex_paths", lambda _, paths: paths)
    monkeypatch.setattr(
        "curator_runtime._apply_collection_plan",
        lambda *args, **kwargs: publications.append("collections"),
    )
    monkeypatch.setattr(
        "curator_runtime._sync_library_snapshot", lambda *_: publications.append("snapshot")
    )

    result = main(
        [
            "auto",
            "--config",
            str(tmp_path / "config.json"),
            "--plan",
            str(plan_path),
            "--backup-receipt",
            str(tmp_path / "backup.json"),
        ]
    )

    output = capsys.readouterr()
    assert len(scan_checks) == 2
    assert len(probes) == 1
    assert len(clients) == 2 and all(client.closed for client in clients)
    if transition == "idle":
        assert result == 0
        assert json.loads(output.out)["outcome"] == "success"
        assert output.err == ""
        assert [receipt["mode"] for receipt in receipts] == ["dry-run", "live"]
        assert publications == ["apply", "collections", "snapshot"]
        assert read_approved_plan(plan_path).config_digest == config.config_digest
        assert cache_path.read_bytes() != b"previous cache"
    else:
        if transition == "scan":
            assert result == 0
            assert json.loads(output.out)["outcome"] == "deferred"
            assert json.loads(output.out)["changed"] is False
            assert output.err == ""
        else:
            assert result == 1
            assert output.out == ""
            failure = json.loads(output.err)
            assert failure["code"] == "CURATOR_FAILED"
            assert failure["detail"] == "live state differs from the approved dry-run plan"
        assert receipts == []
        assert publications == []
        assert {
            str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()
        } == before
    with curator_lock(config.runtime.lock_path):
        pass


@pytest.mark.parametrize(
    ("failure", "exit_code", "outcome", "code"),
    [
        (
            RuntimeContractError("Plex library scan is active; invalid contract"),
            1,
            "failed",
            "CURATOR_FAILED",
        ),
        (CuratorValidationError("source inventory is unavailable"), 1, "failed", "CURATOR_FAILED"),
        (OSError("provider mount I/O failure"), 1, "failed", "CURATOR_FAILED"),
        (httpx.ConnectError("provider connection failed"), 1, "failed", "CURATOR_FAILED"),
        (CuratorBusyError("curator already running"), 75, "degraded", "LOCK_HELD"),
    ],
)
def test_scan_deferral_keeps_other_errors_and_lock_failures_distinct(
    failure, exit_code, outcome, code, scan_guard_cli_config, tmp_path, monkeypatch, capsys
):
    def fail(*args):
        raise failure

    monkeypatch.setattr("curator_runtime._prepare_dry_run", fail)
    result = main(
        [
            "auto",
            "--config",
            str(tmp_path / "config.json"),
            "--plan",
            str(tmp_path / "plan.json"),
            "--backup-receipt",
            str(tmp_path / "backup-receipt.json"),
        ]
    )

    output = capsys.readouterr()
    assert result == exit_code
    assert output.out == ""
    failure_receipt = json.loads(output.err)
    assert failure_receipt["outcome"] == outcome
    assert failure_receipt["code"] == code


def test_approved_plan_binds_cached_exact_file_subtitle_audit(tmp_path: Path, monkeypatch):
    source = tmp_path / "zurg" / "Fixture.Batch"
    source.mkdir(parents=True)
    (source / "Fixture Anime S01E01.mkv").write_bytes(b"episode")
    base = load_runtime_config(REPO_ROOT / "deploy" / "plex-curator" / "config.example.json")
    config = replace(
        base,
        filesystem=CuratorConfig(
            source_root=tmp_path / "zurg",
            overlay_root=tmp_path / "overlay",
        ),
        runtime=replace(base.runtime, state_root=tmp_path / "state"),
    )
    calls: list[Path] = []

    def probe(path, **kwargs):
        calls.append(path)
        return SubtitleProbe(
            duration_ms=60_000,
            tracks=(SubtitleTrack(index=2, codec="subrip", language="eng"),),
        )

    monkeypatch.setattr("curator_runtime.probe_subtitles", probe)
    sections = (
        PlexSection(1, "Anime", ("/old/anime",)),
        PlexSection(2, "Anime Movies", ("/old/movies",)),
        PlexSection(5, "TV Shows", ("/library/symlinks/shows",)),
        PlexSection(6, "Movies", ("/library/symlinks/movies",)),
    )

    first = _build_approved_plan(
        config,
        _manifest(),
        sections,
        CollectionPlan.create(()),
    )
    monkeypatch.setattr(
        "curator_runtime.probe_subtitles",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("cache miss")),
    )
    second = _build_approved_plan(
        config,
        _manifest(),
        sections,
        CollectionPlan.create(()),
    )

    assert len(calls) == 1
    assert len(first.overlay_plan.embedded_subtitles) == 1
    assert first.overlay_plan.plan_hash == second.overlay_plan.plan_hash
    assert (tmp_path / "state" / "subtitle-probe-cache-v2.json").is_file()


def test_failed_plan_persists_successful_probe_evidence(tmp_path: Path, monkeypatch):
    source = tmp_path / "zurg" / "Fixture.Batch"
    source.mkdir(parents=True)
    media = source / "Fixture Anime S01E01.mkv"
    media.write_bytes(b"episode")
    base = load_runtime_config(REPO_ROOT / "deploy" / "plex-curator" / "config.example.json")
    config = replace(
        base,
        filesystem=CuratorConfig(
            source_root=tmp_path / "zurg",
            overlay_root=tmp_path / "overlay",
        ),
        runtime=replace(base.runtime, state_root=tmp_path / "state"),
    )
    monkeypatch.setattr(
        "curator_runtime.probe_subtitles",
        lambda *args, **kwargs: SubtitleProbe(
            duration_ms=60_000,
            tracks=(SubtitleTrack(index=2, codec="subrip", language="eng"),),
        ),
    )

    def fail_after_probe(*args, subtitle_probe, **kwargs):
        subtitle_probe(media)
        raise CuratorValidationError("later media failed")

    monkeypatch.setattr("curator_runtime.build_overlay_plan", fail_after_probe)

    with pytest.raises(CuratorValidationError, match="later media"):
        _build_approved_plan(config, _manifest(), (), CollectionPlan.create(()))

    cache = json.loads(
        (tmp_path / "state" / "subtitle-probe-cache-v2.json").read_text(encoding="utf-8")
    )
    assert str(media) in cache["entries"]


def test_title_coverage_matches_merged_series_and_counts_the_exact_season():
    manifest = {
        "titles": [
            {
                "anilist_id": 104276,
                "canonical_title": "My Hero Academia Season 4",
                "title_aliases": ["Boku no Hero Academia 4", "MHA 4"],
                "format": "TV",
                "season_number": 4,
                "coverage": {"usable_count": 25},
            },
            {
                "anilist_id": 207674,
                "canonical_title": "KAMUI ---He's behind you",
                "title_aliases": ["Ushiro no Shoumen Kamui-san"],
                "format": "TV",
                "season_number": 1,
                "coverage": {"usable_count": 2},
            },
        ]
    }

    class FakeClient:
        def inventory_anime(self, section_key):
            assert section_key == 1
            return (
                PlexAnimeItem("42", "My Hero Academia", (), ()),
                PlexAnimeItem("43", "KAMUI: He's Behind You", (), ()),
            )

        def season_episode_count(self, rating_key, season):
            return {("42", 4): 25, ("43", 1): 2}[(rating_key, season)]

    coverage = _title_coverage(
        FakeClient(),
        manifest,
        (PlexSection(1, "Anime", ("/anime",)),),
    )

    assert [item["plex_visible_count"] for item in coverage] == [25, 2]


def test_title_coverage_counts_a_movie_from_the_movie_section():
    manifest = {
        "titles": [
            {
                "anilist_id": 617,
                "canonical_title": "Ninja Scroll",
                "title_aliases": ["Juubee Ninpuuchou"],
                "format": "MOVIE",
                "season_number": None,
                "coverage": {"usable_count": 1},
            }
        ]
    }

    class FakeClient:
        def inventory_anime(self, section_key):
            assert section_key == 2
            return (PlexAnimeItem("7", "Ninja Scroll", (), ()),)

        def item_media_paths(self, rating_key):
            assert rating_key == "7"
            return ("/library/symlinks/omakase/current/Anime Movies/Ninja Scroll/movie.mkv",)

    coverage = _title_coverage(
        FakeClient(),
        manifest,
        (PlexSection(2, "Anime Movies", ("/anime-movies",)),),
    )

    assert coverage[0]["section"] == "Anime Movies"
    assert coverage[0]["plex_visible_count"] == 1


def test_signed_title_visibility_uses_exact_published_episode_paths_after_plex_rename():
    manifest = {
        "titles": [
            {
                "anilist_id": 185874,
                "canonical_title": "Calamity",
                "title_aliases": [],
                "format": "TV",
                "season_number": 2,
                "coverage": {"usable_count": 8},
            }
        ]
    }
    publication = PublishedTitle(185874, "Anime", 2, tuple(range(1, 9)), tuple(range(1, 9)))

    class FakeClient:
        def inventory_anime(self, _):
            return (PlexAnimeItem("42", "Bleach", (), ()),)

        def season_episode_paths(self, key, season):
            assert (key, season) == ("42", 2)
            return (
                (
                    1,
                    "/library/symlinks/omakase/current/Anime/Calamity/Season 02/Calamity - S02E01.mkv",
                ),
                (
                    4,
                    "/library/symlinks/omakase/current/Anime/Calamity/Season 02/Calamity - S02E04.mkv",
                ),
                (
                    4,
                    "/library/symlinks/omakase/current/Anime/Calamity/Season 02/Calamity - S02E04.mkv",
                ),
                (5, "/unmanaged/Calamity/Season 02/Calamity - S02E05.mkv"),
                (6, "/library/symlinks/omakase/current/Anime/Other/Season 02/Other - S02E06.mkv"),
                (
                    9,
                    "/library/symlinks/omakase/current/Anime/Calamity/Season 02/Calamity - S02E09.mkv",
                ),
            )

    coverage = _title_coverage(
        FakeClient(),
        manifest,
        (PlexSection(1, "Anime", ("/anime",)),),
        published_titles=(publication,),
    )
    assert coverage[0]["plex_visible_count"] == 2


def test_title_coverage_uses_movie_folder_when_plex_changes_the_display_title():
    manifest = {
        "titles": [
            {
                "anilist_id": 8888,
                "canonical_title": "Code Geass: Akito the Exiled - The Wyvern Arrives",
                "title_aliases": ["Code Geass: Boukoku no Akito 1"],
                "format": "MOVIE",
                "season_number": None,
                "coverage": {"usable_count": 1},
            }
        ]
    }

    class FakeClient:
        def inventory_anime(self, section_key):
            assert section_key == 2
            return (
                PlexAnimeItem(
                    "7",
                    "Code Geass: Akito the Exiled 1: The Wyvern Arrives",
                    (),
                    (),
                ),
            )

        def item_media_paths(self, rating_key):
            assert rating_key == "7"
            return (
                "/library/symlinks/omakase/current/Anime Movies/"
                "Code Geass: Akito the Exiled - The Wyvern Arrives/movie.mkv",
            )

    coverage = _title_coverage(
        FakeClient(),
        manifest,
        (PlexSection(2, "Anime Movies", ("/anime-movies",)),),
    )

    assert coverage[0]["plex_visible_count"] == 1


def test_plex_path_probe_rejects_overlay_directories_with_a_broken_link(monkeypatch):
    config = load_runtime_config(REPO_ROOT / "deploy" / "plex-curator" / "config.example.json")
    paths = (
        "/library/symlinks/omakase/current/Anime",
        "/library/symlinks/omakase/current/Anime Movies",
        "/library/symlinks/shows",
        "/library/symlinks/movies",
    )
    results = iter(
        [subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"") for _ in paths]
        + [
            subprocess.CompletedProcess(
                [],
                0,
                stdout=b"/library/symlinks/omakase/current/Anime/Broken.mkv\n",
                stderr=b"",
            )
        ]
    )
    monkeypatch.setattr("curator_runtime.subprocess.run", lambda *args, **kwargs: next(results))

    assert _probe_plex_paths(config, paths) == (
        "/library/symlinks/shows",
        "/library/symlinks/movies",
    )


def test_plex_path_preflight_names_every_unreadable_path(monkeypatch):
    config = load_runtime_config(REPO_ROOT / "deploy" / "plex-curator" / "config.example.json")
    paths = (
        "/library/symlinks/omakase/current/Anime",
        "/library/symlinks/omakase/current/Anime Movies",
        "/library/symlinks/shows",
        "/library/symlinks/movies",
    )
    monkeypatch.setattr(
        "curator_runtime._probe_plex_paths",
        lambda *args: (paths[0], paths[3]),
    )

    with pytest.raises(RuntimeContractError) as exc_info:
        _require_readable_plex_paths(config, paths)

    message = str(exc_info.value)
    assert message == (
        f"Plex path preflight found unreadable or broken paths: {paths[1]}, {paths[2]}"
    )


def test_overlay_scan_targets_only_the_series_with_a_new_match_hint(tmp_path: Path):
    previous = tmp_path / "previous"
    generation = tmp_path / "generation"
    for root in (previous, generation):
        (root / "Anime" / "Hell’s Paradise Season 2" / "Season 02").mkdir(parents=True)
        (root / "Anime Movies").mkdir()
        (
            root
            / "Anime"
            / "Hell’s Paradise Season 2"
            / "Season 02"
            / "Hell’s Paradise Season 2 - S02E01.mkv"
        ).symlink_to("/zurg/Hells.Paradise.S02/Episode01.mkv")
    (generation / "Anime" / "Hell’s Paradise Season 2" / ".plexmatch").write_text(
        "tvdbid: 402474\nseason: 2\n",
        encoding="utf-8",
    )
    config = load_runtime_config(REPO_ROOT / "deploy" / "plex-curator" / "config.example.json")
    result = OverlayApplyResult(
        generation=generation,
        previous_generation=previous,
        plan_hash="a" * 64,
        changed=True,
    )

    assert _overlay_scan_targets(config, result) == (
        (
            "Anime",
            "/library/symlinks/omakase/current/Anime/Hell’s Paradise Season 2",
        ),
    )


def test_overlay_scan_falls_back_to_full_section_when_a_series_is_removed(tmp_path: Path):
    previous = tmp_path / "previous"
    generation = tmp_path / "generation"
    (previous / "Anime" / "Removed Anime" / "Season 01").mkdir(parents=True)
    (previous / "Anime Movies").mkdir()
    (generation / "Anime").mkdir(parents=True)
    (generation / "Anime Movies").mkdir()
    config = load_runtime_config(REPO_ROOT / "deploy" / "plex-curator" / "config.example.json")
    result = OverlayApplyResult(
        generation=generation,
        previous_generation=previous,
        plan_hash="a" * 64,
        changed=True,
    )

    assert _overlay_scan_targets(config, result) == (("Anime", None),)


def test_unchanged_live_run_still_converges_anime_collections(tmp_path: Path, monkeypatch):
    plan = _approved_plan(tmp_path)
    plan_path = tmp_path / "approved-plan.json"
    write_approved_plan(plan_path, plan)
    base = load_runtime_config(REPO_ROOT / "deploy" / "plex-curator" / "config.example.json")
    config = replace(
        base,
        runtime=replace(base.runtime, state_root=tmp_path / "state"),
        collection_overrides={},
        config_digest=plan.config_digest,
    )
    sections = (
        PlexSection(1, "Anime", ("/library/symlinks/omakase/current/Anime",)),
        PlexSection(2, "Anime Movies", ("/library/symlinks/omakase/current/Anime Movies",)),
        PlexSection(3, "Real-Debrid", ("/zurg/__all__",)),
        PlexSection(5, "TV Shows", ("/library/symlinks/shows",)),
        PlexSection(6, "Movies", ("/library/symlinks/movies",)),
    )

    class FakeClient:
        def __init__(self):
            self.collection_calls: list[int] = []

        def active_library_scans(self):
            return ()

        def inventory_sections(self):
            return sections

        def inventory_anime(self, section_key):
            return (PlexAnimeItem("42", "Fixture Anime", (), ()),)

        def apply_topology(self, *args, **kwargs):
            return ()

        def set_managed_collection(self, *, section_key, **kwargs):
            self.collection_calls.append(section_key)
            return None

        def close(self):
            return None

    client = FakeClient()
    result = OverlayApplyResult(
        generation=tmp_path / "generation",
        previous_generation=tmp_path / "generation",
        plan_hash=plan.overlay_plan.plan_hash,
        changed=False,
    )

    cache_calls: list[Path] = []

    class FakeProbeCache:
        def __init__(self, path, *, probe):
            assert path == tmp_path / "state" / "subtitle-probe-cache-v2.json"

        def probe(self, source):
            cache_calls.append(source)
            return SubtitleProbe(
                duration_ms=60_000,
                tracks=(SubtitleTrack(index=2, codec="subrip", language="eng"),),
            )

    def apply_without_change(*args, after_swap, embedded_subtitle_validator, **kwargs):
        binding = plan.overlay_plan.embedded_subtitles[0]
        embedded_subtitle_validator(
            config.filesystem.source_root / Path(binding.source),
            binding,
        )
        after_swap(result)
        return result

    monkeypatch.setattr("curator_runtime._fetch_manifest", lambda *args: _manifest())
    monkeypatch.setattr("curator_runtime._fetch_organization", lambda *args: {"titles": []})
    monkeypatch.setattr("curator_runtime._plex_client", lambda *args: client)
    monkeypatch.setattr("curator_runtime._build_approved_plan", lambda *args: plan)
    monkeypatch.setattr(
        "curator_runtime._probe_plex_paths", lambda *args: plan.topology_plan.intended_paths
    )
    monkeypatch.setattr("curator_runtime._section_observation", lambda *args: (sections, []))
    monkeypatch.setattr("curator_runtime._title_coverage", lambda *args, **kwargs: [])
    monkeypatch.setattr("curator_runtime._receipt", lambda *args, **kwargs: {})
    monkeypatch.setattr("curator_runtime._submit_receipt", lambda *args: None)
    monkeypatch.setattr("curator_runtime.SubtitleProbeCache", FakeProbeCache)
    monkeypatch.setattr(
        "curator_runtime.probe_subtitles",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("raw reprobe")),
    )
    monkeypatch.setattr("curator_runtime.apply_overlay_plan", apply_without_change)

    _live(config, plan_path, tmp_path / "backup.json", "machine-token", None)

    assert client.collection_calls == [1]
    assert cache_calls == [
        config.filesystem.source_root / "Fixture.Batch" / "Fixture Anime S01E01.mkv"
    ]


def test_approved_plan_is_mode_0600_atomic_and_tamper_evident(tmp_path: Path):
    plan = _approved_plan(tmp_path)
    path = tmp_path / "state" / "approved-plan.json"

    write_approved_plan(path, plan)

    assert read_approved_plan(path) == plan
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["manifest_generated_at"] = "2026-07-15T18:37:11Z"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="hash"):
        read_approved_plan(path)


def test_global_curator_lock_refuses_a_concurrent_run(tmp_path: Path):
    lock_path = tmp_path / "curator.lock"

    with curator_lock(lock_path):
        with pytest.raises(CuratorBusyError):
            with curator_lock(lock_path):
                pass


def test_receipt_reports_success_when_nothing_is_blocked_and_topology_converged(
    tmp_path: Path,
):
    plan = _approved_plan(tmp_path)
    sections = (
        PlexSection(1, "Anime", ()),
        PlexSection(2, "Anime Movies", ()),
        PlexSection(5, "TV Shows", ()),
        PlexSection(6, "Movies", ()),
    )

    receipt = _receipt(
        plan=plan,
        mode="live",
        started_at="2026-08-04T00:00:00Z",
        sections=sections,
        observed=[{"title": "Anime", "count": 1}],
        title_coverage=[],
    )

    assert receipt["outcome"] == "success"
    assert receipt["blocked_assets"] == []
    assert receipt["topology_converged"] is True
    assert receipt["schema_version"] == "curator-receipt-v2"


def test_receipt_reports_degraded_with_retry_evidence_when_an_asset_is_blocked(
    tmp_path: Path,
):
    plan = _approved_plan(tmp_path)
    blocked = BlockedAsset(
        anilist_id=42,
        section="Anime",
        season_number=1,
        torrent_hash="A" * 40,
        affected_episodes=(1,),
        code="MEDIA_PROBE_FAILED",
        detail=(
            "The provider-backed media could not be read during the subtitle probe. "
            "It may be locked or temporarily unavailable."
        ),
        retryable=True,
    )
    overlay_with_block = replace(plan.overlay_plan, blocked_assets=(blocked,))
    plan_with_block = replace(plan, overlay_plan=overlay_with_block)

    receipt = _receipt(
        plan=plan_with_block,
        mode="live",
        started_at="2026-08-04T00:00:00Z",
        sections=(),
        observed=[],
        title_coverage=[],
        topology_converged=True,
    )

    assert receipt["outcome"] == "degraded"
    assert len(receipt["blocked_assets"]) == 1
    reported = receipt["blocked_assets"][0]
    assert reported["code"] == "MEDIA_PROBE_FAILED"
    assert reported["retryable"] is True
    assert reported["torrent_hash"] == "A" * 40
    assert "last_observed_at" in reported


def test_held_title_coverage_reports_zero_visibility_for_every_publication(tmp_path: Path):
    publication = PublishedTitle(
        anilist_id=42,
        section="Anime",
        season_number=1,
        usable_episodes=(1, 2),
        published_episodes=(),
    )
    plan = replace(_approved_plan(tmp_path).overlay_plan, title_publications=(publication,))

    coverage = _held_title_coverage(plan)

    assert coverage == [
        {
            "anilist_id": 42,
            "section": "Anime",
            "season_number": 1,
            "usable_count": 2,
            "plex_visible_count": 0,
        }
    ]
