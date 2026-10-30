"""A rendered stylesheet must be served by the real route, not just disk QA."""

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
