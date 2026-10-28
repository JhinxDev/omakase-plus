"""Google Gemini client — uses generativelanguage.googleapis.com.

Endpoint: POST {url}/v1beta/models/{model}:generateContent?key=API_KEY
Reads API key from `api_key` arg, then OMAKASE_API_KEY, then GEMINI_API_KEY,
then GOOGLE_API_KEY.
"""

from __future__ import annotations

import os

import httpx

from omakase.llm.base import BaseLLM


class GeminiLLM(BaseLLM):
    """Google Gemini via the v1beta generateContent endpoint."""

    def __init__(self, url: str, model: str, api_key: str | None = None):
        key = (
            api_key
            or os.environ.get("OMAKASE_API_KEY")
            or os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY", "")
        )
        super().__init__(url, model, key)

    def generate(
        self,
        prompt: str,
        temperature: float = 0.4,
        num_ctx: int = 16384,
        supports_json: bool = True,
    ) -> str:
        payload: dict = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": 4096,
            },
        }
        if supports_json:
            payload["generationConfig"]["responseMimeType"] = "application/json"

        params = {"key": self.api_key} if self.api_key else {}
        with httpx.Client(timeout=300) as client:
            resp = client.post(
                f"{self.url}/v1beta/models/{self.model}:generateContent",
                json=payload,
                params=params,
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
            candidates = data.get("candidates", [])
            if not candidates:
                return ""
            parts = candidates[0].get("content", {}).get("parts", [])
            return "".join(p.get("text", "") for p in parts)
