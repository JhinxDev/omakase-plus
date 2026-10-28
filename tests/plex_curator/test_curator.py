from __future__ import annotations

import sys
from pathlib import Path

import pytest

CURATOR_ROOT = Path(__file__).resolve().parents[2] / "deploy" / "plex-curator"
sys.path.insert(0, str(CURATOR_ROOT))

from plex_curator import (  # noqa: E402
    CuratorConfig,
    CuratorValidationError,
    PlexMatchHint,
    TrustedAnimeFolder,
    apply_overlay_plan,
    build_overlay_plan,
)
from subtitle_support import SubtitleProbe, SubtitleTrack  # noqa: E402


def _manifest(
    *,
    source_folder: str = "Anime.Batch",
    filename_coverage: tuple[int, ...] = (1, 2),
    format: str = "TV",
) -> dict:
    return {
        "schema_version": "manifest-v2",
        "generated_at": "2026-07-15T18:22:11Z",
        "titles": [
            {
                "anilist_id": 42,
                "canonical_title": "Fixture Anime",
                "title_aliases": ["Fixture"],
                "genres": ["Action"],
                "format": format,
                "season_number": 1,
                "expected_episode_count": 2,
                "aired_episode_count": 2,
                "coverage": {
                    "required_episodes": [1, 2],
                    "usable_episodes": list(filename_coverage),
                    "plex_visible_episodes": [],
                    "required_count": 2,
                    "usable_count": len(filename_coverage),
                    "plex_visible_count": 0,
                },
                "assets": [
                    {
                        "torrent_hash": "A" * 40,
                        "source_folder": source_folder,
                        "storage_backend": "zurg",
                        "storage_locator": source_folder,
                        "source_title": "Fixture Anime S01",
                        "quality": "1080p",
                        "episode_coverage": list(filename_coverage),
                    }
                ],
            }
        ],
    }


def _config(tmp_path: Path, **overrides) -> CuratorConfig:
    values = {
        "source_root": tmp_path / "zurg",
        "overlay_root": tmp_path / "overlays",
        "riven_roots": (),
        "mount_aliases": (),
        "trusted_allowlist": (),
        "excluded_source_folders": (),
    }
    values.update(overrides)
    values["source_root"].mkdir(parents=True, exist_ok=True)
    return CuratorConfig(**values)


def _write_batch(config: CuratorConfig, folder: str = "Anime.Batch") -> None:
    source = config.source_root / folder
    source.mkdir(parents=True)
    (source / "Fixture Anime S01E01.mkv").write_bytes(b"episode-1")
    (source / "Fixture Anime S01E02.mkv").write_bytes(b"episode-2")


def test_curator_excludes_recursively_referenced_riven_folder_through_mount_alias(
    tmp_path: Path,
):
    alias_root = tmp_path / "plex-zurg-alias"
    riven_root = tmp_path / "riven"
    link_parent = riven_root / "shows" / "Invincible" / "Season 01"
    link_parent.mkdir(parents=True)
    config = _config(
        tmp_path,
        riven_roots=(riven_root,),
        mount_aliases=((alias_root, tmp_path / "zurg"),),
        trusted_allowlist=(
            TrustedAnimeFolder(
                source_folder="Invincible.LF_[rutor]",
                section="Anime",
                canonical_title="Invincible",
                season_number=1,
                reason="historical anime overlay entry",
            ),
        ),
    )
    source = config.source_root / "Invincible.LF_[rutor]"
    source.mkdir()
    media = source / "Invincible.S01E01.avi"
    media.write_bytes(b"video")
    (link_parent / "Invincible.S01E01.avi").symlink_to(
        alias_root / "Invincible.LF_[rutor]" / media.name
    )

    plan = build_overlay_plan(
        config,
        {"schema_version": "manifest-v2", "generated_at": "2026-07-15T18:22:11Z", "titles": []},
    )

    assert plan.excluded_riven_folders == ("Invincible.LF_[rutor]",)
    assert plan.entries == ()


def test_planning_is_nonmutating_and_round_trips_one_immutable_hash(tmp_path: Path):
    config = _config(tmp_path)
    _write_batch(config)

    plan = build_overlay_plan(config, _manifest())
    restored = type(plan).from_dict(plan.to_dict())

    assert restored == plan
    assert restored.plan_hash == plan.plan_hash
    assert not config.overlay_root.exists()
    assert {entry.section for entry in plan.entries} == {"Anime"}


def test_manifest_media_requires_english_and_binds_exact_embedded_tracks(tmp_path: Path):
    config = _config(tmp_path)
    _write_batch(config)
    probed: list[str] = []

    def probe(source: Path) -> SubtitleProbe:
        probed.append(source.name)
        return SubtitleProbe(
            duration_ms=60_000,
            tracks=(
                SubtitleTrack(index=2, codec="ass", language="eng", default=True),
                SubtitleTrack(index=3, codec="ass", language="eng", title="Signs & Songs"),
            ),
        )

    plan = build_overlay_plan(
        config,
        _manifest(),
        subtitle_probe=probe,
        require_english_subtitles=True,
    )
    restored = type(plan).from_dict(plan.to_dict())

    assert restored == plan
    assert sorted(probed) == [
        "Fixture Anime S01E01.mkv",
        "Fixture Anime S01E02.mkv",
    ]
    assert [(binding.stream_index, binding.role) for binding in plan.embedded_subtitles] == [
        (2, "standard"),
        (3, "forced"),
        (2, "standard"),
        (3, "forced"),
    ]


def test_subtitle_failure_blocks_only_the_affected_asset(tmp_path: Path):
    config = _config(tmp_path)
    _write_batch(config)

    plan = build_overlay_plan(
        config,
        _manifest(),
        subtitle_probe=lambda _: SubtitleProbe(
            duration_ms=60_000,
            tracks=(SubtitleTrack(index=2, codec="subrip", language="jpn"),),
        ),
        require_english_subtitles=True,
    )

    assert plan.entries == ()
    assert not config.overlay_root.exists()
    assert [asset.code for asset in plan.blocked_assets] == ["ENGLISH_SUBTITLE_MISSING"]
    assert plan.blocked_assets[0].retryable is True
    assert len(plan.title_publications) == 1
    assert plan.title_publications[0].published_episodes == ()
    assert plan.title_publications[0].usable_episodes == (1, 2)


def test_embedded_subtitles_are_plan_bound_and_revalidated_before_swap(tmp_path: Path):
    config = _config(tmp_path)
    _write_batch(config)
    plan = build_overlay_plan(
        config,
        _manifest(),
        subtitle_probe=lambda _: SubtitleProbe(
            duration_ms=60_000,
            tracks=(
                SubtitleTrack(
                    index=6,
                    codec="hdmv_pgs_subtitle",
                    language="eng",
                    title="English",
                    default=True,
                ),
            ),
        ),
        require_english_subtitles=True,
    )
    validated: list[tuple[str, int]] = []

    result = apply_overlay_plan(
        config,
        plan,
        expected_plan_hash=plan.plan_hash,
        embedded_subtitle_validator=lambda source, binding: validated.append(
            (source.name, binding.stream_index)
        ),
    )

    assert len(plan.embedded_subtitles) == 2
    assert sorted(validated) == [
        ("Fixture Anime S01E01.mkv", 6),
        ("Fixture Anime S01E02.mkv", 6),
    ]
    assert result.changed is True
    assert not tuple(result.generation.rglob("*.srt"))


def test_overlay_links_target_the_container_alias_after_host_source_validation(tmp_path: Path):
    alias_root = tmp_path / "plex-zurg"
    config = _config(
        tmp_path,
        mount_aliases=((alias_root, tmp_path / "zurg"),),
    )
    _write_batch(config)
    plan = build_overlay_plan(config, _manifest())

    result = apply_overlay_plan(config, plan, expected_plan_hash=plan.plan_hash)
    link = (
        result.generation / "Anime" / "Fixture Anime" / "Season 01" / "Fixture Anime - S01E01.mkv"
    )

    assert link.is_symlink()
    expected = alias_root / "Anime.Batch" / "Fixture Anime S01E01.mkv"
    assert str(link.readlink()).removeprefix("\\\\?\\") == str(expected)


def test_reviewed_plex_match_hint_is_plan_bound_and_staged_with_media(tmp_path: Path):
    hint = PlexMatchHint(
        canonical_title="Fixture Anime",
        season_number=1,
        source="tvdb",
        source_id="402474",
    )
    config = _config(tmp_path, plex_match_hints=(hint,))
    _write_batch(config)

    plan = build_overlay_plan(config, _manifest())
    restored = type(plan).from_dict(plan.to_dict())
    result = apply_overlay_plan(config, plan, expected_plan_hash=plan.plan_hash)
    match_file = result.generation / "Anime" / "Fixture Anime" / ".plexmatch"

    assert restored.plex_match_hints == (hint,)
    assert match_file.read_text(encoding="utf-8") == "tvdbid: 402474\nseason: 1\n"
    assert not match_file.is_symlink()


def test_stale_plex_match_hint_blocks_planning_before_mutation(tmp_path: Path):
    config = _config(
        tmp_path,
        plex_match_hints=(
            PlexMatchHint(
                canonical_title="Different Anime",
                season_number=1,
                source="tvdb",
                source_id="402474",
            ),
        ),
    )
    _write_batch(config)

    with pytest.raises(CuratorValidationError, match="does not identify a staged series"):
        build_overlay_plan(config, _manifest())

    assert not config.overlay_root.exists()


def test_overlay_directories_are_explicitly_traversable_under_service_umask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    chmod_calls: list[tuple[Path, int]] = []
    real_chmod = Path.chmod

    def record_chmod(path: Path, mode: int, *args, **kwargs) -> None:
        chmod_calls.append((path, mode))
        real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "chmod", record_chmod)
    config = _config(tmp_path)
    _write_batch(config)
    plan = build_overlay_plan(config, _manifest())

    result = apply_overlay_plan(config, plan, expected_plan_hash=plan.plan_hash)

    assert chmod_calls
    assert {mode for _, mode in chmod_calls} == {0o755}
    traversable = {path.name for path, _ in chmod_calls}
    assert {"overlays", "generations", "Anime", "Anime Movies", "Season 01"} <= traversable

    real_chmod(result.generation, 0o700)
    chmod_calls.clear()
    second = apply_overlay_plan(config, plan, expected_plan_hash=plan.plan_hash)

    assert second.changed is False
    assert (result.generation, 0o755) in chmod_calls


def test_atomic_dual_overlay_swap_retains_exactly_one_previous_generation(tmp_path: Path):
    config = _config(tmp_path)
    _write_batch(config)
    first = build_overlay_plan(config, _manifest())
    first_result = apply_overlay_plan(config, first, expected_plan_hash=first.plan_hash)

    movie_folder = config.source_root / "Movie.Batch"
    movie_folder.mkdir()
    (movie_folder / "Fixture Anime.1080p.mkv").write_bytes(b"movie")
    second_manifest = _manifest(source_folder="Movie.Batch", filename_coverage=(1,), format="MOVIE")
    second_manifest["generated_at"] = "2026-07-15T18:37:11Z"
    second_title = second_manifest["titles"][0]
    second_title["season_number"] = None
    second_title["expected_episode_count"] = 1
    second_title["aired_episode_count"] = 1
    second_title["coverage"].update(
        required_episodes=[1], required_count=1, usable_episodes=[1], usable_count=1
    )
    second = build_overlay_plan(config, second_manifest)
    second_result = apply_overlay_plan(config, second, expected_plan_hash=second.plan_hash)

    assert config.current_pointer.resolve() == second_result.generation.resolve()
    assert (second_result.generation / "Anime").is_dir()
    assert (second_result.generation / "Anime Movies").is_dir()
    assert len(tuple(config.generations_root.iterdir())) == 2
    assert first_result.generation.exists()


def _retained_generations(tmp_path: Path):
    config = _config(
        tmp_path,
        plex_match_hints=(PlexMatchHint("Fixture Anime", 1, "tvdb", "12345"),),
    )
    _write_batch(config)

    def plan(manifest):
        return build_overlay_plan(
            config,
            manifest,
            subtitle_probe=lambda _: SubtitleProbe(
                duration_ms=60_000,
                tracks=(SubtitleTrack(index=2, codec="subrip", language="eng"),),
            ),
            require_english_subtitles=True,
        )

    first = plan(_manifest())
    first_result = apply_overlay_plan(
        config,
        first,
        expected_plan_hash=first.plan_hash,
        embedded_subtitle_validator=lambda *_: None,
    )
    second_manifest = _manifest()
    second_manifest["titles"][0]["coverage"].update(
        plex_visible_episodes=[1, 2], plex_visible_count=2
    )
    second = plan(second_manifest)
    assert second.plan_hash != first.plan_hash
    second_result = apply_overlay_plan(
        config,
        second,
        expected_plan_hash=second.plan_hash,
        embedded_subtitle_validator=lambda *_: None,
    )
    return config, first, first_result.generation, second_result.generation


def test_return_to_retained_generation_revalidates_approved_content_before_selection(tmp_path):
    config, first, retained, previous = _retained_generations(tmp_path)
    validated = []
    callbacks = []

    result = apply_overlay_plan(
        config,
        first,
        expected_plan_hash=first.plan_hash,
        embedded_subtitle_validator=lambda source, binding: validated.append(source),
        after_swap=callbacks.append,
    )

    assert result.changed is True
    assert result.generation == retained
    assert result.previous_generation == previous
    assert config.current_pointer.resolve() == retained
    assert len(validated) == 2
    assert callbacks == [result]
    assert set(config.generations_root.iterdir()) == {retained, previous}
    repeated = apply_overlay_plan(config, first, expected_plan_hash=first.plan_hash)
    assert repeated.changed is False


@pytest.mark.parametrize(
    "tampering",
    [
        "wrong-link",
        "missing-link",
        "hint",
        "hint-link",
        "extra-file",
        "extra-dir",
        "nested-link",
        "root-link",
    ],
)
def test_retained_generation_with_unapproved_tree_cannot_be_reselected(tmp_path, tampering):
    config, first, retained, previous = _retained_generations(tmp_path)
    entry = retained / first.entries[0].destination
    if tampering == "wrong-link":
        entry.unlink()
        entry.symlink_to(config.source_root / "Anime.Batch" / "Fixture Anime S01E02.mkv")
    elif tampering == "missing-link":
        entry.unlink()
    elif tampering == "hint":
        (retained / "Anime" / "Fixture Anime" / ".plexmatch").write_bytes(
            b"tvdbid: 54321\nseason: 1\n"
        )
    elif tampering == "hint-link":
        hint = retained / "Anime" / "Fixture Anime" / ".plexmatch"
        hint.unlink()
        hint.symlink_to(previous / "Anime" / "Fixture Anime" / ".plexmatch")
    elif tampering == "extra-file":
        (retained / "unapproved.txt").write_bytes(b"unapproved content")
    elif tampering == "extra-dir":
        (retained / "unexpected").mkdir()
    elif tampering == "nested-link":
        (retained / "Anime").rename(config.generations_root / ".saved-anime")
        (retained / "Anime").symlink_to(previous / "Anime", target_is_directory=True)
    else:
        saved = config.generations_root / ".saved-retained"
        retained.rename(saved)
        retained.symlink_to(saved, target_is_directory=True)
    retained_names = set(config.generations_root.iterdir())

    with pytest.raises(CuratorValidationError):
        apply_overlay_plan(
            config,
            first,
            expected_plan_hash=first.plan_hash,
            embedded_subtitle_validator=lambda *_: None,
            after_swap=lambda _: pytest.fail("unapproved tree was selected"),
        )

    assert config.current_pointer.resolve() == previous
    assert set(config.generations_root.iterdir()) == retained_names


def test_retained_generation_verification_never_removes_a_preexisting_stage(tmp_path):
    config, first, retained, previous = _retained_generations(tmp_path)
    staging = config.generations_root / f".staging-{first.plan_hash}"
    staging.mkdir()
    sentinel = staging / "unrelated-work"
    sentinel.write_bytes(b"preserve this stage")

    with pytest.raises(CuratorValidationError, match="staging generation already exists"):
        apply_overlay_plan(
            config,
            first,
            expected_plan_hash=first.plan_hash,
            embedded_subtitle_validator=lambda *_: None,
        )

    assert sentinel.read_bytes() == b"preserve this stage"
    assert config.current_pointer.resolve() == previous
    assert set(config.generations_root.iterdir()) == {retained, previous, staging}


def test_retained_generation_requires_fresh_source_validation(tmp_path):
    config, first, retained, previous = _retained_generations(tmp_path)

    def reject_source(*_):
        raise OSError("source no longer readable")

    with pytest.raises(OSError, match="source no longer readable"):
        apply_overlay_plan(
            config,
            first,
            expected_plan_hash=first.plan_hash,
            embedded_subtitle_validator=reject_source,
        )

    assert config.current_pointer.resolve() == previous
    assert set(config.generations_root.iterdir()) == {retained, previous}


def test_reused_generation_callback_failure_restores_selection_without_removing_recovery(tmp_path):
    config, first, retained, previous = _retained_generations(tmp_path)

    def reject_publication(result):
        assert config.current_pointer.resolve() == retained
        raise RuntimeError("native publication failed")

    with pytest.raises(RuntimeError, match="native publication failed"):
        apply_overlay_plan(
            config,
            first,
            expected_plan_hash=first.plan_hash,
            embedded_subtitle_validator=lambda *_: None,
            after_swap=reject_publication,
        )

    assert config.current_pointer.resolve() == previous
    assert set(config.generations_root.iterdir()) == {retained, previous}


@pytest.mark.parametrize("reuse", [False, True])
def test_pointer_fsync_failure_after_replace_restores_previous_selection(
    tmp_path, monkeypatch, reuse
):
    import plex_curator

    config, first, retained, previous = _retained_generations(tmp_path)
    target = first
    if not reuse:
        manifest = _manifest()
        manifest["titles"][0]["title_aliases"].append("Another Alias")
        target = build_overlay_plan(config, manifest)
    fsync = plex_curator._fsync_directory

    def fail_after_replace(path):
        if path == config.overlay_root and config.current_pointer.resolve() != previous:
            raise OSError("pointer fsync failed after replacement")
        fsync(path)

    monkeypatch.setattr(plex_curator, "_fsync_directory", fail_after_replace)
    with pytest.raises(OSError, match="pointer fsync failed after replacement"):
        apply_overlay_plan(
            config,
            target,
            expected_plan_hash=target.plan_hash,
            embedded_subtitle_validator=lambda *_: None,
        )

    assert config.current_pointer.resolve() == previous
    assert set(config.generations_root.iterdir()) == {retained, previous}


def test_movie_plan_stages_plain_feature_filename_without_episode_parsing(tmp_path: Path):
    config = _config(tmp_path)
    movie_folder = config.source_root / "Movie.Batch"
    movie_folder.mkdir()
    (movie_folder / "Fixture Movie.2160p.mkv").write_bytes(b"movie")
    manifest = _manifest(source_folder="Movie.Batch", filename_coverage=(1,), format="MOVIE")
    title = manifest["titles"][0]
    title["season_number"] = None
    title["expected_episode_count"] = 1
    title["aired_episode_count"] = 1
    title["coverage"].update(
        required_episodes=[1], required_count=1, usable_episodes=[1], usable_count=1
    )
    title["assets"][0]["quality"] = "2160p"

    plan = build_overlay_plan(config, manifest)

    assert len(plan.entries) == 1
    entry = plan.entries[0]
    assert entry.section == "Anime Movies"
    assert entry.destination.startswith("Anime Movies/Fixture Anime/")
    assert "Season" not in entry.destination
    assert entry.destination.endswith(".mkv")


def test_post_swap_failure_restores_previous_pointer_before_propagating(tmp_path: Path):
    config = _config(tmp_path)
    _write_batch(config)
    first = build_overlay_plan(config, _manifest())
    first_result = apply_overlay_plan(config, first, expected_plan_hash=first.plan_hash)

    (config.source_root / "Anime.Batch" / "Fixture Anime S01E02.mkv").rename(
        config.source_root / "Anime.Batch" / "Fixture Anime S01E03.mkv"
    )
    changed = _manifest(filename_coverage=(1, 3))
    changed["titles"][0]["expected_episode_count"] = 3
    changed["titles"][0]["aired_episode_count"] = 3
    changed["titles"][0]["coverage"]["required_episodes"] = [1, 2, 3]
    changed["titles"][0]["coverage"]["required_count"] = 3
    changed["generated_at"] = "2026-07-15T18:52:11Z"
    second = build_overlay_plan(config, changed)

    with pytest.raises(RuntimeError, match="scan failed"):
        apply_overlay_plan(
            config,
            second,
            expected_plan_hash=second.plan_hash,
            after_swap=lambda _: (_ for _ in ()).throw(RuntimeError("scan failed")),
        )

    assert config.current_pointer.resolve() == first_result.generation.resolve()


def test_incomplete_asset_coverage_is_blocked_without_failing_the_whole_plan(tmp_path: Path):
    config = _config(tmp_path)
    source = config.source_root / "Anime.Batch"
    source.mkdir()
    (source / "Fixture Anime S01E01.mkv").write_bytes(b"episode-1")

    plan = build_overlay_plan(config, _manifest())

    assert plan.entries == ()
    assert not config.overlay_root.exists()
    assert [asset.code for asset in plan.blocked_assets] == ["ASSET_COVERAGE_INVALID"]
    assert plan.blocked_assets[0].retryable is True


def _second_title_manifest(*, source_folder: str = "Other.Anime.Batch") -> dict:
    return {
        "anilist_id": 99,
        "canonical_title": "Other Anime",
        "title_aliases": ["Other"],
        "genres": ["Comedy"],
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
                "torrent_hash": "B" * 40,
                "source_folder": source_folder,
                "storage_backend": "zurg",
                "storage_locator": source_folder,
                "source_title": "Other Anime S01",
                "quality": "1080p",
                "episode_coverage": [1],
            }
        ],
    }


def test_one_unreadable_asset_does_not_block_other_titles_from_publishing(tmp_path: Path):
    """Regression test: a single locked/unreadable release must not withhold
    unrelated, healthy titles from the same Plex generation (the KAMUI S01E05
    423 Locked incident)."""
    config = _config(tmp_path)
    _write_batch(config)
    healthy_folder = config.source_root / "Other.Anime.Batch"
    healthy_folder.mkdir()
    (healthy_folder / "Other Anime S01E01.mkv").write_bytes(b"episode-1")

    manifest = _manifest()
    manifest["titles"].append(_second_title_manifest())

    def probe(source: Path) -> SubtitleProbe:
        if "Fixture Anime" in source.name:
            raise OSError("simulated 423 Locked: file is temporarily unavailable")
        return SubtitleProbe(
            duration_ms=60_000,
            tracks=(SubtitleTrack(index=2, codec="ass", language="eng", default=True),),
        )

    plan = build_overlay_plan(
        config,
        manifest,
        subtitle_probe=probe,
        require_english_subtitles=True,
    )

    # The blocked title contributes no entries, but the healthy title still
    # publishes into the exact same generation.
    assert [entry.destination for entry in plan.entries] == [
        "Anime/Other Anime/Season 01/Other Anime - S01E01.mkv"
    ]
    codes = {asset.anilist_id: asset.code for asset in plan.blocked_assets}
    assert codes == {42: "MEDIA_PROBE_FAILED"}
    assert plan.blocked_assets[0].retryable is True

    publications = {item.anilist_id: item for item in plan.title_publications}
    assert publications[42].published_episodes == ()
    assert publications[99].published_episodes == (1,)

    result = apply_overlay_plan(
        config,
        plan,
        expected_plan_hash=plan.plan_hash,
        embedded_subtitle_validator=lambda source, binding: None,
    )
    assert result.changed is True
    assert (
        result.generation / "Anime" / "Other Anime" / "Season 01" / "Other Anime - S01E01.mkv"
    ).is_symlink()
    assert not (result.generation / "Anime" / "Fixture Anime").exists()


REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("unit_name", "required_lines"),
    [
        (
            "plex-anime-curator.service",
            (
                "Type=oneshot",
                "NoNewPrivileges=true",
                "EnvironmentFile=/etc/omakase/plex-curator.env",
                "ExecStartPre=/usr/bin/test -x /usr/bin/ffprobe",
            ),
        ),
        (
            "plex-anime-curator.timer",
            (
                "Persistent=true",
                "OnBootSec=5m",
                "OnUnitActiveSec=15m",
            ),
        ),
    ],
)
def test_plex_curator_systemd_units_are_hardened(unit_name, required_lines):
    unit = REPO_ROOT / "deploy" / "systemd" / unit_name
    text = unit.read_text(encoding="utf-8")

    for required_line in required_lines:
        assert required_line in text

    assert "PLEX_TOKEN=" not in text
    assert "OMAKASE_LIBRARY_MANIFEST_TOKEN=" not in text
