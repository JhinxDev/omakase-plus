"""The real response policy renews cookies and preserves explicit logout."""

from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.testclient import TestClient

from omakase.web.server import plus_security_headers


def test_response_renews_secure_persistent_cookie(monkeypatch):
    monkeypatch.setenv("OMAKASE_SESSION_COOKIE_SECURE", "true")
    monkeypatch.delenv("OMAKASE_SESSION_DAYS", raising=False)
    app = FastAPI()
    app.middleware("http")(plus_security_headers)

    @app.get("/plus/home")
    async def home(request: Request):
        request.state.session_cookie_refresh = "renewed"
        return Response("ok")

    response = TestClient(app, base_url="https://testserver").get("/plus/home")
    cookie = response.headers["set-cookie"]
    assert "omakase_session=renewed" in cookie
    assert "Max-Age=7776000" in cookie
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie
    assert "Path=/plus" in cookie


def test_logout_cookie_wins_over_refresh():
    app = FastAPI()
    app.middleware("http")(plus_security_headers)

    @app.get("/plus/logout")
    async def logout(request: Request):
        request.state.session_cookie_refresh = "old"
        response = Response("ok")
        response.delete_cookie("omakase_session", path="/plus")
        return response

    response = TestClient(app).get("/plus/logout")
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert "omakase_session=old" not in response.headers["set-cookie"]
