"""Actual JSON CLI, SQLite boundaries and optional MCP wire protocol contracts."""

import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from omakase.plus.agent_cli import agent_cli
from omakase.plus.agent_service import AgentError, AgentService, PreferencesPatch
from omakase.plus.db import run_migrations


@pytest.fixture
def account(tmp_path, monkeypatch):
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "true")
    monkeypatch.delenv("OMAKASE_PLUS_REVIEW_MODE", raising=False)
    db = sqlite3.connect(tmp_path / "omakase-plus.db")
    run_migrations(db)
    db.executemany(
        "INSERT INTO users(id,email,password_hash) VALUES(?,?,?)",
        [
            (1, "one@example.invalid", "hidden-password-hash"),
            (2, "two@example.invalid", "other-password-hash"),
        ],
    )
    for n in (1, 2, 3):
        db.execute(
            "INSERT INTO media_monitors(id,user_id,anilist_id,title) VALUES(?,?,?,?)",
            (n, 1 if n < 3 else 2, n, "Own title" if n < 3 else "Other account private title"),
        )
    db.execute(
        "INSERT INTO movie_catalog(user_id,imdb_id,tmdb_id,title,year,updated_at) VALUES(1,'tt0084787',1091,'The Thing',1982,datetime('now'))"
    )
    db.execute(
        "INSERT INTO tv_catalog VALUES(1,1396,?,datetime('now'))",
        (json.dumps({"title": "Breaking Bad"}),),
    )
    db.commit()
    db.close()
    return tmp_path


def test_reads_are_offline_bounded_and_account_scoped(account, monkeypatch):
    import socket

    monkeypatch.setattr(socket, "socket", lambda *a, **k: pytest.fail("unexpected network"))
    before = (account / "omakase-plus.db").read_bytes()
    service = AgentService(account, 1)
    assert service.capabilities()["counts"] == {"anime": 2, "movies": 1, "tv": 1}
    first = service.inventory("anime", 1)
    assert first["next_offset"] == 1
    assert service.inventory("anime", 1, first["next_offset"])["next_offset"] is None
    assert len(service.inventory("movies")["items"]) == 1
    assert service.inventory("tv")["items"][0]["title"] == "Breaking Bad"
    result = json.dumps([service.inventory("anime"), service.preferences(), service.audit()])
    assert "Other account" not in result and "password" not in result
    assert (account / "omakase-plus.db").read_bytes() == before
    with service._db() as db, pytest.raises(sqlite3.OperationalError):
        db.execute("DELETE FROM users")


@pytest.mark.parametrize(
    "args", [("anime", True, 0), ("anime", 101, 0), ("movies", 25, -1), ("sql", 25, 0)]
)
def test_bad_inventory_rejected(account, args):
    with pytest.raises(AgentError, match="bounds|Choose"):
        AgentService(account, 1).inventory(*args)


def test_missing_database_never_created(tmp_path, monkeypatch):
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "true")
    with pytest.raises(AgentError, match="database"):
        AgentService(tmp_path, 1)
    assert not list(tmp_path.iterdir())


def test_private_and_account_boundary(account, monkeypatch):
    with pytest.raises(AgentError, match="account"):
        AgentService(account, 99)
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "false")
    with pytest.raises(AgentError, match="private"):
        AgentService(account, 1)


def test_explicit_write_scope_and_rollback(account, monkeypatch):
    patch = PreferencesPatch(count=5, completed_only=True)
    with pytest.raises(AgentError, match="scope"):
        AgentService(account, 1).set_preferences(patch)
    service = AgentService(account, 1, write_scopes=["preferences"])
    assert service.set_preferences(patch)["count"] == 5
    assert AgentService(account, 2).preferences()["count"] == 8
    assert json.loads(service.audit()["items"][0]["changed_fields_json"]) == [
        "completed_only",
        "count",
    ]
    with pytest.raises(RuntimeError), service._db("preferences") as db:
        db.execute("UPDATE discovery_preferences SET preferences='{}' WHERE user_id=1")
        raise RuntimeError("force rollback")
    assert service.preferences()["count"] == 5
    monkeypatch.setenv("OMAKASE_PLUS_REVIEW_MODE", "true")
    with pytest.raises(AgentError, match="Review"):
        service.set_preferences(patch)


def test_monitor_scope_ownership_and_idempotence(account):
    service = AgentService(account, 1, write_scopes=["monitors"])
    with pytest.raises(AgentError, match="own"):
        service.set_monitor_active(3, False)
    assert service.set_monitor_active(1, False)["changed"]
    assert not service.set_monitor_active(1, False)["changed"]
    assert len(service.audit()["items"]) == 1
    assert AgentService(account, 2).inventory("anime")["items"][0]["active"] == 1


@pytest.mark.parametrize(
    "patch",
    [
        {"count": True},
        {"count": 16},
        {"completed_only": "yes"},
        {"shell": "rm"},
        {"mood": "x" * 501},
    ],
)
def test_strict_preferences(patch):
    with pytest.raises(ValidationError):
        PreferencesPatch.model_validate(patch)


def test_cli_json_and_denied_write(account):
    runner = CliRunner()
    args = ["--data-dir", str(account), "--user-id", "1"]
    good = runner.invoke(agent_cli, [*args, "status"])
    assert good.exit_code == 0, good.output
    assert json.loads(good.output)["result"]["account_id"] == 1
    denied = runner.invoke(agent_cli, [*args, "set-preferences", '{"count":5}'])
    assert denied.exit_code == 2
    assert json.loads(denied.output)["error"]["code"] == "scope_denied"
    saved = runner.invoke(
        agent_cli, [*args, "--allow-write", "preferences", "set-preferences", '{"count":5}']
    )
    assert saved.exit_code == 0 and json.loads(saved.output)["result"]["count"] == 5


def test_real_mcp_subprocess(account):
    pytest.importorskip("mcp")
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters

    async def exercise():
        env = {
            k: v
            for k, v in os.environ.items()
            if k in {"PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "USERPROFILE"}
        }
        env["OMAKASE_PLUS_PRIVATE"] = "true"
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "omakase.cli",
                "plus",
                "agent",
                "--data-dir",
                str(account),
                "--user-id",
                "1",
                "mcp",
            ],
            env=env,
            cwd=str(Path(__file__).resolve().parents[2]),
        )
        async with Client(params, read_timeout_seconds=10) as client:
            tools = (await client.list_tools()).tools
            names = {t.name for t in tools}
            assert names == {
                "omakase_status",
                "omakase_inventory",
                "omakase_preferences",
                "omakase_audit",
            }
            assert all(t.annotations.read_only_hint for t in tools)
            result = await client.call_tool("omakase_status", {})
            assert not result.is_error
            assert result.structured_content is not None, result.model_dump()
            payload = result.structured_content
            assert payload["account_id"] == 1
            invalid = await client.call_tool("omakase_inventory", {"kind": "anime", "limit": 101})
            assert invalid.is_error
            denied = await client.call_tool("omakase_set_preferences", {"patch": {"count": 5}})
            assert denied.is_error

    asyncio.run(exercise())


def test_real_mcp_enabled_write_and_audit(account):
    pytest.importorskip("mcp")
    from mcp import Client

    from omakase.plus.agent_mcp import create_server

    async def exercise():
        server = create_server(AgentService(account, 1, write_scopes=["preferences"]))
        async with Client(server, read_timeout_seconds=10) as client:
            tools = (await client.list_tools()).tools
            assert "omakase_set_preferences" in {t.name for t in tools}
            assert "omakase_set_monitor_active" not in {t.name for t in tools}
            changed = await client.call_tool("omakase_set_preferences", {"patch": {"count": 6}})
            assert not changed.is_error and changed.structured_content["count"] == 6
            audit = await client.call_tool("omakase_audit", {})
            assert audit.structured_content["items"][0]["operation"] == "preferences"
            wrong = await client.call_tool(
                "omakase_set_preferences", {"patch": {"api_key": "do-not-save"}}
            )
            assert wrong.is_error

    asyncio.run(exercise())


def test_help_page_private_authenticated_and_escaped(account, monkeypatch):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.templating import Jinja2Templates
    from fastapi.testclient import TestClient

    from omakase.plus.agent_routes import create_agent_router
    from omakase.plus.middleware import require_user

    app = FastAPI()
    templates = Jinja2Templates(
        directory=str(Path(__file__).resolve().parents[2] / "src/omakase/plus/templates")
    )
    templates.env.globals["asset_version"] = "fixture"
    app.include_router(
        create_agent_router(
            templates, lambda request, user, **kwargs: {"email": "safe@example.invalid", **kwargs}
        ),
        prefix="/plus",
    )
    with TestClient(app) as client:
        # Override auth only after proving the unauthenticated route has no content.
        from omakase.plus.deps import get_db

        db = sqlite3.connect(account / "omakase-plus.db", check_same_thread=False)
        db.row_factory = sqlite3.Row
        app.dependency_overrides[get_db] = lambda: db
        assert client.get("/plus/settings/agents").status_code == 401
        app.dependency_overrides[require_user] = lambda: SimpleNamespace(
            id=1, email="safe@example.invalid"
        )
        response = client.get("/plus/settings/agents")
        assert response.status_code == 200
        assert "No open port" in response.text and 'aria-current="page"' in response.text
        assert response.headers["cache-control"] == "private, no-store"
        monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "false")
        assert client.get("/plus/settings/agents").status_code == 404
        db.close()


def test_audit_failure_rolls_back_saved_preferences(account):
    service = AgentService(account, 1, write_scopes=["preferences"])
    with sqlite3.connect(account / "omakase-plus.db") as db:
        db.execute(
            "CREATE TRIGGER fail_agent_audit BEFORE INSERT ON agent_audit BEGIN SELECT RAISE(ABORT,'fixture'); END"
        )
    with pytest.raises(AgentError, match="database"):
        service.set_preferences(PreferencesPatch(count=6))
    assert service.preferences()["count"] == 8


@pytest.mark.parametrize("mode", ["true", "yes", "1", "on", " TRUE ", " On "])
def test_all_review_mode_spellings_deny_writes(account, monkeypatch, mode):
    service = AgentService(account, 1, write_scopes=["preferences"])
    monkeypatch.setenv("OMAKASE_PLUS_REVIEW_MODE", mode)
    with pytest.raises(AgentError) as error:
        service.set_preferences(PreferencesPatch(count=5))
    assert error.value.code == "review_read_only"
    assert not service.audit()["items"]


def test_mcp_domain_error_preserves_safe_code(account):
    pytest.importorskip("mcp")
    from mcp import Client

    from omakase.plus.agent_mcp import create_server

    async def exercise():
        server = create_server(AgentService(account, 1, write_scopes=["monitors"]))
        async with Client(server, read_timeout_seconds=10) as client:
            response = await client.call_tool(
                "omakase_set_monitor_active", {"monitor_id": 3, "active": False}
            )
            assert response.is_error
            content = " ".join(getattr(block, "text", "") for block in response.content)
            assert "not_found" in content and "does not own" in content
            assert "Other account" not in content and "Traceback" not in content

    asyncio.run(exercise())
