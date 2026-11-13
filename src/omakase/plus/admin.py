"""CLI admin commands for Omakase Plus.

Usage::

    python -m omakase.plus.admin seed-user
    python -m omakase.plus.admin list-users
    python -m omakase.plus.admin set-password --email owner@example.com
    python -m omakase.plus.admin revoke-sessions --email owner@example.com
"""

from __future__ import annotations

import os
import sys

import click

from omakase.plus.auth import (
    hash_password,
    revoke_user_sessions,
    validate_new_password,
)
from omakase.plus.auth import (
    set_password as _set_password,
)
from omakase.plus.db import get_db


@click.group()
def admin():
    """Omakase Plus administration commands."""


@admin.command()
def seed_user():
    """Seed a user from ``OMAKASE_SEED_EMAIL`` / ``OMAKASE_SEED_PASSWORD``.

    The user is created only if no row with that email exists.
    """
    email = os.getenv("OMAKASE_SEED_EMAIL")
    password = os.getenv("OMAKASE_SEED_PASSWORD")

    if not email or not password:
        click.echo(
            "ERROR: OMAKASE_SEED_EMAIL and OMAKASE_SEED_PASSWORD must both be set.", err=True
        )
        sys.exit(1)

    conn = get_db()
    existing = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()

    if existing:
        click.echo(f"User '{email}' already exists (id={existing['id']}).")
        return

    pw_hash = hash_password(password)
    cursor = conn.execute(
        "INSERT INTO users (email, password_hash) VALUES (?, ?)",
        (email, pw_hash),
    )
    conn.commit()
    click.echo(f"Created user '{email}' (id={cursor.lastrowid}).")


@admin.command()
def list_users():
    """List all registered users."""
    conn = get_db()
    rows = conn.execute("SELECT id, email, created_at FROM users ORDER BY id").fetchall()

    if not rows:
        click.echo("No users found.")
        return

    click.echo(f"{'ID':<5} {'Email':<40} {'Created at':<20}")
    click.echo("-" * 65)
    for row in rows:
        click.echo(f"{row['id']:<5} {row['email']:<40} {row['created_at']:<20}")


@admin.command("set-password")
@click.option("--email", required=True, help="Account email to update.")
@click.option(
    "--password",
    default="",
    help="New password. When omitted, uses OMAKASE_ADMIN_NEW_PASSWORD or prompts.",
)
def set_password_command(email: str, password: str):
    """Reset a user's password and revoke all of their sessions.

    This is the operator recovery path. The new password is never logged or
    written outside the Argon2 hash.
    """
    conn = get_db()
    row = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if row is None:
        click.echo(f"ERROR: no user with email '{email}'.", err=True)
        sys.exit(1)

    new_password = password or os.getenv("OMAKASE_ADMIN_NEW_PASSWORD", "")
    if not new_password:
        new_password = click.prompt("New password", hide_input=True, confirmation_prompt=True)
    problem = validate_new_password(new_password)
    if problem is not None:
        click.echo(f"ERROR: {problem}", err=True)
        sys.exit(1)

    _set_password(conn, row["id"], new_password)
    click.echo(f"Password updated for '{email}' (id={row['id']}); all sessions were revoked.")


@admin.command("revoke-sessions")
@click.option("--email", required=True, help="Account email whose sessions to revoke.")
def revoke_sessions_command(email: str):
    """Sign a user out everywhere without changing their password."""
    conn = get_db()
    row = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if row is None:
        click.echo(f"ERROR: no user with email '{email}'.", err=True)
        sys.exit(1)

    count = revoke_user_sessions(conn, row["id"])
    click.echo(f"Revoked {count} session(s) for '{email}' (id={row['id']}).")


if __name__ == "__main__":
    admin()
