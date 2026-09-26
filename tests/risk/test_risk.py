"""Phase 3 risk engine — every hard gate has a reject path."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trading.config import RiskSettings
from trading.risk.context import RiskMarketContext, RiskPortfolioContext
from trading.risk.engine import RiskEngine
from trading.types import AccountState, AnalystDecision, Position, Side, TradeCandidate


def _account(equity: float = 1000.0) -> AccountState:
    return AccountState(balance=equity, equity=equity, account_mode="demo")


def _candidate(
    *,
    symbol: str = "EURUSD",
    entry: float = 1.10,
    stop: float = 1.09,
    direction: str = "LONG",
) -> TradeCandidate:
    return TradeCandidate(
        symbol=symbol,
        strategy="trend_pullback_v1",
        direction=direction,  # type: ignore[arg-type]
        entry=entry,
        stop=stop,
        target=entry + 2 * abs(entry - stop) if direction == "LONG" else entry - 2 * abs(entry - stop),
        risk_reward=2.0,
        regime="trend",
        setup_score=80,
    )


def _approve(modifier: float = 1.0, confidence: float = 0.9) -> AnalystDecision:
    return AnalystDecision(
        decision="APPROVE",
        confidence=confidence,
        risk_modifier=modifier,
        reason_code="OK",
    )


def _reject() -> AnalystDecision:
    return AnalystDecision(decision="REJECT", confidence=0.9, risk_modifier=1.0)


def _now() -> datetime:
    return datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


def _fresh_market(spread: float = 0.00012) -> RiskMarketContext:
    t = _now()
    return RiskMarketContext(now=t, bar_time=t, spread=spread, pip_size=0.0001)


def _portfolio(
    *,
    day: float = 1000.0,
    week: float = 1000.0,
    consecutive: int = 0,
    positions: list[Position] | None = None,
) -> RiskPortfolioContext:
    return RiskPortfolioContext(
        day_start_equity=day,
        week_start_equity=week,
        consecutive_losses=consecutive,
        open_positions=positions or [],
    )


def _open(symbol: str = "EURUSD") -> Position:
    return Position(
        ticket="1",
        symbol=symbol,
        side=Side.BUY,
        volume=0.1,
        entry_price=1.1,
        stop_loss=1.09,
    )


# --- Authority / sizing ----------------------------------------------------


def test_risk_rejects_when_analyst_rejects() -> None:
    risk = RiskEngine()
    decision = risk.validate_and_size(
        _candidate(), _reject(), _account(), market=_fresh_market(), portfolio=_portfolio()
    )
    assert decision.approved is False
    assert decision.reason_code == "ANALYST_REJECT"


def test_risk_modifier_only_reduces_size() -> None:
    risk = RiskEngine(
        RiskSettings(
            risk_fraction_per_trade=0.01,
            min_volume=0.01,
            volume_step=0.01,
            max_spread_pips=None,
        )
    )
    # 10-pip stop → full volume 0.10, half 0.05
    cand = _candidate(entry=1.1000, stop=1.0990)
    full = risk.validate_and_size(
        cand, _approve(1.0), _account(), market=_fresh_market(), portfolio=_portfolio()
    )
    half = risk.validate_and_size(
        cand, _approve(0.5), _account(), market=_fresh_market(), portfolio=_portfolio()
    )
    assert full.approved and half.approved
    assert half.volume < full.volume
    assert half.risk_dollars == pytest.approx(full.risk_dollars * 0.5)


def test_modifier_above_one_clamped() -> None:
    risk = RiskEngine(
        RiskSettings(
            risk_fraction_per_trade=0.01,
            min_volume=0.01,
            volume_step=0.01,
            max_spread_pips=None,
        )
    )
    cand = _candidate(entry=1.1000, stop=1.0990)
    # Bypass pydantic clamp via model_construct for the engine path.
    over = AnalystDecision.model_construct(
        decision="APPROVE", confidence=1.0, risk_modifier=2.0, reason_code="X"
    )
    a = risk.validate_and_size(
        cand, _approve(1.0), _account(), market=_fresh_market(), portfolio=_portfolio()
    )
    b = risk.validate_and_size(
        cand, over, _account(), market=_fresh_market(), portfolio=_portfolio()
    )
    assert a.approved and b.approved
    assert b.volume == a.volume


def test_disallow_llm_increase_flag() -> None:
    with pytest.raises(ValueError):
        RiskEngine(RiskSettings(allow_llm_increase_risk=True))


def test_sizing_from_equity_and_stop() -> None:
    # equity=1000, risk 1%, stop 0.01, point_value 100000
    # risk_dollars=10; volume = 10 / (0.01 * 100000) = 0.01
    risk = RiskEngine(
        RiskSettings(
            risk_fraction_per_trade=0.01,
            min_volume=0.01,
            volume_step=0.01,
            max_spread_pips=None,
        )
    )
    decision = risk.validate_and_size(
        _candidate(entry=1.10, stop=1.09),
        _approve(1.0),
        _account(1000),
        market=_fresh_market(),
        portfolio=_portfolio(),
    )
    assert decision.approved
    assert decision.volume == pytest.approx(0.01)
    assert decision.risk_dollars == pytest.approx(10.0)


def test_invalid_stop_distance() -> None:
    risk = RiskEngine(RiskSettings(max_spread_pips=None))
    bad = _candidate(entry=1.10, stop=1.10)
    decision = risk.validate_and_size(
        bad, _approve(), _account(), market=_fresh_market(), portfolio=_portfolio()
    )
    assert decision.approved is False
    assert decision.reason_code == "INVALID_STOP_DISTANCE"


def test_size_too_small() -> None:
    # Tiny equity / tight risk → below min_volume after quantize.
    risk = RiskEngine(
        RiskSettings(
            risk_fraction_per_trade=0.0001,
            min_volume=0.01,
            volume_step=0.01,
            max_spread_pips=None,
        )
    )
    decision = risk.validate_and_size(
        _candidate(),
        _approve(),
        _account(100),
        market=_fresh_market(),
        portfolio=_portfolio(day=100, week=100),
    )
    assert decision.approved is False
    assert decision.reason_code == "SIZE_TOO_SMALL"


# --- Locks / kill / halt ---------------------------------------------------


def test_manual_lock_blocks_new_trades() -> None:
    risk = RiskEngine()
    risk.lock_new_trades("MANUAL")
    decision = risk.validate_and_size(
        _candidate(), _approve(), _account(), market=_fresh_market(), portfolio=_portfolio()
    )
    assert decision.approved is False
    assert decision.new_trades_locked is True
    assert decision.reason_code == "MANUAL"


def test_kill_switch() -> None:
    risk = RiskEngine(
        RiskSettings(
            risk_fraction_per_trade=0.01,
            min_volume=0.01,
            volume_step=0.01,
            max_spread_pips=None,
        )
    )
    cand = _candidate(entry=1.1000, stop=1.0990)
    risk.engage_kill_switch("OPS_KILL")
    decision = risk.validate_and_size(
        cand, _approve(), _account(), market=_fresh_market(), portfolio=_portfolio()
    )
    assert decision.approved is False
    assert decision.reason_code == "OPS_KILL"
    assert risk.kill_switch_active
    risk.clear_kill_switch()
    assert risk.kill_switch_active is False
    ok = risk.validate_and_size(
        cand, _approve(), _account(), market=_fresh_market(), portfolio=_portfolio()
    )
    assert ok.approved


def test_halt_hook() -> None:
    risk = RiskEngine()
    risk.halt("BROKER_DISCONNECT")
    decision = risk.account_risk_state(_account(), _portfolio())
    assert decision.approved is False
    assert decision.reason_code == "BROKER_DISCONNECT"
    risk.clear_halt()
    assert risk.account_risk_state(_account(), _portfolio()).approved


def test_daily_loss_lock() -> None:
    risk = RiskEngine(RiskSettings(daily_loss_lock_pct=0.015, max_spread_pips=None))
    # Day started 1000; equity 980 → 2% loss > 1.5%
    decision = risk.validate_and_size(
        _candidate(),
        _approve(),
        _account(980),
        market=_fresh_market(),
        portfolio=_portfolio(day=1000, week=1000),
    )
    assert decision.approved is False
    assert decision.reason_code == "DAILY_LOSS_LOCK"
    assert decision.new_trades_locked


def test_weekly_drawdown_lock() -> None:
    risk = RiskEngine(RiskSettings(weekly_drawdown_lock_pct=0.045, max_spread_pips=None))
    # Week started 1000; equity 950 → 5% DD > 4.5%
    decision = risk.validate_and_size(
        _candidate(),
        _approve(),
        _account(950),
        market=_fresh_market(),
        portfolio=_portfolio(day=950, week=1000),
    )
    assert decision.approved is False
    assert decision.reason_code == "WEEKLY_DRAWDOWN_LOCK"


def test_consecutive_loss_lock() -> None:
    risk = RiskEngine(RiskSettings(consecutive_loss_lock=3, max_spread_pips=None))
    decision = risk.validate_and_size(
        _candidate(),
        _approve(),
        _account(),
        market=_fresh_market(),
        portfolio=_portfolio(consecutive=3),
    )
    assert decision.approved is False
    assert decision.reason_code == "CONSECUTIVE_LOSS_LOCK"


def test_record_closed_trade_streak() -> None:
    risk = RiskEngine(RiskSettings(consecutive_loss_lock=3, max_spread_pips=None))
    risk.record_closed_trade(-10)
    risk.record_closed_trade(-5)
    risk.record_closed_trade(-1)
    decision = risk.validate_and_size(
        _candidate(),
        _approve(),
        _account(),
        market=_fresh_market(),
        portfolio=_portfolio(consecutive=0),  # engine streak alone
    )
    assert decision.reason_code == "CONSECUTIVE_LOSS_LOCK"
    risk.record_closed_trade(5)
    assert risk.consecutive_losses == 0


# --- Exposure / correlation ------------------------------------------------


def test_max_positions() -> None:
    risk = RiskEngine(RiskSettings(max_positions=1, max_spread_pips=None))
    decision = risk.validate_and_size(
        _candidate(symbol="USDJPY"),
        _approve(),
        _account(),
        market=_fresh_market(),
        portfolio=_portfolio(positions=[_open("EURUSD")]),
    )
    assert decision.approved is False
    assert decision.reason_code == "MAX_POSITIONS"


def test_averaging_down_forbidden() -> None:
    risk = RiskEngine(
        RiskSettings(max_positions=2, forbid_averaging_down=True, max_spread_pips=None)
    )
    decision = risk.validate_and_size(
        _candidate(symbol="EURUSD"),
        _approve(),
        _account(),
        market=_fresh_market(),
        portfolio=_portfolio(positions=[_open("EURUSD")]),
    )
    assert decision.approved is False
    assert decision.reason_code == "AVERAGING_DOWN_FORBIDDEN"


def test_correlation_exposure() -> None:
    risk = RiskEngine(
        RiskSettings(
            max_positions=2,
            correlation_groups=[["EURUSD", "GBPUSD"]],
            max_spread_pips=None,
        )
    )
    decision = risk.validate_and_size(
        _candidate(symbol="GBPUSD", entry=1.30, stop=1.29),
        _approve(),
        _account(),
        market=_fresh_market(),
        portfolio=_portfolio(positions=[_open("EURUSD")]),
    )
    assert decision.approved is False
    assert decision.reason_code == "CORRELATION_EXPOSURE"


# --- Data freshness / spread ----------------------------------------------


def test_stale_market_data() -> None:
    risk = RiskEngine(RiskSettings(max_data_age_seconds=60, max_spread_pips=None))
    now = _now()
    stale = RiskMarketContext(
        now=now,
        bar_time=now - timedelta(seconds=120),
        spread=0.0001,
    )
    decision = risk.validate_and_size(
        _candidate(), _approve(), _account(), market=stale, portfolio=_portfolio()
    )
    assert decision.approved is False
    assert decision.reason_code == "STALE_MARKET_DATA"


def test_missing_bar_time_fail_closed() -> None:
    risk = RiskEngine(RiskSettings(max_data_age_seconds=60, max_spread_pips=None))
    market = RiskMarketContext(now=_now(), bar_time=None, spread=0.0001)
    decision = risk.validate_and_size(
        _candidate(), _approve(), _account(), market=market, portfolio=_portfolio()
    )
    assert decision.reason_code == "STALE_MARKET_DATA"


def test_spread_too_wide() -> None:
    risk = RiskEngine(RiskSettings(max_spread_pips=2.0, max_data_age_seconds=300))
    # 0.0005 / 0.0001 = 5 pips > 2
    market = RiskMarketContext(
        now=_now(), bar_time=_now(), spread=0.0005, pip_size=0.0001
    )
    decision = risk.validate_and_size(
        _candidate(), _approve(), _account(), market=market, portfolio=_portfolio()
    )
    assert decision.approved is False
    assert decision.reason_code == "SPREAD_TOO_WIDE"


def test_spread_unknown_fail_closed() -> None:
    risk = RiskEngine(RiskSettings(max_spread_pips=2.0, max_data_age_seconds=300))
    market = RiskMarketContext(now=_now(), bar_time=_now(), spread=None)
    decision = risk.validate_and_size(
        _candidate(), _approve(), _account(), market=market, portfolio=_portfolio()
    )
    assert decision.reason_code == "SPREAD_UNKNOWN"


def test_fresh_approved_path() -> None:
    risk = RiskEngine(
        RiskSettings(
            risk_fraction_per_trade=0.01,
            min_volume=0.01,
            volume_step=0.01,
            max_spread_pips=3.0,
            max_data_age_seconds=120,
        )
    )
    decision = risk.validate_and_size(
        _candidate(entry=1.1000, stop=1.0990),
        _approve(),
        _account(),
        market=_fresh_market(spread=0.00012),
        portfolio=_portfolio(),
    )
    assert decision.approved
    assert decision.reason_code == "APPROVED"
    assert decision.volume > 0
