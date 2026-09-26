"""Phase 1 risk stub — enforces hard caps and fail-closed vs LLM.

Full daily/weekly/consecutive locks land in Phase 3; this stub wires the
authority boundary so Qwen cannot bypass sizing.
"""

from __future__ import annotations

from trading.config import RiskSettings
from trading.types import AccountState, AnalystDecision, RiskDecision, TradeCandidate


class RiskEngine:
    """Boss. Analyst risk_modifier may only reduce size."""

    name = "risk_v1_stub"

    def __init__(self, settings: RiskSettings | None = None) -> None:
        self.settings = settings or RiskSettings()
        if self.settings.allow_llm_increase_risk:
            raise ValueError("allow_llm_increase_risk must remain false in V1")
        self._new_trades_locked = False
        self._lock_reason = ""

    def lock_new_trades(self, reason: str) -> None:
        self._new_trades_locked = True
        self._lock_reason = reason

    def unlock_new_trades(self) -> None:
        self._new_trades_locked = False
        self._lock_reason = ""

    def account_risk_state(self, account: AccountState) -> RiskDecision:
        _ = account
        if self._new_trades_locked:
            return RiskDecision(
                approved=False,
                reason_code=self._lock_reason or "NEW_TRADES_LOCKED",
                new_trades_locked=True,
            )
        return RiskDecision(approved=True, reason_code="OK", new_trades_locked=False)

    def validate_and_size(
        self,
        candidate: TradeCandidate,
        analyst: AnalystDecision,
        account: AccountState,
    ) -> RiskDecision:
        state = self.account_risk_state(account)
        if state.new_trades_locked:
            return state

        if analyst.decision != "APPROVE":
            return RiskDecision(
                approved=False,
                reason_code="ANALYST_REJECT",
                new_trades_locked=False,
            )

        stop_distance = abs(candidate.entry - candidate.stop)
        if stop_distance <= 0:
            return RiskDecision(
                approved=False,
                reason_code="INVALID_STOP_DISTANCE",
            )

        risk_fraction = self.settings.risk_fraction_per_trade
        # LLM may only reduce — never increase above deterministic cap.
        modifier = min(1.0, max(0.0, analyst.risk_modifier))
        risk_dollars = account.equity * risk_fraction * modifier
        # Crude unit sizing for stub (Phase 3 replaces with broker contract specs).
        volume = round(risk_dollars / stop_distance, 2)
        if volume <= 0:
            return RiskDecision(
                approved=False,
                reason_code="SIZE_TOO_SMALL",
                risk_dollars=risk_dollars,
            )

        return RiskDecision(
            approved=True,
            volume=volume,
            reason_code="APPROVED",
            risk_dollars=risk_dollars,
            new_trades_locked=False,
        )
