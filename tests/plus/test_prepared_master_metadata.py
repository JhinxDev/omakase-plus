"""Prepared HLS metadata comes from the bytes owned by the legacy store."""

from types import SimpleNamespace

from omakase.plus.debrid_stream import _master_playlist
from omakase.plus.prepared_hls import PreparedStore
from tests.plus.test_web_playback_codecs import box, hvcc


def _track(handler, entry):
    stsd = box(b"stsd", bytes(4) + (1).to_bytes(4, "big") + entry)
    hdlr = box(b"hdlr", bytes(8) + handler + bytes(12))
    return box(b"trak", box(b"mdia", hdlr + box(b"minf", box(b"stbl", stsd))))


def _fixture(tmp_path, config):
    key = "a" * 64
    directory = tmp_path / key
    directory.mkdir()
    visual = bytes(24) + bytes.fromhex("0f000870") + bytes(50)
    # AAC LC AudioSpecificConfig in an ES / decoder config / DSI descriptor.
    esds = box(
        b"esds", bytes(4) + bytes.fromhex("031600010004114015000000000000000000000005021210")
    )
    audio = box(b"mp4a", bytes(28) + esds)
    init = box(b"ftyp", b"iso5\0\0\0\1iso5") + box(
        b"moov",
        _track(b"vide", box(b"hev1", visual + box(b"hvcC", config))) + _track(b"soun", audio),
    )
    (directory / "init.mp4").write_bytes(init)
    (directory / "00000.m4s").write_bytes(bytes(600))
    (directory / "00001.m4s").write_bytes(bytes(1800))
    (directory / "stream.m3u8").write_text(
        '#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-MAP:URI="init.mp4"\n'
        "#EXTINF:6,\n00000.m4s\n#EXTINF:6,\n00001.m4s\n#EXT-X-ENDLIST\n"
    )
    return PreparedStore(tmp_path).open(key)


def test_prepared_master_uses_exact_init_and_actual_byte_rates(tmp_path):
    title = _fixture(tmp_path, hvcc(profile=0xA2))
    master = _master_playlist(SimpleNamespace(), prepared=title).decode()
    assert 'CODECS="hev1.B2.4.H150.90,mp4a.40.2"' in master
    assert "RESOLUTION=3840x2160" in master
    assert "BANDWIDTH=2400" in master
    assert "AVERAGE-BANDWIDTH=1600" in master


def test_truncated_hvcc_is_unknown_without_mislabeling_variant(tmp_path):
    title = _fixture(tmp_path, hvcc()[:13])
    master = _master_playlist(SimpleNamespace(), prepared=title).decode()
    assert "CODECS" not in master
    assert "RESOLUTION=3840x2160" in master
