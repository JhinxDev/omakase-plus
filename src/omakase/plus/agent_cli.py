"""Machine-readable local agent entry point; stdout is JSON or MCP, never logs."""

from __future__ import annotations

import json

import click
from pydantic import ValidationError

from omakase.plus.agent_service import AgentError, AgentService, PreferencesPatch


@click.group("agent")
@click.option("--data-dir", type=click.Path(file_okay=False, exists=True), required=True)
@click.option("--user-id", type=click.IntRange(min=1), required=True)
@click.option("--allow-write", multiple=True, type=click.Choice(["preferences", "monitors"]))
@click.pass_context
def agent_cli(ctx, data_dir, user_id, allow_write):
    """Offline, account-bound tools. Writes require named startup scopes."""
    try:
        ctx.obj = AgentService(data_dir, user_id, write_scopes=allow_write)
    except AgentError as exc:
        _error(ctx, exc.code, exc.detail)


def _error(ctx, code, detail):
    click.echo(json.dumps({"ok": False, "error": {"code": code, "detail": detail}}))
    ctx.exit(2)


def _run(ctx, operation):
    try:
        click.echo(
            json.dumps({"ok": True, "result": operation()}, ensure_ascii=True, allow_nan=False)
        )
    except AgentError as exc:
        _error(ctx, exc.code, exc.detail)
    except (ValueError, TypeError, ValidationError):
        _error(ctx, "invalid_input", "Arguments do not match the supported schema.")


@agent_cli.command("status")
@click.pass_context
def status(ctx):
    """List capabilities, scope, counts and existing operator commands."""
    _run(ctx, ctx.obj.capabilities)


@agent_cli.command("inventory")
@click.argument("kind", type=click.Choice(["anime", "movies", "tv"]))
@click.option("--limit", type=click.IntRange(1, 100), default=25)
@click.option("--offset", type=click.IntRange(0, 10000), default=0)
@click.pass_context
def inventory(ctx, kind, limit, offset):
    """Read a bounded page of cached catalog, progress and readiness."""
    _run(ctx, lambda: ctx.obj.inventory(kind, limit, offset))


@agent_cli.command("preferences")
@click.pass_context
def preferences(ctx):
    """Read saved recommendation defaults without using an AI provider."""
    _run(ctx, ctx.obj.preferences)


@agent_cli.command("set-preferences")
@click.argument("patch")
@click.pass_context
def set_preferences(ctx, patch):
    """Apply a small JSON PATCH object (not JSON Patch operations)."""

    def save():
        if len(patch.encode()) > 4096:
            raise AgentError("invalid_input", "Preference input exceeds 4 KiB.")
        return ctx.obj.set_preferences(PreferencesPatch.model_validate_json(patch))

    _run(ctx, save)


@agent_cli.command("set-monitor")
@click.argument("monitor_id", type=click.IntRange(min=1))
@click.option("--active/--inactive", required=True)
@click.pass_context
def set_monitor(ctx, monitor_id, active):
    """Pause or resume one owned monitor. Resume enables its existing scheduler."""
    _run(ctx, lambda: ctx.obj.set_monitor_active(monitor_id, active))


@agent_cli.command("audit")
@click.option("--limit", type=click.IntRange(1, 100), default=25)
@click.option("--offset", type=click.IntRange(0, 10000), default=0)
@click.pass_context
def audit(ctx, limit, offset):
    """Read this account's agent changes (metadata only)."""
    _run(ctx, lambda: ctx.obj.audit(limit, offset))


@agent_cli.command("mcp")
@click.pass_context
def mcp(ctx):
    """Run optional official-SDK MCP over stdio. No listening TCP port."""
    try:
        from omakase.plus.agent_mcp import create_server
    except ImportError:
        raise click.ClickException(
            "Install the optional agents extra: uv sync --frozen --extra agents"
        ) from None
    create_server(ctx.obj).run(transport="stdio")
