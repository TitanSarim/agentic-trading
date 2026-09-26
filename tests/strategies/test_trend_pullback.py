"""Trend pullback strategy — candidate contract + determinism."""

from __future__ import annotations

from trading.data.mock import MockMarketData
from trading.strategies.trend_pullback import TrendPullbackStrategy
from trading.types import Timeframe


def test_strategy_emits_valid_candidates() -> None:
    market = MockMarketData(scenario="trend_pullback", seed=11)
    bars = market.get_bars("EURUSD", Timeframe.M5, count=300)
    strat = TrendPullbackStrategy()
    hits = strat.evaluate_series("EURUSD", bars)
    assert hits, "expected at least one synthetic setup"
    for idx, cand in hits[:5]:
        assert cand.strategy == "trend_pullback_v1"
        assert cand.direction in ("LONG", "SHORT")
        assert cand.risk_reward == strat.config.risk_reward
        assert cand.setup_score >= strat.config.min_setup_score
        if cand.direction == "LONG":
            assert cand.stop < cand.entry < cand.target
        else:
            assert cand.target < cand.entry < cand.stop
        # Re-evaluate same index → identical (deterministic).
        again = strat.evaluate("EURUSD", bars, at_index=idx)
        assert again == cand


def test_strategy_ignores_future_bars() -> None:
    market = MockMarketData(scenario="trend_pullback", seed=3)
    bars = market.get_bars("XAUUSD", Timeframe.M15, count=200)
    strat = TrendPullbackStrategy()
    idx = 80
    a = strat.evaluate("XAUUSD", bars, at_index=idx)
    b = strat.evaluate("XAUUSD", bars[: idx + 1], at_index=idx)
    assert a == b
