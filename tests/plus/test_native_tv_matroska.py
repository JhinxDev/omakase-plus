import pytest

from omakase.plus import native_tv_matroska as matroska
from omakase.plus.web_playback_source import PlexStream


def element(tag, data):
    if isinstance(data, int):
        data = data.to_bytes(max(1, (data.bit_length() + 7) // 8), "big")
    width = 1 if len(data) < 127 else 2
    size = ((1 << (7 * width)) | len(data)).to_bytes(width, "big")
    return tag.to_bytes((tag.bit_length() + 7) // 8, "big") + size + data


def fixture(*, bad_timestamp=False, compress=False, media_tracks=False):
    info = element(0x1549A966, element(0x2AD7B1, 1_000_000))
    track = element(0xD7, 7) + element(0x83, 17) + element(0x86, b"S_TEXT/UTF8")
    if compress:
        track += element(0x6D80, b"")
    media = b""
    if media_tracks:
        media = element(0xAE, element(0xD7, 1) + element(0x83, 1)) + element(
            0xAE, element(0xD7, 2) + element(0x83, 2)
        )
    tracks = element(0x1654AE6B, media + element(0xAE, track))
    block = element(0xA1, b"\x87\x01\xf4\x00<i>First cue</i> <other>")
    timestamp = element(0xE7, 1000)
    cluster = element(0x1F43B675, timestamp + element(0xA0, block + element(0x9B, 4000)))

    def seek_head(positions):
        return element(
            0x114D9B74,
            b"".join(
                element(0x4DBB, element(0x53AB, tag) + element(0x53AC, pos.to_bytes(4, "big")))
                for tag, pos in positions.items()
            ),
        )

    tags = {0x1549A966: 0, 0x1654AE6B: 0, 0x1C53BB6B: 0}
    header_size = len(seek_head(tags))
    cluster_position = header_size + len(info) + len(tracks)
    tags = {
        0x1549A966: header_size,
        0x1654AE6B: header_size + len(info),
        0x1C53BB6B: cluster_position + len(cluster),
    }
    cue = element(0xB3, 1501 if bad_timestamp else 1500) + element(
        0xB7,
        element(0xF7, 7)
        + element(0xF1, cluster_position)
        + element(0xF0, len(timestamp))
        + element(0xB2, 4000),
    )
    index = element(0x1C53BB6B, element(0xBB, cue))
    return element(0x1A45DFA3, element(0x4282, b"matroska")) + element(
        0x18538067, seek_head(tags) + info + tracks + cluster + index
    )


TRACK = PlexStream("7", "subtitle", "srt", "en", False, False, False, 0)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_indexed_text_fragment_keeps_cue_crossing_the_seek_boundary():
    body = fixture()

    async def read(offset, size):
        assert 0 <= offset < offset + size <= len(body)
        return body[offset : offset + size]

    tracks = await matroska.indexed_tracks(read, len(body), (TRACK,))
    assert len(tracks) == 1 and tracks[0].stream == TRACK
    fragment = await matroska.fragment(read, len(body), tracks[0], 5000, 10000, 900_000)
    assert b"X-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:900000" in fragment
    assert b"00:00:01.500 --> 00:00:05.500" in fragment
    assert b"<i>First cue</i> &lt;other&gt;" in fragment
    assert b"First cue" not in await matroska.fragment(
        read, len(body), tracks[0], 6000, 9000, 900_000
    )


@pytest.mark.anyio
async def test_index_does_not_accept_compressed_tracks_or_mismatched_block_times():
    body = fixture(compress=True)

    async def read(offset, size):
        return body[offset : offset + size]

    assert await matroska.indexed_tracks(read, len(body), (TRACK,)) == ()
    body = fixture(bad_timestamp=True)
    tracks = await matroska.indexed_tracks(read, len(body), (TRACK,))
    with pytest.raises(ValueError, match="timestamp"):
        await matroska.fragment(read, len(body), tracks[0], 5000, 10000, 900_000)
