"""Regression tests for the planning-mode safety filter.

Bug discovered 2026-05-13: when use_planning=True, the adapter's post-fetch
filter was excluding planning items because they appear in _fetch_history()
output. This meant Plan-to-Watch always returned 0 candidates.
"""

from __future__ import annotations

from unittest.mock import patch

from omakase.adapters.anilist import AniListAdapter
from omakase.adapters.myanimelist import MALAdapter
from omakase.types import MediaItem


def _hist():
    return [
        MediaItem(id=1, title_romaji="Watched A", status="COMPLETED"),
        MediaItem(id=2, title_romaji="Watching B", status="CURRENT"),
        MediaItem(id=3, title_romaji="Dropped C", status="DROPPED"),
        MediaItem(id=4, title_romaji="Paused D", status="PAUSED"),
        MediaItem(id=5, title_romaji="Planning E", status="PLANNING"),
        MediaItem(id=6, title_romaji="Planning F", status="PLANNING"),
    ]


def _planning():
    # The planning fetcher returns the same IDs as the planning entries in history
    return [
        MediaItem(id=5, title_romaji="Planning E"),
        MediaItem(id=6, title_romaji="Planning F"),
    ]


def test_anilist_planning_mode_preserves_candidates():
    adapter = AniListAdapter()
    with (
        patch.object(adapter, "_fetch_history", return_value=_hist()),
        patch.object(adapter, "_fetch_planning", return_value=_planning()),
    ):
        data = adapter.fetch("anyuser", use_planning=True)
    cand_ids = {c.id for c in data.candidates}
    assert cand_ids == {5, 6}, f"expected planning items preserved, got {cand_ids}"


def test_anilist_planning_mode_drops_watched_items_leaking_into_planning():
    """If a watched item somehow appears in the planning fetch, drop it."""
    adapter = AniListAdapter()
    leaky_planning = _planning() + [MediaItem(id=1, title_romaji="Watched A")]
    with (
        patch.object(adapter, "_fetch_history", return_value=_hist()),
        patch.object(adapter, "_fetch_planning", return_value=leaky_planning),
    ):
        data = adapter.fetch("anyuser", use_planning=True)
    cand_ids = {c.id for c in data.candidates}
    assert cand_ids == {5, 6}, "watched item should be filtered out of planning candidates"


def test_anilist_popular_pool_mode_still_filters_history():
    """Non-planning mode must still strip history IDs from candidates."""
    adapter = AniListAdapter()
    popular = [
        MediaItem(id=99, title_romaji="Unwatched"),
        MediaItem(id=1, title_romaji="Watched A"),
    ]
    with (
        patch.object(adapter, "_fetch_history", return_value=_hist()),
        patch.object(adapter, "_fetch_candidates", return_value=popular),
    ):
        data = adapter.fetch("anyuser", use_planning=False)
    cand_ids = {c.id for c in data.candidates}
    assert cand_ids == {99}


def test_mal_planning_mode_preserves_candidates():
    adapter = MALAdapter()
    with (
        patch.object(adapter, "_fetch_history", return_value=_hist()),
        patch.object(adapter, "_fetch_planning", return_value=_planning()),
    ):
        data = adapter.fetch("anyuser", use_planning=True)
    cand_ids = {c.id for c in data.candidates}
    assert cand_ids == {5, 6}
