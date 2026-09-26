"""LLM factory."""

from __future__ import annotations

from trading.config import Settings
from trading.llm.base import LlmPort
from trading.llm.mock import MockLlm
from trading.llm.ollama import OllamaClient


def create_llm(
    settings: Settings,
    *,
    force_mock: bool = False,
    backend: str | None = None,
) -> LlmPort:
    """Build analyst client.

    ``force_mock=True`` or ``backend='mock'`` → offline MockLlm (CI-safe).
    Default backend follows ``settings.ollama.backend`` (``ollama`` | ``mock``).
    """
    chosen = (backend or settings.ollama.backend).lower()
    if force_mock or chosen == "mock":
        return MockLlm()
    return OllamaClient(
        base_url=settings.ollama.base_url,
        analyst_model=settings.ollama.analyst_model,
        screen_model=settings.ollama.screen_model,
        temperature=settings.ollama.temperature,
        timeout_seconds=settings.ollama.timeout_seconds,
        fail_closed_on_error=settings.ollama.fail_closed_on_error,
        use_screen_model=settings.ollama.use_screen_model,
    )
