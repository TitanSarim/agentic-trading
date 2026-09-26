"""Feature pipeline — no look-ahead and readiness."""

from __future__ import annotations

from trading.data.mock import MockMarketData
from trading.features.pipeline import FeatureConfig, compute_features, features_ready
from trading.types import Timeframe


def test_features_aligned_and_warmup() -> None:
    market = MockMarketData(scenario="trend_pullback")
    bars = market.get_bars("EURUSD", Timeframe.M5, count=80)
    cfg = FeatureConfig(ema_fast=8, ema_slow=21, atr_period=14, rsi_period=14)
    rows = compute_features(bars, cfg)
    assert len(rows) == len(bars)
    # Before slow EMA ready, features incomplete.
    assert not features_ready(rows[10])
    assert features_ready(rows[40])
    assert rows[40].trend in ("up", "down", "flat")


def test_no_lookahead_prefix_stability() -> None:
    """Features at index i must match computing on bars[:i+1] only."""
    market = MockMarketData(scenario="trend_pullback", seed=7)
    bars = market.get_bars("GBPUSD", Timeframe.M15, count=100)
    full = compute_features(bars)
    for i in (30, 50, 75):
        prefix = compute_features(bars[: i + 1])
        assert prefix[i].ema_fast == full[i].ema_fast
        assert prefix[i].ema_slow == full[i].ema_slow
        assert prefix[i].atr == full[i].atr
        assert prefix[i].rsi == full[i].rsi
        assert prefix[i].trend == full[i].trend
