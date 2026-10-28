"""First-run guidance stays private, scoped and free of implicit service work."""

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from omakase.plus.auth import create_session
from omakase.plus.db import run_migrations
from omakase.plus.deps import get_db
from omakase.plus.secrets import read_secret, store_secret
from omakase.plus.web_security import csrf_token


@pytest.fixture
def browser(tmp_path, monkeypatch):
    from omakase.plus.onboarding_routes import create_onboarding_router

    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "true")
    conn = sqlite3.connect(tmp_path / "onboarding.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    run_migrations(conn)
    for uid in (1, 2):
        conn.execute(
            "INSERT INTO users(id,email,password_hash) VALUES (?,?,?)",
            (uid, f"viewer{uid}@example.invalid", "synthetic"),
        )
    conn.commit()
    app = FastAPI()
    templates = Jinja2Templates(
        directory=str(Path(__file__).parents[2] / "src/omakase/plus/templates")
    )
    templates.env.globals["asset_version"] = "test"
    app.include_router(create_onboarding_router(templates), prefix="/plus")
    app.dependency_overrides[get_db] = lambda: conn
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as client:
        yield client, conn
    conn.close()


def login(client, db, uid=1):
    session = create_session(db, uid)
    client.cookies.set("omakase_session", session, domain="testserver.local", path="/plus")
    return csrf_token(
        Request({"type": "http", "headers": [(b"cookie", f"omakase_session={session}".encode())]})
    )


def test_all_routes_require_login(browser):
    client, _ = browser
    assert client.get("/plus/onboarding").status_code == 401
    for action in ("intent", "finish"):
        assert client.post(f"/plus/onboarding/{action}").status_code == 401


@pytest.mark.parametrize("action", ["intent", "finish"])
def test_mutations_require_csrf_without_browser_headers(browser, action):
    client, db = browser
    login(client, db)
    assert client.post(f"/plus/onboarding/{action}", data={"intent": "local"}).status_code == 403
    assert read_secret(db, 1, "onboarding_intent") is None
    assert read_secret(db, 1, "onboarding_completed") is None


def test_intent_is_encrypted_scoped_and_cannot_change_runtime(browser, monkeypatch):
    client, db = browser
    monkeypatch.setenv("OMAKASE_ACQUISITION_MODE", "import-only")
    token = login(client, db)
    store_secret(db, 1, "realdebrid_api_key", "keep-this-key")
    response = client.post("/plus/onboarding/intent", data={"csrf_token": token, "intent": "cloud"})
    assert response.status_code == 303
    assert response.headers["location"] == "/plus/onboarding?step=2"
    assert read_secret(db, 1, "onboarding_intent") == "cloud"
    assert read_secret(db, 2, "onboarding_intent") is None
    assert read_secret(db, 1, "realdebrid_api_key") == "keep-this-key"
    import os

    assert os.environ["OMAKASE_ACQUISITION_MODE"] == "import-only"
    assert (
        db.execute(
            "SELECT encrypted_value FROM user_secrets WHERE key_name='onboarding_intent'"
        ).fetchone()[0]
        != "cloud"
    )
    assert "keep-this-key" not in client.get("/plus/onboarding?step=3").text


def test_get_only_uses_saved_receipts_and_makes_no_network_calls(browser, monkeypatch):
    from omakase.plus import connections

    client, db = browser
    login(client, db)

    async def forbidden(*args, **kwargs):
        pytest.fail("GET must not test or contact services")

    monkeypatch.setattr(connections, "test_connection", forbidden)
    monkeypatch.setattr(connections, "_resolved_ips", forbidden)
    before = db.total_changes
    for step in range(1, 5):
        response = client.get(f"/plus/onboarding?step={step}")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, no-store"
        assert f"Step {step} of 4" in response.text
    assert db.total_changes == before


def test_invalid_forms_and_steps_are_bounded(browser):
    client, db = browser
    token = login(client, db)
    for intent in ("nas:/private", "", "cloud&local"):
        assert (
            client.post(
                "/plus/onboarding/intent", data={"csrf_token": token, "intent": intent}
            ).status_code
            == 422
        )
    assert client.get("/plus/onboarding?step=0").status_code == 422
    assert client.get("/plus/onboarding?step=5").status_code == 422
    assert (
        client.post(
            "/plus/onboarding/intent",
            content="x" * 5000,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ).status_code
        == 413
    )
    assert (
        client.post(
            "/plus/onboarding/intent",
            content=f"csrf_token={token}&intent=local&intent=cloud",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ).status_code
        == 422
    )


def test_local_guidance_and_finish_do_not_require_movie_services(browser):
    client, db = browser
    token = login(client, db)
    assert (
        client.post(
            "/plus/onboarding/intent", data={"csrf_token": token, "intent": "local"}
        ).status_code
        == 303
    )
    page = client.get("/plus/onboarding?step=2").text
    assert "mounted on the server" in page
    assert "connection-plex" in page
    response = client.post("/plus/onboarding/finish", data={"csrf_token": token})
    assert response.status_code == 303
    assert response.headers["location"] == "/plus/home"
    assert read_secret(db, 1, "onboarding_completed") == "true"
    assert client.get("/plus/onboarding").status_code == 200
    assert read_secret(db, 1, "onboarding_intent") == "local"


def test_review_receipts_are_user_scoped_and_never_expose_addresses(browser):
    from omakase.plus import connections

    client, db = browser
    login(client, db)
    store_secret(db, 1, "plex_url", "https://private-plex.example.invalid")
    store_secret(db, 1, "plex_token", "secret-fixture-plex-token")
    store_secret(db, 2, "onboarding_intent", "local")
    store_secret(db, 2, "onboarding_completed", "true")
    own = client.get("/plus/onboarding?step=3")
    assert own.status_code == 200
    assert "private-plex.example.invalid" not in own.text
    assert "secret-fixture-plex-token" not in own.text
    assert "Reviewing your setup" not in own.text
    assert "Explore first" in client.get("/plus/onboarding?step=2").text
    login(client, db, 2)
    other = client.get("/plus/onboarding?step=2")
    assert "Local or NAS library" in other.text
    assert "Reviewing your setup" in other.text
    assert connections.connection_cards(db, 2)[0]["key_saved"] is False


def test_public_edition_has_no_onboarding_routes(browser, monkeypatch):
    client, db = browser
    login(client, db)
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "false")
    assert client.get("/plus/onboarding").status_code == 404
    assert client.post("/plus/onboarding/finish").status_code == 404


@pytest.mark.parametrize("setup_path", ["standalone", "existing", "fresh_plex"])
def test_setup_paths_are_optional_scoped_guidance(browser, setup_path):
    client, db = browser
    token = login(client, db)
    response = client.post(
        "/plus/onboarding/intent",
        data={"csrf_token": token, "intent": "explore", "setup_path": setup_path},
    )
    assert response.status_code == 303
    assert read_secret(db, 1, "onboarding_setup_path") == setup_path
    assert read_secret(db, 2, "onboarding_setup_path") is None
    page = client.get("/plus/onboarding?step=2")
    assert page.status_code == 200
    if setup_path == "standalone":
        assert "compose.standalone.yaml" in page.text
        assert "no required media mounts" in page.text
    elif setup_path == "fresh_plex":
        assert "--profile plex config --quiet" in page.text
        assert "claims no account" in page.text
    else:
        assert "Connecting does not install software or move media" in page.text


def test_old_forms_preserve_saved_setup_path(browser):
    client, db = browser
    token = login(client, db)
    store_secret(db, 1, "onboarding_setup_path", "existing")
    response = client.post("/plus/onboarding/intent", data={"csrf_token": token, "intent": "local"})
    assert response.status_code == 303
    assert read_secret(db, 1, "onboarding_setup_path") == "existing"
    assert read_secret(db, 1, "onboarding_intent") == "local"


def test_invalid_or_duplicate_setup_path_does_not_save_intent(browser):
    client, db = browser
    token = login(client, db)
    for suffix in ("setup_path=install", "setup_path=existing&setup_path=fresh_plex"):
        response = client.post(
            "/plus/onboarding/intent",
            content=f"csrf_token={token}&intent=local&{suffix}",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert response.status_code == 422
    assert read_secret(db, 1, "onboarding_intent") is None
    assert read_secret(db, 1, "onboarding_setup_path") is None


def test_standalone_compose_has_only_app_data_and_no_media_requirements():
    root = Path(__file__).parents[2]
    compose = (root / "compose.standalone.yaml").read_text(encoding="utf-8")
    assert "${OMAKASE_BIND_ADDRESS:-127.0.0.1}" in compose
    assert compose.count("      - type: bind") == 1
    assert "target: /home/omakase/data" in compose
    for forbidden in (
        "OMAKASE_IMPORT_PATH",
        "OMAKASE_LIBRARY_PATH",
        "OMAKASE_IMPORT_ROOT",
        "OMAKASE_LIBRARY_ROOT",
        "docker.sock",
        "  plex:",
        "  qbittorrent:",
    ):
        assert forbidden not in compose
    assert 'OMAKASE_PLUS_PRIVATE: "true"' in compose
    assert "OMAKASE_ACQUISITION_MODE: import-only" in compose
    assert "OMAKASE_VPN_MODE: none" in compose


def test_standalone_compose_renders_without_media_paths(tmp_path):
    docker = shutil.which("docker")
    if not docker:
        pytest.skip("Docker Compose CLI is not installed")
    version = subprocess.run([docker, "compose", "version"], capture_output=True, timeout=15)
    if version.returncode:
        pytest.skip("Docker Compose plugin is not installed")
    # Isolate from real dotenv/secret files and prove empty media settings suffice.
    source = Path(__file__).parents[2] / "compose.standalone.yaml"
    config_path = tmp_path / source.name
    config_path.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    env_path = tmp_path / "empty.env"
    env_path.write_text("", encoding="utf-8")
    result = subprocess.run(
        [
            docker,
            "compose",
            "--env-file",
            str(env_path),
            "-f",
            str(config_path),
            "config",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, "Standalone Compose could not render"
    services = json.loads(result.stdout)["services"]
    assert set(services) == {"omakase-plus"}
    app = services["omakase-plus"]
    assert [volume["target"] for volume in app["volumes"]] == ["/home/omakase/data"]
    assert app["ports"][0]["host_ip"] == "127.0.0.1"
    assert app["environment"]["OMAKASE_ACQUISITION_MODE"] == "import-only"
