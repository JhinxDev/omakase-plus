"""Public BYOK request credentials and profiles stay request-scoped."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from omakase.types import Recommendation
from omakase.web.server import app, recommend


def test_recommendation_request_does_not_persist_profile_or_stage_environment(
    monkeypatch,
):
    monkeypatch.delenv("OMAKASE_API_KEY", raising=False)
    monkeypatch.delenv("MAL_CLIENT_ID", raising=False)
    captured = {}

    def fake_pipeline(config):
        captured["config"] = config
        captured["profile_path"] = config.profile_path
        assert Path(config.profile_path).read_text(encoding="utf-8") == "Private taste"
        return [
            Recommendation(
                title="Fresh",
                predicted_score=8,
                reasoning="fit",
                best_match_from_history="Loved",
            )
        ]

    monkeypatch.setitem(recommend.__globals__, "run_pipeline", fake_pipeline)
    monkeypatch.setitem(
        recommend.__globals__,
        "validate_user_outbound_url",
        lambda *_args, **_kwargs: "https://api.openai.com",
    )
    response = TestClient(app).post(
        "/api/recommend",
        json={
            "source": "anilist",
            "username": "viewer",
            "profile": "Private taste",
            "llm_type": "openai",
            "llm_url": "https://api.openai.com",
            "model": "gpt-4o-mini",
            "api_key": "request-key",
            "mal_client_id": "request-mal-client",
        },
    )

    assert response.status_code == 200
    assert captured["config"].api_key == "request-key"
    assert captured["config"].mal_client_id == "request-mal-client"
    assert not Path(captured["profile_path"]).exists()
    assert "OMAKASE_API_KEY" not in __import__("os").environ
    assert "MAL_CLIENT_ID" not in __import__("os").environ
