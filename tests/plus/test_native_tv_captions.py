import pytest

from omakase.plus.native_tv_captions import rendition
from omakase.plus.web_playback_delivery import DeliveryError


def test_alternate_subtitle_segments_keep_the_video_timeline_and_crossing_cues(tmp_path):
    (tmp_path / "media-video.m3u8").write_text(
        "#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXT-X-PLAYLIST-TYPE:EVENT\n"
        '#EXT-X-MAP:URI="init-video.mp4"\n'
        "#EXTINF:4.000000,\npart-video-00000.m4s\n"
        "#EXTINF:4.000000,\npart-video-00001.m4s\n"
        "#EXTINF:4.000000,\npart-video-00002.m4s\n",
        encoding="utf-8",
    )
    (tmp_path / "text-1.vtt").write_text(
        "WEBVTT\n\n00:01.000 --> 00:02.000\nBefore the cut.\n\n"
        "00:03.900 --> 00:04.100\nCrosses the cut.\n\n"
        "00:06.000 --> 00:06.500\nAfter the cut.\n\n"
        "00:09.000 --> 00:11.000\nIncomplete tail",
        encoding="utf-8",
    )
    index, media_type = rendition(tmp_path, "captions-1.m3u8", 2)
    assert media_type == "application/vnd.apple.mpegurl"
    assert index.count(b"#EXTINF:4.000000,") == 3
    assert b"#EXT-X-ENDLIST" not in index
    assert b"init-video" not in index and b"part-video" not in index
    first, media_type = rendition(tmp_path, "captions-1-00000.vtt", 2)
    second, _ = rendition(tmp_path, "captions-1-00001.vtt", 2)
    silent, _ = rendition(tmp_path, "captions-1-00002.vtt", 2)
    assert media_type == "text/vtt"
    assert b"Before the cut." in first and b"Before the cut." not in second
    assert b"00:03.900 --> 00:04.100\nCrosses the cut." in first
    assert b"00:03.900 --> 00:04.100\nCrosses the cut." in second
    assert b"After the cut." in second and b"After the cut." not in first
    assert silent == b"WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:0\n\n"
    with pytest.raises(DeliveryError):
        rendition(tmp_path, "captions-2.m3u8", 2)
    with pytest.raises(DeliveryError):
        rendition(tmp_path, "captions-1-00003.vtt", 2)
