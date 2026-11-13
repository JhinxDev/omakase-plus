"""LLM backend registry.

Each backend is a class that subclasses BaseLLM. New backends can be added
via the @register decorator and will appear in `omakase backends`.
"""

from __future__ import annotations

from omakase.llm.anthropic import AnthropicLLM
from omakase.llm.base import BaseLLM
from omakase.llm.codex import CodexLLM
from omakase.llm.gemini import GeminiLLM
from omakase.llm.ollama import OllamaLLM
from omakase.llm.openai import OpenAILLM

_REGISTRY: dict[str, type[BaseLLM]] = {}


def register(name: str):
    def wrapper(cls: type[BaseLLM]) -> type[BaseLLM]:
        _REGISTRY[name] = cls
        return cls

    return wrapper


def get_llm(llm_type: str, url: str, model: str, api_key: str | None = None) -> BaseLLM:
    if llm_type not in _REGISTRY:
        available = ", ".join(sorted(_REGISTRY))
        raise ValueError(f"Unknown LLM type '{llm_type}'. Available: {available}")
    cls = _REGISTRY[llm_type]
    try:
        return cls(url=url, model=model, api_key=api_key)
    except TypeError:
        # Backends whose __init__ doesn't accept api_key (e.g. Ollama)
        return cls(url=url, model=model)


def list_backends() -> list[str]:
    return sorted(_REGISTRY)


# Local / self-hosted
register("ollama")(OllamaLLM)

# OpenAI-compatible: all share the /v1/chat/completions shape
register("openai")(OpenAILLM)
register("lmstudio")(OpenAILLM)
register("deepseek")(OpenAILLM)
register("openrouter")(OpenAILLM)
register("groq")(OpenAILLM)
register("together")(OpenAILLM)

# Native protocols
register("anthropic")(AnthropicLLM)
register("gemini")(GeminiLLM)
register("codex")(CodexLLM)
