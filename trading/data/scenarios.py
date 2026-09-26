"""Deterministic synthetic bar scenarios for offline research / backtests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from trading.types import Bar, Timeframe


def synthesize_trend_pullback_bars(
    symbol: str,
    timeframe: Timeframe,
    *,
    count: int = 400,
    base_price: float | None = None,
    seed: int = 42,
) -> list[Bar]:
    """Uptrend with periodic pullbacks — enough structure for trend_pullback_v1.

    Purely synthetic; not market data. Deterministic for a given seed/count.
    """
    bases = {
        "EURUSD": 1.1000,
        "GBPUSD": 1.2700,
        "USDJPY": 150.00,
        "XAUUSD": 2400.0,
    }
    px = base_price if base_price is not None else bases.get(symbol, 1.0)
    # Scale noise/steps to instrument magnitude.
    if symbol == "XAUUSD":
        step_up, pull_depth, noise = 0.35, 1.2, 0.15
        spread = 0.25
    elif symbol == "USDJPY":
        step_up, pull_depth, noise = 0.04, 0.12, 0.02
        spread = 0.015
    else:
        step_up, pull_depth, noise = 0.00035, 0.0011, 0.00012
        spread = 0.00012

    minutes = 5 if timeframe == Timeframe.M5 else 15
    step = timedelta(minutes=minutes)
    now = datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)
    start = now - step * count

    bars: list[Bar] = []
    price = px
    # Simple LCG for deterministic "noise"
    state = seed & 0xFFFFFFFF

    def rnd() -> float:
        nonlocal state
        state = (1664525 * state + 1013904223) & 0xFFFFFFFF
        return (state / 0xFFFFFFFF) * 2.0 - 1.0

    for i in range(count):
        # Cycle: 12 bars up, 4 bar pullback, with occasional deeper shakeouts.
        phase = i % 16
        cycle = i // 16
        if phase < 12:
            drift = step_up * (0.7 + 0.3 * abs(rnd()))
            # Every 3rd cycle: choppy counter-moves that can stop out longs.
            if cycle % 3 == 2 and phase in (3, 7):
                drift = -pull_depth * (0.9 + 0.5 * abs(rnd()))
        else:
            depth_mult = 1.8 if cycle % 4 == 0 else 1.0
            drift = -pull_depth / 4.0 * depth_mult * (0.8 + 0.4 * abs(rnd()))

        open_ = price
        close = price + drift + noise * rnd() * 0.35
        high = max(open_, close) + abs(noise * rnd())
        low = min(open_, close) - abs(noise * rnd()) * (1.5 if cycle % 3 == 2 else 1.0)
        # Keep OHLC consistent.
        high = max(high, open_, close)
        low = min(low, open_, close)
        t = start + step * i
        bars.append(
            Bar(
                symbol=symbol,
                timeframe=timeframe,
                time=t,
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=100.0 + (i % 50),
                spread=spread,
            )
        )
        price = close

    return bars
