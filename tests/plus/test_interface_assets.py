"""A rendered stylesheet must be served by the real route, not just disk QA."""

import json
from html.parser import HTMLParser
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from omakase.plus import routes


class Stylesheets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "link" and values.get("rel") == "stylesheet":
            self.urls.append(values["href"])


def test_every_base_stylesheet_is_served_with_exact_source_bytes():
    document = routes.templates.env.get_template("base.html").render(
        email="fixture@example.test", active_page="home", households_enabled=True
    )
    styles = Stylesheets()
    styles.feed(document)
    assert any("/interface.css?" in url for url in styles.urls)
    app = FastAPI()
    app.include_router(routes.router)
    source = Path(routes.__file__).parent / "static"
    with TestClient(app) as client:
        for url in styles.urls:
            response = client.get(url)
            assert response.status_code == 200, url
            assert response.headers["content-type"].startswith("text/css"), url
            assert (
                response.content == (source / url.split("?", 1)[0].rsplit("/", 1)[-1]).read_bytes()
            )


def test_renderer_source_index_and_bounded_source_download_are_deliverable():
    app = FastAPI()
    app.include_router(routes.router)
    source = Path(routes.__file__).parent / "static/ass"
    with TestClient(app) as client:
        response = client.get("/plus/static/ass/SOURCE.json")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        assert response.json() == json.loads((source / "SOURCE.json").read_text())
        name = response.json()["bundle"]
        response = client.get("/plus/static/ass/" + name, headers={"range": "bytes=0-127"})
        assert response.status_code == 206
        assert response.headers["content-type"].startswith("application/gzip")
        with (source / name).open("rb") as stream:
            assert response.content == stream.read(128)
        assert client.get("/plus/static/ass/unlisted.tar.gz").status_code == 404


def test_ass_worker_isolation_applies_to_compressed_and_range_responses_only():
    app = FastAPI()
    app.include_router(routes.router)
    with TestClient(app) as client:
        for headers, status in [
            ({"accept-encoding": "gzip"}, 200),
            ({"accept-encoding": "identity"}, 200),
            ({"range": "bytes=0-15"}, 206),
        ]:
            response = client.get("/plus/static/ass/worker.js", headers=headers)
            assert response.status_code == status
            assert response.headers["cross-origin-embedder-policy"] == "require-corp"
        assert (
            "cross-origin-embedder-policy" not in client.get("/plus/static/ass/jassub.js").headers
        )


def test_synthetic_preview_worker_isolation_uses_normalized_native_paths(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from scripts.player_preview import preview

    for name in ("OMAKASE_PLUS_PRIVATE", "OMAKASE_PLUS_WEB_PLAYER_ENABLED", "OMAKASE_DATA_DIR"):
        monkeypatch.setenv(name, "")
    media = tmp_path / "synthetic.mp4"
    media.write_bytes(b"x" * 2048)
    app = preview(media, str(tmp_path / "data"))
    response = TestClient(app).get("/plus/static/ass/worker.js")
    assert response.status_code == 200
    assert response.headers["cross-origin-embedder-policy"] == "require-corp"
