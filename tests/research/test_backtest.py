"""Offline backtest — reproducible reports with costs."""

from __future__ import annotations

from research.backtest import BacktestConfig, Backtester
from research.costs import CostConfig, CostModel
from trading.data.mock import MockMarketData
from trading.strategies.trend_pullback import TrendPullbackStrategy
from trading.types import Timeframe


def test_backtest_reproducible_and_reports_metrics() -> None:
    market = MockMarketData(scenario="trend_pullback", seed=99)
    bars = market.get_bars("EURUSD", Timeframe.M5, count=400)
    engine = Backtester(
        strategy=TrendPullbackStrategy(),
        costs=CostModel(CostConfig()),
        config=BacktestConfig(volume=0.1, max_hold_bars=48),
    )
    r1 = engine.run("EURUSD", Timeframe.M5, bars)
    r2 = engine.run("EURUSD", Timeframe.M5, bars)
    assert r1.to_dict() == r2.to_dict()
    assert r1.trades >= 1
    assert r1.wins + r1.losses == r1.trades
    assert 0.0 <= r1.win_rate <= 1.0
    assert r1.max_drawdown >= 0.0
    d = r1.to_dict()
    for key in (
        "expectancy",
        "profit_factor",
        "max_drawdown",
        "net_pnl",
        "avg_win",
        "avg_loss",
    ):
        assert key in d


def test_backtest_m15_universe_smoke() -> None:
    market = MockMarketData(scenario="trend_pullback")
    engine = Backtester()
    for symbol in ("EURUSD", "GBPUSD"):
        bars = market.get_bars(symbol, Timeframe.M15, count=250)
        report = engine.run(symbol, "M15", bars)
        assert report.timeframe == "M15"
        assert report.strategy == "trend_pullback_v1"
