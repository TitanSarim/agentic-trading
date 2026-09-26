"""LLM factory."""

from __future__ import annotations

from trading.config import Settings
from trading.llm.base import LlmPort
from trading.llm.mock import MockLlm
from trading.llm.ollama import OllamaClient


def create_llm(settings: Settings, *, force_mock: bool = False) -> LlmPort:
    if force_mock:
        return MockLlm()
    return OllamaClient(
        base_url=settings.ollama.base_url,
        analyst_model=settings.ollama.analyst_model,
        screen_model=settings.ollama.screen_model,
        temperature=settings.ollama.temperature,
        timeout_seconds=settings.ollama.timeout_seconds,
        fail_closed_on_error=settings.ollama.fail_closed_on_error,
    )
