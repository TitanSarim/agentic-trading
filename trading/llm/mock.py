"""Mock LLM for tests/CI — deterministic approve/reject without network."""

from __future__ import annotations

from trading.types import AnalystDecision, TradeCandidate


class MockLlm:
    """Offline analyst stub. Does not call Ollama."""

    name = "mock"

    def __init__(
        self,
        *,
        decision: str = "APPROVE",
        confidence: float = 0.7,
        risk_modifier: float = 0.9,
        healthy: bool = True,
        models: list[str] | None = None,
    ) -> None:
        self._decision = decision
        self._confidence = confidence
        self._risk_modifier = risk_modifier
        self._healthy = healthy
        self._models = models or ["qwen3.5:9b", "qwen3.6:27b", "qwen3.8:27b"]

    def health(self) -> bool:
        return self._healthy

    def list_models(self) -> list[str]:
        if not self._healthy:
            return []
        return list(self._models)

    def validate_candidate(
        self,
        candidate: TradeCandidate,
        *,
        market_context: dict | None = None,
    ) -> AnalystDecision:
        if not self._healthy:
            # Fail closed: reject when provider unhealthy.
            return AnalystDecision(
                decision="REJECT",
                confidence=0.0,
                risk_modifier=0.0,
                reason_code="LLM_UNAVAILABLE",
                model="mock",
                prompt_version="mock-v1",
            )
        _ = market_context
        return AnalystDecision(
            decision=self._decision,  # type: ignore[arg-type]
            confidence=self._confidence,
            risk_modifier=min(1.0, max(0.0, self._risk_modifier)),
            reason_code="MOCK_DECISION",
            model="mock",
            prompt_version="mock-v1",
            raw_response=f"mock:{candidate.symbol}:{self._decision}",
        )
