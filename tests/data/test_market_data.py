"""Market data mock + historical download."""

from __future__ import annotations

from pathlib import Path

from trading.data.historical import HistoricalDownloader
from trading.data.mock import MockMarketData
from trading.journal.db import JournalDB
from trading.types import Timeframe


def test_mock_bars_and_download(tmp_path: Path) -> None:
    market = MockMarketData()
    bars = market.get_bars("EURUSD", Timeframe.M5, count=10)
    assert len(bars) == 10
    assert bars[0].timeframe == Timeframe.M5
    assert market.is_fresh("EURUSD", Timeframe.M5, max_age_seconds=3600)

    db = JournalDB(tmp_path / "t.db")
    db.migrate()
    downloader = HistoricalDownloader(market, db)
    written = downloader.download(
        ["EURUSD", "XAUUSD"],
        ["M5", "M15"],
        count=20,
    )
    assert written == 80
    assert db.count_bars() == 80
    events = db.recent_system_events(limit=5)
    assert any(e["event_type"] == "historical_download_complete" for e in events)
    db.close()
