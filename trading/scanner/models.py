"""Scanner result types — rank only; never authorize trades."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from trading.types import Timeframe, TradeCandidate


class ScoreComponents(BaseModel):
    """Persisted opportunity score breakdown (plan §4.4)."""

    trend_score: float = 0.0
    volatility_score: float = 0.0
    momentum_score: float = 0.0
    setup_quality: float = 0.0
    liquidity_score: float = 0.0
    spread_penalty: float = 0.0
    abnormal_volatility_penalty: float = 0.0
    correlation_penalty: float = 0.0

    @property
    def total(self) -> float:
        return (
            self.trend_score
            + self.volatility_score
            + self.momentum_score
            + self.setup_quality
            + self.liquidity_score
            - self.spread_penalty
            - self.abnormal_volatility_penalty
            - self.correlation_penalty
        )

    def as_dict(self) -> dict[str, float]:
        return {
            "trend_score": self.trend_score,
            "volatility_score": self.volatility_score,
            "momentum_score": self.momentum_score,
            "setup_quality": self.setup_quality,
            "liquidity_score": self.liquidity_score,
            "spread_penalty": self.spread_penalty,
            "abnormal_volatility_penalty": self.abnormal_volatility_penalty,
            "correlation_penalty": self.correlation_penalty,
            "opportunity_score": round(self.total, 6),
        }


class EligibilityResult(BaseModel):
    eligible: bool
    reason_code: str = "OK"
    details: dict[str, Any] = Field(default_factory=dict)


class ScannedMarket(BaseModel):
    """One symbol/timeframe scan row — score ranks; risk still must approve."""

    symbol: str
    timeframe: Timeframe
    eligible: bool
    reason_code: str = "OK"
    opportunity_score: float = 0.0
    components: ScoreComponents = Field(default_factory=ScoreComponents)
    rank: int | None = None
    selected_for_strategy: bool = False
    candidate: TradeCandidate | None = None
    regime: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "symbol": self.symbol,
            "timeframe": self.timeframe.value,
            "eligible": self.eligible,
            "reason_code": self.reason_code,
            "opportunity_score": round(self.opportunity_score, 6),
            "components": self.components.as_dict(),
            "rank": self.rank,
            "selected_for_strategy": self.selected_for_strategy,
            "regime": self.regime,
        }
        if self.candidate is not None:
            out["candidate"] = self.candidate.model_dump(mode="json")
        return out


class ScanReport(BaseModel):
    """Full scanner output — deterministic for fixed inputs; no orders."""

    markets: list[ScannedMarket]
    top_n: int
    selected: list[ScannedMarket] = Field(default_factory=list)
    risk_locked: bool = False
    risk_lock_reason: str = ""
    note: str = (
        "Opportunity score ranks only — never opens a trade. "
        "Candidates must still pass RiskEngine."
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "top_n": self.top_n,
            "risk_locked": self.risk_locked,
            "risk_lock_reason": self.risk_lock_reason,
            "note": self.note,
            "selected": [m.to_dict() for m in self.selected],
            "markets": [m.to_dict() for m in self.markets],
        }


EligibilityGate = Literal[
    "OK",
    "INSUFFICIENT_BARS",
    "STALE_DATA",
    "SPREAD_TOO_WIDE",
    "ABNORMAL_GAP",
    "BROKER_UNAVAILABLE",
    "API_UNHEALTHY",
    "MARKET_CLOSED",
    "RISK_LOCKED",
    "FEATURES_NOT_READY",
]
