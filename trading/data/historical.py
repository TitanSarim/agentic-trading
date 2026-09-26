"""Historical bar download path for the V1 universe (four symbols, M5/M15).

P1: pulls from a MarketDataPort (typically mock or later MT5) and persists
to SQLite. Cloud CI uses mock — no live broker required.
"""

from __future__ import annotations

from datetime import datetime

import structlog

from trading.data.base import MarketDataPort
from trading.journal.db import JournalDB
from trading.types import Timeframe

log = structlog.get_logger(__name__)


class HistoricalDownloader:
    def __init__(self, market_data: MarketDataPort, journal: JournalDB) -> None:
        self.market_data = market_data
        self.journal = journal

    def download(
        self,
        symbols: list[str],
        timeframes: list[str],
        *,
        count: int = 500,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> int:
        """Download bars and upsert into market_bars. Returns bars written."""
        written = 0
        for symbol in symbols:
            for tf_name in timeframes:
                tf = Timeframe(tf_name)
                bars = self.market_data.get_bars(
                    symbol, tf, start=start, end=end, count=count
                )
                n = self.journal.upsert_bars(bars)
                written += n
                log.info(
                    "historical_download",
                    symbol=symbol,
                    timeframe=tf_name,
                    bars=n,
                )
        self.journal.record_system_event(
            "historical_download_complete",
            {
                "symbols": symbols,
                "timeframes": timeframes,
                "bars_written": written,
            },
        )
        return written
