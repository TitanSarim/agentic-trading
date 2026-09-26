"""Risk stub authority tests."""

from __future__ import annotations

import pytest

from trading.config import RiskSettings
from trading.risk.engine import RiskEngine
from trading.types import AccountState, AnalystDecision, TradeCandidate


def _account() -> AccountState:
    return AccountState(balance=1000, equity=1000, account_mode="demo")


def _candidate() -> TradeCandidate:
    return TradeCandidate(
        symbol="EURUSD",
        strategy="stub",
        direction="LONG",
        entry=1.10,
        stop=1.09,
        target=1.12,
        risk_reward=2.0,
    )


def test_risk_rejects_when_analyst_rejects() -> None:
    risk = RiskEngine()
    analyst = AnalystDecision(decision="REJECT", confidence=0.9, risk_modifier=1.0)
    decision = risk.validate_and_size(_candidate(), analyst, _account())
    assert decision.approved is False
    assert decision.reason_code == "ANALYST_REJECT"


def test_risk_modifier_only_reduces_size() -> None:
    risk = RiskEngine(RiskSettings(risk_fraction_per_trade=0.01))
    full = AnalystDecision(decision="APPROVE", confidence=0.8, risk_modifier=1.0)
    half = AnalystDecision(decision="APPROVE", confidence=0.8, risk_modifier=0.5)
    a = risk.validate_and_size(_candidate(), full, _account())
    b = risk.validate_and_size(_candidate(), half, _account())
    assert a.approved and b.approved
    assert b.volume < a.volume
    assert b.risk_dollars == pytest.approx(a.risk_dollars * 0.5)


def test_lock_blocks_new_trades() -> None:
    risk = RiskEngine()
    risk.lock_new_trades("DAILY_LOSS")
    analyst = AnalystDecision(decision="APPROVE", confidence=1.0, risk_modifier=1.0)
    decision = risk.validate_and_size(_candidate(), analyst, _account())
    assert decision.approved is False
    assert decision.new_trades_locked is True


def test_disallow_llm_increase_flag() -> None:
    with pytest.raises(ValueError):
        RiskEngine(RiskSettings(allow_llm_increase_risk=True))
