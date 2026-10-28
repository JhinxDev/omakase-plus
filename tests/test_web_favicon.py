from fastapi.testclient import TestClient

from omakase.web.server import app

client = TestClient(app)


def test_home_links_static_favicon():
    response = client.get("/")

    assert response.status_code == 200
    assert '<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">' in response.text


def test_favicon_ico_compat_route_serves_svg():
    response = client.get("/favicon.ico")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert "bento" in response.text


def test_plus_responses_have_locked_down_browser_headers():
    response = client.get("/plus/not-found")

    assert response.status_code == 404
    policy = response.headers["content-security-policy"]
    assert "default-src 'self'" in policy
    assert "script-src 'self'" in policy
    assert "'unsafe-inline'" not in policy
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "private, no-store"
