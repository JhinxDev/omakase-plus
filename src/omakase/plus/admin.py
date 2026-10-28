"""CLI admin commands for Omakase Plus.

Usage::

    python -m omakase.plus.admin seed-user
    python -m omakase.plus.admin list-users
"""

from __future__ import annotations

import os
import sys

import click

from omakase.plus.auth import hash_password
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


if __name__ == "__main__":
    admin()
