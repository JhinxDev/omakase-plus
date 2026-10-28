"""Resumable setup state tests."""

from pathlib import Path

import pytest

from omakase.plus.setup import (
    SetupStateError,
    load_setup_state,
    new_setup_state,
    save_setup_state,
    setup_progress,
    update_setup_step,
)


def test_setup_state_is_atomic_resumable_and_revision_bound(tmp_path: Path):
    path = tmp_path / "private" / "setup-state.json"
    state = new_setup_state(deployment_mode="local", now="2026-07-26T12:00:00Z")
    save_setup_state(path, state, expected_revision=0)

    loaded = load_setup_state(path)
    updated = update_setup_step(
        loaded,
        step="deployment",
        status="ready",
        now="2026-07-26T12:01:00Z",
        evidence=("compose profile rendered",),
    )
    save_setup_state(path, updated, expected_revision=loaded.revision)

    assert load_setup_state(path).revision == 2
    assert setup_progress(load_setup_state(path)) == {
        "complete": 1,
        "total": 6,
        "blocked": 0,
        "ready": False,
    }
    with pytest.raises(SetupStateError, match="changed"):
        save_setup_state(path, updated, expected_revision=1)


@pytest.mark.parametrize(
    "evidence",
    [
        ("PLEX_TOKEN=secret",),
        ("magnet:?xt=urn:btih:AAAAAAAA",),
        ("https://tracker.invalid/announce",),
        ("episode filename.mkv",),
        ("Hell's Paradise S02E01.mkv",),
        (r"library\anime",),
    ],
)
def test_setup_state_refuses_secret_or_identity_bearing_evidence(evidence):
    state = new_setup_state(deployment_mode="local", now="2026-07-26T12:00:00Z")
    with pytest.raises(SetupStateError, match="unsafe|URLs"):
        update_setup_step(
            state,
            step="storage",
            status="ready",
            now="2026-07-26T12:01:00Z",
            evidence=evidence,
        )
