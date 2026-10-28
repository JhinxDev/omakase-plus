"""Machine-readable private movie controls; credentials never use CLI arguments."""

from __future__ import annotations

import asyncio
import json
import os

import click

from omakase.plus.connections import movie_connections
from omakase.plus.db import _connect
from omakase.plus.movie_history import UNSET, MovieHistoryService
from omakase.plus.movies import MovieError, MovieService


@click.group("movies")
@click.option("--user-id", type=click.IntRange(min=1), required=True)
@click.option("--data-dir", type=click.Path(file_okay=False))
@click.pass_context
def movies_cli(ctx, user_id, data_dir):
    """List, request and verify exact-ID movies in the private runtime."""
    if os.getenv("OMAKASE_PLUS_PRIVATE", "").lower() not in {"1", "true", "yes", "on"}:
        raise click.ClickException("Movie controls require the private runtime")
    ctx.obj = {"user_id": user_id, "data_dir": data_dir}


def _run(ctx, operation):
    db = _connect(ctx.obj["data_dir"])
    try:
        svc = MovieService(db, movie_connections(db, ctx.obj["user_id"]))
        value = operation(svc, ctx.obj["user_id"])
        if hasattr(value, "__await__"):
            value = asyncio.run(value)
        click.echo(json.dumps({"ok": True, "result": value}, ensure_ascii=False, allow_nan=False))
    except MovieError as exc:
        click.echo(json.dumps({"ok": False, "error": exc.as_dict()}))
        ctx.exit(2)
    except (ValueError, TypeError):
        click.echo(
            json.dumps(
                {
                    "ok": False,
                    "error": {"code": "invalid_input", "detail": "Movie input is invalid"},
                }
            )
        )
        ctx.exit(2)
    finally:
        db.close()


@movies_cli.command("list")
@click.option("--view", type=click.Choice(["all", "requests", "ready", "discover"]), default="all")
@click.pass_context
def list_movies(ctx, view):
    """Read metadata and current readiness without probing video bytes."""
    _run(ctx, lambda svc, user: svc.list(user, view=view))


@movies_cli.command("search")
@click.argument("query")
@click.pass_context
def search_movies(ctx, query):
    _run(ctx, lambda svc, user: svc.search(user, query))


@movies_cli.command("request")
@click.option("--imdb-id", required=True)
@click.option("--tmdb-id", type=click.IntRange(min=1), required=True)
@click.pass_context
def request_movie(ctx, imdb_id, tmdb_id):
    """Submit one exact identity; an acknowledgement is not delivery."""
    _run(ctx, lambda svc, user: svc.request(user, imdb_id=imdb_id, tmdb_id=tmdb_id))


@movies_cli.command("verify")
@click.argument("imdb_id")
@click.pass_context
def verify_movie(ctx, imdb_id):
    """Check identity, selected English audio and bounded Plex file access."""
    _run(ctx, lambda svc, user: svc.verify(user, imdb_id))


@movies_cli.command("refresh")
@click.argument("imdb_id")
@click.pass_context
def refresh_movie(ctx, imdb_id):
    _run(ctx, lambda svc, user: svc.refresh(user, imdb_id))


@movies_cli.command("history")
@click.pass_context
def export_history(ctx):
    _run(ctx, lambda svc, user: MovieHistoryService(svc).export_history(user))


@movies_cli.command("sync-history")
@click.option("--offset", type=click.IntRange(min=0, max=1000))
@click.pass_context
def sync_history(ctx, offset):
    """Read this account's Plex watched state; preserve manual ratings."""
    _run(ctx, lambda svc, user: MovieHistoryService(svc).sync_plex(user, offset=offset))


@movies_cli.command("rate")
@click.option("--imdb-id", required=True)
@click.option("--tmdb-id", type=click.IntRange(min=1), required=True)
@click.option("--rating", type=click.FloatRange(min=1, max=10))
@click.option("--clear-rating", is_flag=True)
@click.option("--status", type=click.Choice(["unwatched", "watching", "completed", "dropped"]))
@click.pass_context
def rate_movie(ctx, imdb_id, tmdb_id, rating, clear_rating, status):
    """Save an explicit owner status/rating; never infer watched from delivery."""
    if (clear_rating and rating is not None) or (
        rating is None and not clear_rating and status is None
    ):
        raise click.UsageError("Choose a rating, clear-rating or watched status")
    _run(
        ctx,
        lambda svc, user: MovieHistoryService(svc).save(
            user,
            imdb_id=imdb_id,
            tmdb_id=tmdb_id,
            rating=None if clear_rating else rating if rating is not None else UNSET,
            status=status if status else UNSET,
        ),
    )
