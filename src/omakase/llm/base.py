"""Abstract LLM client."""

from abc import ABC, abstractmethod


class BaseLLM(ABC):
    """Talk to an LLM and get JSON back."""

    def __init__(self, url: str, model: str, api_key: str | None = None):
        self.url = url.rstrip("/")
        self.model = model
        self.api_key = api_key

    @abstractmethod
    def generate(
        self,
        prompt: str,
        temperature: float = 0.4,
        num_ctx: int = 16384,
        supports_json: bool = True,
    ) -> str:
        """Send a prompt and return the raw response text (ideally JSON)."""
        ...
