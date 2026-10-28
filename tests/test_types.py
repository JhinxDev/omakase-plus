"""Tests for the model preset resolver and default URL substitution."""

from __future__ import annotations

import pytest

from omakase.types import (
    DEFAULT_URLS,
    MODEL_PRESETS,
    MediaItem,
    MediaRelation,
    resolve_model_preset,
)

OLLAMA_DEFAULT = "http://localhost:11434"


@pytest.mark.parametrize(
    "llm_type,mode,expected_supports_json",
    [
        ("ollama", "fast", True),
        ("ollama", "pro", True),
        ("anthropic", "fast", False),
        ("anthropic", "pro", False),
        ("gemini", "fast", True),
        ("gemini", "pro", True),
        ("deepseek", "fast", False),
        ("deepseek", "pro", False),
        ("openai", "fast", True),
        ("openrouter", "fast", True),
        ("openrouter", "pro", False),
    ],
)
def test_preset_supports_json(llm_type, mode, expected_supports_json):
    _, _, _, supports_json = resolve_model_preset(OLLAMA_DEFAULT, llm_type, "qwen2.5:7b", mode)
    assert supports_json is expected_supports_json


def test_resolver_substitutes_default_url_for_non_ollama_backend():
    url, _, _, _ = resolve_model_preset(OLLAMA_DEFAULT, "anthropic", "qwen2.5:7b", "fast")
    assert url == DEFAULT_URLS["anthropic"]


def test_resolver_keeps_custom_url():
    url, _, _, _ = resolve_model_preset(
        "https://my-proxy.example.com", "openai", "qwen2.5:7b", "fast"
    )
    assert url == "https://my-proxy.example.com"


def test_resolver_substitutes_preset_model_when_default_passed():
    _, _, model, _ = resolve_model_preset(OLLAMA_DEFAULT, "anthropic", "qwen2.5:7b", "fast")
    assert model == MODEL_PRESETS["anthropic-fast"]["model"]


def test_resolver_keeps_user_model_when_explicit():
    _, _, model, _ = resolve_model_preset(OLLAMA_DEFAULT, "openai", "gpt-4o-2024-11-20", "fast")
    assert model == "gpt-4o-2024-11-20"


def test_unknown_backend_returns_inputs_unchanged():
    url, llm_type, model, supports_json = resolve_model_preset(
        "https://x.example", "made-up-backend", "some-model", "fast"
    )
    assert (url, llm_type, model, supports_json) == (
        "https://x.example",
        "made-up-backend",
        "some-model",
        True,
    )


def test_all_backends_have_default_urls():
    from omakase.llm import list_backends

    for backend in list_backends():
        assert backend in DEFAULT_URLS, f"{backend} missing from DEFAULT_URLS"


def test_media_item_accepts_rich_relation_metadata():
    item = MediaItem(
        id=2,
        title_romaji="Example Season 2",
        season="SPRING",
        season_year=2026,
        start_date="2026-04-01",
        next_airing_episode=4,
        next_airing_at=1776200000,
        relations=[
            MediaRelation(
                relation_type="PREQUEL",
                media_id=1,
                title_romaji="Example",
                title_english="Example",
                format="TV",
                status="FINISHED",
                episodes=12,
                season="WINTER",
                season_year=2025,
            )
        ],
    )
    assert item.relations[0].relation_type == "PREQUEL"
    assert item.next_airing_episode == 4
