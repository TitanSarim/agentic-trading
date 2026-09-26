"""Risk evaluation context — portfolio marks and market freshness."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from trading.types import Position


@dataclass(slots=True)
class RiskMarketContext:
    """Per-candidate market gates (data freshness + spread)."""

    now: datetime
    bar_time: datetime | None = None
    spread: float | None = None
    pip_size: float | None = None

    def __post_init__(self) -> None:
        if self.now.tzinfo is None:
            self.now = self.now.replace(tzinfo=timezone.utc)
        if self.bar_time is not None and self.bar_time.tzinfo is None:
            self.bar_time = self.bar_time.replace(tzinfo=timezone.utc)


@dataclass(slots=True)
class RiskPortfolioContext:
    """Account / exposure state for hard locks.

    Callers supply day/week equity marks and open positions. The engine does
    not invent broker state — fail closed when marks are missing for lock checks.
    """

    day_start_equity: float
    week_start_equity: float
    consecutive_losses: int = 0
    open_positions: list[Position] = field(default_factory=list)

    @property
    def open_count(self) -> int:
        return len(self.open_positions)

    @property
    def open_symbols(self) -> set[str]:
        return {p.symbol for p in self.open_positions}
