"""Risk engine port — highest authority; LLM cannot raise caps."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from trading.types import AccountState, AnalystDecision, RiskDecision, TradeCandidate


@runtime_checkable
class RiskPort(Protocol):
    def account_risk_state(self, account: AccountState) -> RiskDecision:
        """Return whether new trades are locked (drawdown / consecutive loss)."""
        ...

    def validate_and_size(
        self,
        candidate: TradeCandidate,
        analyst: AnalystDecision,
        account: AccountState,
    ) -> RiskDecision: ...
