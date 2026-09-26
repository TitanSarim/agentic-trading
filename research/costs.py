"""Spread / commission / slippage / swap cost model for research backtests."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from trading.types import Side


# Typical IC Markets-style research defaults (not broker guarantees).
DEFAULT_PIP_SIZE: dict[str, float] = {
    "EURUSD": 0.0001,
    "GBPUSD": 0.0001,
    "USDJPY": 0.01,
    "XAUUSD": 0.01,
}

DEFAULT_SPREAD: dict[str, float] = {
    "EURUSD": 0.00012,
    "GBPUSD": 0.00015,
    "USDJPY": 0.015,
    "XAUUSD": 0.25,
}

# Approximate USD value of 1.0 price unit per 1.0 lot (research stub).
# Phase 3 replaces with broker contract specs.
DEFAULT_POINT_VALUE_PER_LOT: dict[str, float] = {
    "EURUSD": 100_000.0,  # 1.0 price move ≈ $100k / lot
    "GBPUSD": 100_000.0,
    "USDJPY": 100_000.0 / 150.0,  # rough at ~150 JPY
    "XAUUSD": 100.0,  # $1 move per oz × 100 oz lot (approx)
}


@dataclass(frozen=True, slots=True)
class CostConfig:
    """Round-trip research costs. Spread may also come from bar.spread."""

    commission_per_lot: float = 7.0  # USD round-turn per 1.0 lot
    slippage_pips: float = 0.5
    swap_per_lot_per_day: float = 0.0  # USD; 0 disables overnight accrual
    bars_per_day: dict[str, int] = field(
        default_factory=lambda: {"M5": 288, "M15": 96}
    )
    pip_size: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_PIP_SIZE))
    default_spread: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_SPREAD)
    )
    point_value_per_lot: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_POINT_VALUE_PER_LOT)
    )


class CostModel:
    """Apply entry/exit costs and estimate P&L in account currency (USD)."""

    def __init__(self, config: CostConfig | None = None) -> None:
        self.config = config or CostConfig()

    def pip_size(self, symbol: str) -> float:
        return self.config.pip_size.get(symbol, 0.0001)

    def spread(self, symbol: str, bar_spread: float | None = None) -> float:
        if bar_spread is not None and bar_spread > 0:
            return bar_spread
        return self.config.default_spread.get(symbol, self.pip_size(symbol) * 1.2)

    def slippage(self, symbol: str) -> float:
        return self.config.slippage_pips * self.pip_size(symbol)

    def point_value(self, symbol: str) -> float:
        return self.config.point_value_per_lot.get(symbol, 100_000.0)

    def apply_entry_price(
        self,
        price: float,
        side: Side,
        symbol: str,
        *,
        bar_spread: float | None = None,
    ) -> float:
        """Worse fill: long pays half-spread + slippage; short receives less."""
        half_spread = self.spread(symbol, bar_spread) / 2.0
        slip = self.slippage(symbol)
        if side == Side.BUY:
            return price + half_spread + slip
        return price - half_spread - slip

    def apply_exit_price(
        self,
        price: float,
        side: Side,
        symbol: str,
        *,
        bar_spread: float | None = None,
    ) -> float:
        """Exit is opposite side of the market."""
        half_spread = self.spread(symbol, bar_spread) / 2.0
        slip = self.slippage(symbol)
        if side == Side.BUY:
            # Closing a long = sell
            return price - half_spread - slip
        return price + half_spread + slip

    def commission(self, volume: float) -> float:
        return abs(volume) * self.config.commission_per_lot

    def swap(
        self,
        symbol: str,
        volume: float,
        hold_bars: int,
        timeframe: str,
    ) -> float:
        if self.config.swap_per_lot_per_day == 0 or hold_bars <= 0:
            return 0.0
        per_day = self.config.bars_per_day.get(timeframe, 288)
        days = hold_bars / per_day
        return abs(volume) * self.config.swap_per_lot_per_day * days

    def pnl_usd(
        self,
        symbol: str,
        side: Side,
        volume: float,
        entry: float,
        exit_: float,
        *,
        hold_bars: int = 0,
        timeframe: str = "M5",
    ) -> float:
        """Net P&L after commission and swap (prices already cost-adjusted)."""
        direction = 1.0 if side == Side.BUY else -1.0
        gross = direction * (exit_ - entry) * volume * self.point_value(symbol)
        return gross - self.commission(volume) - self.swap(
            symbol, volume, hold_bars, timeframe
        )

    def roundtrip_cost_estimate(
        self,
        symbol: str,
        volume: float = 1.0,
        *,
        bar_spread: float | None = None,
        hold_bars: int = 0,
        timeframe: str = "M5",
    ) -> float:
        """Rough USD cost of opening+closing 1 lot (spread+slip+commission+swap)."""
        spread = self.spread(symbol, bar_spread)
        slip = 2.0 * self.slippage(symbol)  # in and out
        pv = self.point_value(symbol)
        friction = (spread + slip) * volume * pv
        return friction + self.commission(volume) + self.swap(
            symbol, volume, hold_bars, timeframe
        )


def side_from_direction(direction: Literal["LONG", "SHORT"]) -> Side:
    return Side.BUY if direction == "LONG" else Side.SELL
