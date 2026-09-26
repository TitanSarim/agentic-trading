"""Mock LLM for tests/CI — deterministic approve/reject without network."""

from __future__ import annotations

import json

from trading.llm.ollama import candidate_input_hash
from trading.llm.schema import PROMPT_VERSION, reject_decision
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
        reason_code: str = "MOCK_DECISION",
        simulate_timeout: bool = False,
    ) -> None:
        self._decision = decision
        self._confidence = confidence
        self._risk_modifier = risk_modifier
        self._healthy = healthy
        self._models = models or ["qwen3.5:9b", "qwen3.6:27b", "qwen3.8:27b"]
        self._reason_code = reason_code
        self._simulate_timeout = simulate_timeout
        self.calls = 0

    def health(self) -> bool:
        return self._healthy and not self._simulate_timeout

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
        self.calls += 1
        input_hash = candidate_input_hash(
            candidate,
            market_context=market_context,
            prompt_version=PROMPT_VERSION,
        )
        if self._simulate_timeout:
            return reject_decision(
                "LLM_TIMEOUT",
                model="mock",
                prompt_version=PROMPT_VERSION,
                input_hash=input_hash,
            )
        if not self._healthy:
            return reject_decision(
                "LLM_UNAVAILABLE",
                model="mock",
                prompt_version=PROMPT_VERSION,
                input_hash=input_hash,
            )
        _ = market_context
        risk_modifier = min(1.0, max(0.0, self._risk_modifier))
        confidence = min(1.0, max(0.0, self._confidence))
        decision = str(self._decision).upper()
        if decision not in {"APPROVE", "REJECT"}:
            decision = "REJECT"
        validated = {
            "decision": decision,
            "confidence": confidence,
            "risk_modifier": risk_modifier,
            "reason_code": self._reason_code,
        }
        return AnalystDecision(
            decision=decision,  # type: ignore[arg-type]
            confidence=confidence,
            risk_modifier=risk_modifier,
            reason_code=self._reason_code,
            model="mock",
            prompt_version=PROMPT_VERSION,
            input_hash=input_hash,
            raw_response=json.dumps(validated),
            validated_response=validated,
        )
