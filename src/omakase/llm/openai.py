"""OpenAI-compatible LLM client.

Works with any service exposing /v1/chat/completions in OpenAI's shape:
OpenAI, DeepSeek, OpenRouter, Groq, Together, LM Studio, vLLM, etc.

API key is read from the `api_key` argument, otherwise from one of these env
vars (first match wins):
    OMAKASE_API_KEY, OPENAI_API_KEY, DEEPSEEK_API_KEY,
    OPENROUTER_API_KEY, GROQ_API_KEY, TOGETHER_API_KEY
"""

from __future__ import annotations

import os

import httpx

from omakase.llm.base import BaseLLM

_API_KEY_ENV_VARS = (
    "OMAKASE_API_KEY",
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "OPENROUTER_API_KEY",
    "GROQ_API_KEY",
    "TOGETHER_API_KEY",
)

_DEFAULT_MAX_TOKENS = 8192
_DEEPSEEK_REASONING_MAX_TOKENS = 32768


def _discover_api_key() -> str:
    for var in _API_KEY_ENV_VARS:
        v = os.environ.get(var)
        if v:
            return v
    return ""


class OpenAILLM(BaseLLM):
    """Generic OpenAI-compatible API client."""

    def __init__(self, url: str, model: str, api_key: str | None = None):
        super().__init__(url, model, api_key or _discover_api_key())

    def _max_tokens(self) -> int:
        url = self.url.rstrip("/").lower()
        model = self.model.lower()
        if url == "https://api.deepseek.com" and model in {
            "deepseek-reasoner",
            "deepseek-v4-pro",
        }:
            return _DEEPSEEK_REASONING_MAX_TOKENS
        return _DEFAULT_MAX_TOKENS

    def generate(
        self,
        prompt: str,
        temperature: float = 0.4,
        num_ctx: int = 16384,
        supports_json: bool = True,
    ) -> str:
        payload: dict = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            # 8192 is the safe ceiling for most providers this generic client
            # serves. Official DeepSeek thinking models are the exception:
            # their max_tokens includes the hidden reasoning budget, so an 8k
            # cap can leave the final answer empty after reasoning.
            "max_tokens": self._max_tokens(),
        }
        if supports_json:
            payload["response_format"] = {"type": "json_object"}

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        with httpx.Client(timeout=300) as client:
            resp = client.post(
                f"{self.url}/v1/chat/completions",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]
