"""Tests for the LLM backend registry."""

from __future__ import annotations

import pytest

from omakase.llm import get_llm, list_backends
from omakase.llm.anthropic import AnthropicLLM
from omakase.llm.gemini import GeminiLLM
from omakase.llm.ollama import OllamaLLM
from omakase.llm.openai import OpenAILLM


def test_registry_lists_all_expected_backends():
    expected = {
        "codex",
        "ollama",
        "lmstudio",
        "openai",
        "anthropic",
        "gemini",
        "deepseek",
        "openrouter",
        "groq",
        "together",
    }
    assert set(list_backends()) == expected


@pytest.mark.parametrize(
    "name,cls",
    [
        ("ollama", OllamaLLM),
        ("openai", OpenAILLM),
        ("lmstudio", OpenAILLM),
        ("deepseek", OpenAILLM),
        ("openrouter", OpenAILLM),
        ("groq", OpenAILLM),
        ("together", OpenAILLM),
        ("anthropic", AnthropicLLM),
        ("gemini", GeminiLLM),
    ],
)
def test_get_llm_returns_correct_class(name, cls):
    inst = get_llm(name, "http://example.com", "some-model", api_key="test")
    assert isinstance(inst, cls)
    assert inst.url == "http://example.com"
    assert inst.model == "some-model"


def test_unknown_backend_raises():
    with pytest.raises(ValueError, match="Unknown LLM type"):
        get_llm("not-a-real-backend", "http://x", "m")


def test_api_key_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("OMAKASE_API_KEY", "env-key-123")
    inst = get_llm("openai", "https://api.openai.com", "gpt-4o")
    assert inst.api_key == "env-key-123"


def test_anthropic_uses_anthropic_api_key_env(monkeypatch):
    monkeypatch.delenv("OMAKASE_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-fallback")
    inst = get_llm("anthropic", "https://api.anthropic.com", "claude-haiku-4-5")
    assert inst.api_key == "ant-fallback"
