"""Market data port — bars/ticks; mock + historical downloader for P1."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from trading.types import Bar, Timeframe


@runtime_checkable
class MarketDataPort(Protocol):
    def get_bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        count: int | None = None,
    ) -> list[Bar]: ...

    def is_fresh(self, symbol: str, timeframe: Timeframe, *, max_age_seconds: float) -> bool: ...
