"""Bounded HEVC initialization metadata; no encoders or provider requests."""

import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from omakase.plus import web_playback_codecs as codecs
from omakase.plus.web_playback_source import PlexStream


def box(kind, payload):
    return (8 + len(payload)).to_bytes(4, "big") + kind + payload


def hvcc(*, constraints=b"\x90\0\0\0\0\0", profile=2, compatibility=0x20000000):
    # V13's exact first 23 hvcC bytes, with three small synthetic parameter sets.
    header = bytes.fromhex("01022000000090000000000096f000fcfdfafa00000f03")
    header = (
        header[:1] + bytes([profile]) + compatibility.to_bytes(4, "big") + constraints + header[12:]
    )
    arrays = b"".join(bytes([kind]) + b"\0\1\0\2" + bytes([kind << 1, 1]) for kind in (32, 33, 34))
    return header + arrays


def init_segment(config=None, *, entry=b"hev1", duplicate=False, tracks=1, color=b""):
    config = hvcc() if config is None else config
    child = box(b"hvcC", config)
    visual = box(entry, bytes(78) + child * (2 if duplicate else 1) + color)
    stsd = box(b"stsd", bytes(4) + (1).to_bytes(4, "big") + visual)
    hdlr = box(b"hdlr", bytes(8) + b"vide" + bytes(12))
    trak = box(b"trak", box(b"mdia", hdlr + box(b"minf", box(b"stbl", stsd))))
    return box(b"ftyp", b"iso5\0\0\0\1iso5") + box(b"moov", trak * tracks)


@pytest.fixture
def partial(tmp_path):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    migration = (
        Path(__file__).resolve().parents[2]
        / "src/omakase/plus/migrations/040-web-playback-remux.sql"
    )
    db.executescript(migration.read_text())
    job, owner = "a" * 32, "b" * 64
    folder = tmp_path / job
    folder.mkdir()
    (folder / ".omakase-remux").write_text(job)
    db.execute(
        "INSERT INTO web_playback_remux(job_id,media_version,actor_user_id,state,phase,created,expires,coordinator_pid,coordinator_token) VALUES(?, ?,1,'RUNNING','validating',0,1,?,?)",
        (job, "c" * 64, os.getpid(), owner),
    )
    db.commit()
    source = SimpleNamespace(
        video_codec="hevc",
        media_version="c" * 64,
        actor_user_id=1,
        streams=(
            PlexStream("100", "video", "hevc", "und", False, False, True, 0),
            PlexStream("101", "audio", "opus", "jpn", False, False, True, 1),
        ),
    )
    recipe = {
        "mode": "remux",
        "video": {"id": "100", "index": 0},
        "audio": [{"id": "101", "index": 1}],
    }
    (folder / "init-0.mp4").write_bytes(init_segment())
    (folder / "init-1.mp4").write_bytes(b"unchanged audio init")
    (folder / "chunk-0-00001.m4s").write_bytes(b"unchanged video packets")
    (folder / "manifest.mpd").write_text(
        """<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"><Period><AdaptationSet><Representation id="0" codecs="hev1" mimeType="video/mp4"><SegmentTemplate initialization="init-$RepresentationID$.mp4"/></Representation></AdaptationSet><AdaptationSet><Representation id="1" codecs="opus" mimeType="audio/mp4"/></AdaptationSet></Period></MPD>"""
    )
    yield db, folder, source, recipe, job, owner
    db.close()


def normalize(partial):
    db, folder, source, recipe, job, owner = partial
    return codecs.normalize_partial_manifest(
        db, folder, source, recipe, job_id=job, owner_token=owner
    )


def test_default_ffmpeg_bare_hevc_normalizes_exact_v13_metadata_only(partial):
    folder = partial[1]
    before = {path.name: path.read_bytes() for path in folder.iterdir()}
    assert normalize(partial) == "hev1.2.4.L150.90"
    assert b'codecs="hev1.2.4.L150.90"' in (folder / "manifest.mpd").read_bytes()
    assert all(
        (folder / name).read_bytes() == raw
        for name, raw in before.items()
        if name != "manifest.mpd"
    )
    assert {path.name for path in folder.iterdir()} == set(before)
    corrected = (folder / "manifest.mpd").read_bytes()
    assert normalize(partial) == "hev1.2.4.L150.90"
    assert (folder / "manifest.mpd").read_bytes() == corrected


@pytest.mark.parametrize("entry", [b"hev1", b"hvc1"])
def test_exact_sample_entry_prefix_is_preserved(entry):
    config = hvcc()
    if entry == b"hvc1":
        config = config[:23] + b"".join(
            bytes([kind | 0x80]) + b"\0\1\0\2" + bytes([kind << 1, 1]) for kind in (32, 33, 34)
        )
    assert (
        codecs.hevc_codec_from_init(init_segment(config, entry=entry))
        == entry.decode() + ".2.4.L150.90"
    )


def test_compatibility_bit_reversal_and_constraint_trailing_zeros():
    assert (
        codecs.hevc_codec_from_init(
            init_segment(hvcc(compatibility=0x80000001, constraints=b"\x90\0\x01\0\0\0"))
        )
        == "hev1.2.80000001.L150.90.0.1"
    )
    assert (
        codecs.hevc_codec_from_init(init_segment(hvcc(compatibility=0, constraints=bytes(6))))
        == "hev1.2.0.L150"
    )


def test_profile_space_and_high_tier_are_not_guessed():
    assert codecs.hevc_codec_from_init(init_segment(hvcc(profile=0xA2))) == "hev1.B2.4.H150.90"


@pytest.mark.parametrize(
    ("color", "expected"),
    [
        (bytes.fromhex("6e636c7800090010000900"), b"PQ"),
        (bytes.fromhex("6e636c7800090012000900"), b"HLG"),
        (bytes.fromhex("6e636c7800010001000100"), None),
        (bytes.fromhex("6e636c78000900100009"), None),
    ],
)
def test_native_video_range_uses_the_color_box_in_the_exact_hevc_entry(tmp_path, color, expected):
    name = "init-video.mp4"
    (tmp_path / name).write_bytes(init_segment(color=box(b"colr", color)))
    assert codecs.hevc_video_range(tmp_path, name) == expected


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"hvcC" + bytes(100),
        init_segment()[:-1],
        init_segment(duplicate=True),
        init_segment(tracks=2),
        init_segment(hvcc()[:-1]),
        init_segment(bytes(23)),
        init_segment(entry=b"encv"),
        bytes(1024**2 + 1),
        box(b"moov", b"\0\0\0\0free"),
    ],
    ids=[
        "empty",
        "fake_signature",
        "truncated_box",
        "duplicate_hvcc",
        "multiple_tracks",
        "truncated_nal",
        "invalid_header",
        "encrypted",
        "oversized",
        "open_ended_box",
    ],
)
def test_init_parser_rejects_malformed_ambiguous_or_unbounded(data):
    with pytest.raises(codecs.CodecMetadataError):
        codecs.hevc_codec_from_init(data)


@pytest.mark.parametrize(
    "change",
    [
        "ready",
        "wrong_owner",
        "wrong_pid",
        "missing_marker",
        "wrong_phase",
        "wrong_track_id",
        "wrong_track_index",
        "proper_contradiction",
        "wrong_prefix",
        "wrong_rep",
        "external_init",
        "duplicate_rep",
        "dtd",
    ],
)
def test_normalizer_rejects_nonowned_or_contradictory_metadata(partial, change):
    db, folder, _, recipe, _, _ = partial
    manifest = folder / "manifest.mpd"
    raw = manifest.read_text()
    if change == "ready":
        db.execute("UPDATE web_playback_remux SET state='READY'")
    elif change == "wrong_owner":
        db.execute("UPDATE web_playback_remux SET coordinator_token='other'")
    elif change == "wrong_pid":
        db.execute("UPDATE web_playback_remux SET coordinator_pid=-1")
    elif change == "missing_marker":
        (folder / ".omakase-remux").unlink()
    elif change == "wrong_phase":
        db.execute("UPDATE web_playback_remux SET phase='remux'")
    elif change == "wrong_track_id":
        recipe["video"]["id"] = "other"
    elif change == "wrong_track_index":
        recipe["audio"][0]["index"] = 42
    elif change == "proper_contradiction":
        raw = raw.replace('codecs="hev1"', 'codecs="hev1.1.4.L150.90"')
    elif change == "wrong_prefix":
        raw = raw.replace('codecs="hev1"', 'codecs="hvc1"')
    elif change == "wrong_rep":
        raw = raw.replace('id="0"', 'id="7"')
    elif change == "external_init":
        raw = raw.replace('initialization="init-', 'initialization="https://external.invalid/init-')
    elif change == "duplicate_rep":
        raw = raw.replace("</Period>", '<Representation id="0" codecs="hev1"/></Period>')
    else:
        raw = '<!DOCTYPE MPD [<!ENTITY x "value">]>' + raw
    db.commit()
    manifest.write_text(raw)
    before = manifest.read_bytes()
    with pytest.raises(codecs.CodecMetadataError):
        normalize(partial)
    assert manifest.read_bytes() == before


def test_normalization_preserves_default_namespace_without_global_registry_changes(partial):
    import xml.etree.ElementTree as ET

    manifest = partial[1] / "manifest.mpd"
    raw = manifest.read_text().replace(
        "<MPD ",
        '<MPD xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="urn:mpeg:dash:schema:mpd:2011 schema.xsd" ',
    )
    manifest.write_text(raw)
    registry = dict(ET._namespace_map)
    normalize(partial)
    data = manifest.read_bytes()
    assert b"<MPD " in data and b"<ns0:MPD" not in data
    assert ET.fromstring(data).tag == "{urn:mpeg:dash:schema:mpd:2011}MPD"
    assert ET._namespace_map == registry


def test_correct_qualified_equivalent_is_retained_and_xml_depth_is_bounded(partial):
    manifest = partial[1] / "manifest.mpd"
    raw = manifest.read_text().replace('codecs="hev1"', 'codecs="hev1.2.00000004.L150.90.00"')
    manifest.write_text(raw)
    before = manifest.read_bytes()
    assert normalize(partial) == "hev1.2.4.L150.90"
    assert manifest.read_bytes() == before
    raw = raw.replace("</MPD>", "<Extra>" * 13 + "</Extra>" * 13 + "</MPD>")
    manifest.write_text(raw)
    with pytest.raises(codecs.CodecMetadataError):
        normalize(partial)
