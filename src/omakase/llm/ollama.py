"""Ollama LLM client — uses /api/generate endpoint."""

from __future__ import annotations

import httpx

from omakase.llm.base import BaseLLM


class OllamaLLM(BaseLLM):
    """Local Ollama instance. Supports JSON mode and context window config."""

    def __init__(self, url: str, model: str, api_key: str | None = None):
        # Ollama doesn't need an API key but we accept it for interface parity.
        super().__init__(url, model, api_key)

    def generate(
        self,
        prompt: str,
        temperature: float = 0.4,
        num_ctx: int = 16384,
        supports_json: bool = True,
    ) -> str:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_ctx": num_ctx,
            },
        }
        if supports_json:
            payload["format"] = "json"
        with httpx.Client(timeout=300) as client:
            resp = client.post(f"{self.url}/api/generate", json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data.get("response", "")
