"""Tests for the OpenAI-compatible LLM client payloads."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from omakase.llm.openai import OpenAILLM


def _mock_client():
    response = MagicMock()
    response.json.return_value = {"choices": [{"message": {"content": '{"ok": true}'}}]}

    client = MagicMock()
    client.post.return_value = response

    manager = MagicMock()
    manager.__enter__.return_value = client
    manager.__exit__.return_value = None
    return manager, client


@pytest.mark.parametrize("model", ["deepseek-reasoner", "deepseek-v4-pro"])
def test_official_deepseek_thinking_models_use_reasoning_sized_output_budget(model):
    manager, client = _mock_client()

    with patch("httpx.Client", return_value=manager):
        llm = OpenAILLM("https://api.deepseek.com", model, api_key="test")
        llm.generate("Return JSON.", supports_json=False)

    payload = client.post.call_args.kwargs["json"]
    assert payload["max_tokens"] == 32768


def test_deepseek_chat_keeps_conservative_output_budget():
    manager, client = _mock_client()

    with patch("httpx.Client", return_value=manager):
        llm = OpenAILLM("https://api.deepseek.com", "deepseek-chat", api_key="test")
        llm.generate("Return JSON.", supports_json=False)

    payload = client.post.call_args.kwargs["json"]
    assert payload["max_tokens"] == 8192
