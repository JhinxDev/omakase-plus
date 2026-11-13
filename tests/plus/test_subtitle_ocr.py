from omakase.plus.debrid_subs import SubtitleCache


def test_srt_to_webvtt_preserves_cues():
    data = b"1\r\n00:00:01,668 --> 00:00:04,588\r\nHello there\r\n\r\n2\r\n00:00:07,633 --> 00:00:09,676\r\nSecond line\r\n"
    result = SubtitleCache._srt_vtt(data)
    text = result.decode()
    assert text.startswith("WEBVTT\n\n")
    assert "00:00:01.668 --> 00:00:04.588" in text
    assert "Hello there" in text
    assert "00:00:07.633 --> 00:00:09.676" in text
    assert "Second line" in text


def test_srt_to_webvtt_rejects_empty_input():
    assert SubtitleCache._srt_vtt(b"") == b""


def test_asr_segments_produce_webvtt():
    result = SubtitleCache._vtt_from_segments(
        [(2.03, 11.29, "First line"), (11.29, 18.29, "Second line")]
    )
    text = result.decode()
    assert text.startswith("WEBVTT\n\n")
    assert "00:00:02.030 --> 00:00:11.290" in text
    assert "First line" in text
    assert "Second line" in text


def test_asr_segments_drop_empty_rows():
    assert SubtitleCache._vtt_from_segments([(1.0, 2.0, "")]) == b""
