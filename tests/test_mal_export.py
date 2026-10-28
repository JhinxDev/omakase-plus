"""Tests for the MAL XML export parser + the upload path through MALAdapter.fetch."""

from __future__ import annotations

import gzip
from unittest.mock import patch

import pytest

from omakase.adapters.myanimelist import MALAdapter, MALExportError, parse_mal_export

_SAMPLE_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<myanimelist>
  <myinfo>
    <user_id>123</user_id>
    <user_name>SamplePilot</user_name>
    <user_export_type>1</user_export_type>
    <user_total_anime>4</user_total_anime>
  </myinfo>
  <anime>
    <series_animedb_id>1535</series_animedb_id>
    <series_title>Death Note</series_title>
    <series_type>TV</series_type>
    <series_episodes>37</series_episodes>
    <my_score>10</my_score>
    <my_status>Completed</my_status>
  </anime>
  <anime>
    <series_animedb_id>5114</series_animedb_id>
    <series_title>Fullmetal Alchemist: Brotherhood</series_title>
    <series_type>TV</series_type>
    <series_episodes>64</series_episodes>
    <my_score>9</my_score>
    <my_status>Completed</my_status>
  </anime>
  <anime>
    <series_animedb_id>9999</series_animedb_id>
    <series_title>Some Plan To Watch Show</series_title>
    <series_type>TV</series_type>
    <series_episodes>12</series_episodes>
    <my_score>0</my_score>
    <my_status>Plan to Watch</my_status>
  </anime>
  <anime>
    <series_animedb_id>4321</series_animedb_id>
    <series_title>Dropped Show</series_title>
    <series_type>TV</series_type>
    <series_episodes>24</series_episodes>
    <my_score>3</my_score>
    <my_status>Dropped</my_status>
  </anime>
</myanimelist>
"""


def test_parse_mal_export_extracts_username_and_entries():
    username, items = parse_mal_export(_SAMPLE_XML)
    assert username == "SamplePilot"
    by_id = {m.id: m for m in items}
    assert set(by_id) == {1535, 5114, 9999, 4321}
    assert by_id[1535].title_romaji == "Death Note"
    assert by_id[1535].score == 10.0
    assert by_id[1535].status == "COMPLETED"
    assert by_id[5114].score == 9.0
    # Score "0" in the XML means unscored — must surface as None.
    assert by_id[9999].score is None
    assert by_id[9999].status == "PLANNING"
    assert by_id[4321].status == "DROPPED"


def test_parse_mal_export_accepts_gzipped_bytes():
    gzipped = gzip.compress(_SAMPLE_XML)
    username, items = parse_mal_export(gzipped)
    assert username == "SamplePilot"
    assert len(items) == 4


def test_parse_mal_export_rejects_empty_input():
    with pytest.raises(MALExportError):
        parse_mal_export(b"")


def test_parse_mal_export_rejects_wrong_root():
    bogus = b"""<?xml version="1.0"?><mymangalist><manga/></mymangalist>"""
    with pytest.raises(MALExportError) as exc:
        parse_mal_export(bogus)
    assert "myanimelist" in str(exc.value)


def test_parse_mal_export_rejects_malformed_xml():
    with pytest.raises(MALExportError):
        parse_mal_export(b"<not-xml")


def test_parse_mal_export_rejects_empty_list():
    empty = b"""<?xml version="1.0"?><myanimelist><myinfo/></myanimelist>"""
    with pytest.raises(MALExportError):
        parse_mal_export(empty)


def test_mal_adapter_fetch_uses_export_and_bypasses_api():
    """When export_data is provided, the adapter must NOT call the MAL API."""
    adapter = MALAdapter()
    # If _fetch_history were called we'd hit the live MAL API (and crash without
    # MAL_CLIENT_ID); the export path must skip it entirely.
    with (
        patch.object(adapter, "_fetch_history", side_effect=AssertionError("API path used")),
        patch.object(adapter, "_fetch_candidates_jikan", return_value=[]) as jikan,
    ):
        data = adapter.fetch("", export_data=_SAMPLE_XML)
    assert data.username == "SamplePilot"
    assert {m.id for m in data.history} == {1535, 5114, 9999, 4321}
    # Jikan still gets called for the popular candidate pool.
    jikan.assert_called_once()


def test_mal_adapter_export_planning_mode_uses_planning_entries_from_xml():
    """In planning mode with an upload, candidates come from the XML's plan-to-watch."""
    adapter = MALAdapter()
    with patch.object(adapter, "_fetch_history", side_effect=AssertionError("API path used")):
        data = adapter.fetch("", export_data=_SAMPLE_XML, use_planning=True)
    cand_ids = {c.id for c in data.candidates}
    assert cand_ids == {9999}, f"expected only the plan-to-watch id, got {cand_ids}"


def test_mal_adapter_export_username_override():
    """Form-supplied username should take precedence over the one in the XML."""
    adapter = MALAdapter()
    with patch.object(adapter, "_fetch_candidates_jikan", return_value=[]):
        data = adapter.fetch("OverrideName", export_data=_SAMPLE_XML)
    assert data.username == "OverrideName"
