"""Pydantic v2 models for omakase Plus database entities."""

from __future__ import annotations

from pydantic import BaseModel


class User(BaseModel):
    """A registered user."""

    id: int
    email: str
    password_hash: str
    created_at: str


class Session(BaseModel):
    """An authenticated user session."""

    id: str
    user_id: int
    expires_at: str
    created_at: str


class UserSecret(BaseModel):
    """An encrypted secret stored per user (API keys, tokens, etc.)."""

    id: int
    user_id: int
    key_name: str
    encrypted_value: str
    created_at: str


class TasteProfile(BaseModel):
    """A user's taste profile describing their anime preferences."""

    id: int
    user_id: int
    content: str
    updated_at: str


class RunHistory(BaseModel):
    """A record of a previous recommendation run."""

    id: int
    user_id: int
    source: str
    model: str
    picks: str
    created_at: str


class AniListPlanning(BaseModel):
    """An anime entry from a user's AniList Planning list."""

    id: int
    user_id: int
    anilist_id: int
    title: str
    added_at: str
    status: str = "PLANNING"


class OverseerrRequest(BaseModel):
    """A media request submitted to Overseerr."""

    id: int
    user_id: int
    anilist_planning_id: int
    overseerr_request_id: int | None = None
    status: str = "pending"
    created_at: str
