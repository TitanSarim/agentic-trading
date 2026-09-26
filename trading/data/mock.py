"""Synthetic / fixture market data for tests and cloud CI."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from trading.types import Bar, Timeframe, utc_now


class MockMarketData:
    """Deterministic OHLCV generator — no broker network required."""

    def __init__(
        self,
        *,
        symbols: list[str] | None = None,
        base_prices: dict[str, float] | None = None,
    ) -> None:
        self.symbols = symbols or ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD"]
        self.base_prices = base_prices or {
            "EURUSD": 1.1000,
            "GBPUSD": 1.2700,
            "USDJPY": 150.00,
            "XAUUSD": 2400.0,
        }
        self._bars: dict[tuple[str, Timeframe], list[Bar]] = {}

    def seed(
        self,
        symbol: str,
        timeframe: Timeframe,
        bars: list[Bar],
    ) -> None:
        self._bars[(symbol, timeframe)] = list(bars)

    def get_bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        count: int | None = None,
    ) -> list[Bar]:
        key = (symbol, timeframe)
        need = count or 64
        if key not in self._bars or len(self._bars[key]) < need:
            self._bars[key] = self._synthesize(symbol, timeframe, need)
        bars = self._bars[key]
        if start is not None:
            bars = [b for b in bars if b.time >= start]
        if end is not None:
            bars = [b for b in bars if b.time <= end]
        if count is not None:
            bars = bars[-count:]
        return list(bars)

    def is_fresh(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        max_age_seconds: float,
    ) -> bool:
        bars = self.get_bars(symbol, timeframe, count=1)
        if not bars:
            return False
        age = (utc_now() - bars[-1].time).total_seconds()
        return age <= max_age_seconds

    def _synthesize(self, symbol: str, timeframe: Timeframe, count: int) -> list[Bar]:
        step = timedelta(minutes=5 if timeframe == Timeframe.M5 else 15)
        px = self.base_prices.get(symbol, 1.0)
        now = utc_now().replace(second=0, microsecond=0)
        start = now - step * count
        bars: list[Bar] = []
        for i in range(count):
            t = start + step * i
            # Tiny deterministic drift for mocks.
            open_ = px + (i * 0.0001)
            close = open_ + 0.00005
            high = max(open_, close) + 0.0001
            low = min(open_, close) - 0.0001
            bars.append(
                Bar(
                    symbol=symbol,
                    timeframe=timeframe,
                    time=t if t.tzinfo else t.replace(tzinfo=timezone.utc),
                    open=open_,
                    high=high,
                    low=low,
                    close=close,
                    volume=100.0 + i,
                    spread=0.00012 if symbol != "XAUUSD" else 0.25,
                )
            )
        return bars
