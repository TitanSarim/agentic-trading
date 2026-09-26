"""Optional candidate → Qwen analyze helper for scanner/strategy path.

RiskEngine remains the boss. This module never places orders.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trading.llm.base import LlmPort
from trading.types import AnalystDecision, TradeCandidate


@dataclass(frozen=True, slots=True)
class AnalyzedCandidate:
    """Candidate plus analyst decision (advisory)."""

    candidate: TradeCandidate
    analyst: AnalystDecision
    market_context: dict[str, Any] | None = None

    @property
    def approved_by_analyst(self) -> bool:
        return self.analyst.decision == "APPROVE"

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate.model_dump(mode="json"),
            "analyst": self.analyst.model_dump(mode="json"),
            "market_context": self.market_context or {},
            "approved_by_analyst": self.approved_by_analyst,
        }


def analyze_candidates(
    llm: LlmPort,
    candidates: list[TradeCandidate],
    *,
    market_context: dict[str, Any] | None = None,
    contexts: list[dict[str, Any] | None] | None = None,
) -> list[AnalyzedCandidate]:
    """Run analyst on each candidate. Fail-closed behavior lives in the LLM port.

    Does not call RiskEngine or broker — optional wiring only.
    """
    results: list[AnalyzedCandidate] = []
    for i, candidate in enumerate(candidates):
        ctx = None
        if contexts is not None and i < len(contexts):
            ctx = contexts[i]
        elif market_context is not None:
            ctx = market_context
        decision = llm.validate_candidate(candidate, market_context=ctx)
        results.append(
            AnalyzedCandidate(
                candidate=candidate,
                analyst=decision,
                market_context=ctx,
            )
        )
    return results
