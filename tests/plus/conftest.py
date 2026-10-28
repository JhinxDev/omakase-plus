"""Keep ordinary route fixtures offline; synopsis transport has its own mocked tests."""

import pytest


@pytest.fixture(autouse=True)
def cached_title_synopses_only(monkeypatch):
    from omakase.plus import routes
    from omakase.plus.anime_synopsis import cached_synopsis

    async def fixture_synopsis(db, *, user_id, anilist_id):
        return cached_synopsis(db, anilist_id)

    monkeypatch.setattr(routes, "ensure_anime_synopsis", fixture_synopsis)
