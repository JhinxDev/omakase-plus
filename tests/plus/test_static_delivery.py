"""Only fixed public assets are compressed; authenticated HTML is untouched."""

import gzip
from pathlib import Path

import pytest
from starlette.requests import Request
from starlette.responses import FileResponse


def response(tmp_path, accept, *, range_header=None):
    from omakase.plus.static_delivery import static_response

    path = tmp_path / "style.css"
    original = b".card{display:grid;color:rebeccapurple}\n" * 1000
    path.write_bytes(original)
    headers = [(b"accept-encoding", accept.encode())]
    if range_header:
        headers.append((b"range", range_header.encode()))
    request = Request({"type": "http", "headers": headers})
    return static_response(path, "text/css", request), original


def test_stylesheet_transfer_is_compressed_without_changing_decoded_bytes(tmp_path):
    result, original = response(tmp_path, "br, gzip, deflate")
    assert result.headers["content-encoding"] == "gzip"
    assert result.headers["vary"] == "Accept-Encoding"
    assert gzip.decompress(result.body) == original
    assert len(result.body) < len(original) / 10


@pytest.mark.parametrize("accept", ["", "identity", "gzip;q=0", "br, *;q=0", "gzip;q=0, *;q=1"])
def test_identity_or_explicit_gzip_rejection_keeps_file_response(tmp_path, accept):
    result, _ = response(tmp_path, accept)
    assert isinstance(result, FileResponse)
    assert "content-encoding" not in result.headers
    assert result.headers["vary"] == "Accept-Encoding"


def test_range_requests_keep_the_original_file_contract(tmp_path):
    result, _ = response(tmp_path, "gzip", range_header="bytes=0-20")
    assert isinstance(result, FileResponse)


def test_binary_font_is_not_gzipped(tmp_path):
    from omakase.plus.static_delivery import static_response

    path = tmp_path / "font.woff2"
    path.write_bytes(b"font-fixture")
    request = Request({"type": "http", "headers": [(b"accept-encoding", b"gzip")]})
    assert isinstance(static_response(path, "font/woff2", request), FileResponse)


def test_packaged_stylesheet_has_a_meaningful_wire_size_reduction():
    from omakase.plus.static_delivery import static_response

    path = Path(__file__).resolve().parents[2] / "src/omakase/plus/static/dashboard.css"
    request = Request({"type": "http", "headers": [(b"accept-encoding", b"gzip")]})
    result = static_response(path, "text/css", request)
    assert gzip.decompress(result.body) == path.read_bytes()
    assert len(result.body) < path.stat().st_size / 3
