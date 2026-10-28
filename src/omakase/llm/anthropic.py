"""Anthropic native client — uses /v1/messages.

Reads API key from `api_key` arg, then OMAKASE_API_KEY, then ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import os

import httpx

from omakase.llm.base import BaseLLM

ANTHROPIC_VERSION = "2023-06-01"


class AnthropicLLM(BaseLLM):
    """Anthropic Claude via /v1/messages."""

    def __init__(self, url: str, model: str, api_key: str | None = None):
        key = (
            api_key or os.environ.get("OMAKASE_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")
        )
        super().__init__(url, model, key)

    def generate(
        self,
        prompt: str,
        temperature: float = 0.4,
        num_ctx: int = 16384,
        supports_json: bool = True,
    ) -> str:
        # Anthropic doesn't have a `response_format` field, but a strong system
        # nudge plus the prompt's own format instructions yield valid JSON.
        payload = {
            "model": self.model,
            "max_tokens": 4096,
            "temperature": temperature,
            "system": (
                "You are a careful recommender. Respond with a single JSON "
                "object only — no prose, no markdown, no code fences."
            ),
            "messages": [{"role": "user", "content": prompt}],
        }
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": ANTHROPIC_VERSION,
        }
        if self.api_key:
            headers["x-api-key"] = self.api_key

        with httpx.Client(timeout=300) as client:
            resp = client.post(f"{self.url}/v1/messages", json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            blocks = data.get("content", [])
            # content is a list of typed blocks; concatenate the text ones
            return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
