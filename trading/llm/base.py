"""LLM / analyst port — Qwen is advisory only; risk remains boss."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from trading.types import AnalystDecision, TradeCandidate


@runtime_checkable
class LlmPort(Protocol):
    """Analyst interface. Never sizes or executes trades."""

    def health(self) -> bool:
        """True if provider is reachable; False must fail-closed upstream."""
        ...

    def list_models(self) -> list[str]: ...

    def validate_candidate(
        self,
        candidate: TradeCandidate,
        *,
        market_context: dict | None = None,
    ) -> AnalystDecision: ...
