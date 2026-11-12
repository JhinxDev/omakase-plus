"""The native Plex bridge accepts only a verified, same-session HLS playlist."""

from types import SimpleNamespace

import pytest

from omakase.plus import native_tv_compatible as native
from omakase.plus import web_playback_compatible as web
from omakase.plus.web_playback_delivery import DeliveryError

SESSION = "a" * 32
MASTER = (
    "#EXTM3U\n"
    "#EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH=8500000,RESOLUTION=1920x1080,FRAME-RATE=23.976000\n"
    f"session/{SESSION}/base/index.m3u8\n"
)
PLAYLIST = (
    "#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXT-X-ALLOW-CACHE:NO\n#EXT-X-MEDIA-SEQUENCE:0\n"
    "#EXTINF:5, nodesc\n00000.ts\n#EXTINF:4.125, nodesc\n00001.ts\n#EXT-X-ENDLIST\n"
)


@pytest.mark.parametrize("codec", ["h264", "hevc", "vc1"])
def test_plex_transcode_can_accept_non_h264_sources(codec, monkeypatch):
    monkeypatch.setenv("OMAKASE_NATIVE_TV_PLEX_HLS", "1")
    source = SimpleNamespace(
        duration_ms=7_800_000,
        container="mkv",
        video_codec=codec,
        audio_codec="dca-ma",
        _single_plex_part=True,
    )
    assert native.eligible(source)
    assert bool(web.eligible(source, {"h264", "aac"})) == (codec == "h264")


@pytest.mark.parametrize(
    "overrides",
    [
        {"video_codec": "unknown"},
        {"container": "avi"},
        {"container": "mp4", "audio_codec": "aac"},
        {"audio_codec": ""},
        {"_single_plex_part": False},
        {"duration_ms": 14_400_001},
    ],
)
def test_plex_transcode_keeps_existing_source_bounds(overrides, monkeypatch):
    monkeypatch.setenv("OMAKASE_NATIVE_TV_PLEX_HLS", "1")
    source = SimpleNamespace(
        **(
            dict(
                duration_ms=7_800_000,
                container="mkv",
                video_codec="hevc",
                audio_codec="dca-ma",
                _single_plex_part=True,
            )
            | overrides
        )
    )
    assert not native.eligible(source)


def test_playlist_preserves_measured_durations_and_uses_local_segments():
    assert native._variant(MASTER.encode(), SESSION) == f"session/{SESSION}/base/index.m3u8"
    playlist, count = native._playlist(PLAYLIST.encode(), 9.125)
    assert count == 2
    assert b"#EXT-X-PLAYLIST-TYPE:VOD" in playlist
    assert b"#EXTINF:4.125,\n00001.ts\n#EXT-X-ENDLIST\n" in playlist
    assert b"nodesc" not in playlist


@pytest.mark.parametrize(
    "variant",
    [
        MASTER.replace(SESSION, "b" * 32),
        MASTER.replace("session/", "https://outside.invalid/session/"),
        MASTER.replace("index.m3u8", "index.m3u8?secret=bad"),
        MASTER + MASTER.splitlines()[-1] + "\n",
    ],
)
def test_master_rejects_ambiguous_and_unscoped_variants(variant):
    with pytest.raises(DeliveryError):
        native._variant(variant.encode(), SESSION)


@pytest.mark.parametrize(
    "playlist",
    [
        PLAYLIST.replace("00000.ts", "../00000.ts"),
        PLAYLIST.replace("00001.ts", "00002.ts"),
        PLAYLIST.replace("#EXTINF:4.125, nodesc", "#EXT-X-KEY:URI=untrusted"),
        PLAYLIST.replace("#EXT-X-ENDLIST\n", ""),
        PLAYLIST.replace("#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-MEDIA-SEQUENCE:1"),
        PLAYLIST.replace("#EXTINF:4.125", "#EXTINF:1.125"),
    ],
)
def test_playlist_rejects_unlisted_tags_inexact_durations_and_external_paths(playlist):
    with pytest.raises(DeliveryError):
        native._playlist(playlist.encode(), 9.125)
