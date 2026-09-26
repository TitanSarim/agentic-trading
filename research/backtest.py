"""Offline backtest engine — deterministic strategy + cost model.

No live orders. Entry fills on the *next* bar open after a signal on a closed
bar (avoids same-bar look-ahead). Exits at stop / target using bar high/low
with cost-adjusted prices.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from research.costs import CostModel, CostConfig, side_from_direction
from trading.strategies.trend_pullback import TrendPullbackConfig, TrendPullbackStrategy
from trading.types import Bar, Side, Timeframe, TradeCandidate


@dataclass(frozen=True, slots=True)
class BacktestTrade:
    symbol: str
    timeframe: str
    direction: str
    signal_index: int
    entry_index: int
    exit_index: int
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    stop: float
    target: float
    volume: float
    pnl: float
    bars_held: int
    exit_reason: str
    setup_score: float


@dataclass(frozen=True, slots=True)
class BacktestReport:
    symbol: str
    timeframe: str
    strategy: str
    trades: int
    wins: int
    losses: int
    win_rate: float
    expectancy: float
    profit_factor: float
    max_drawdown: float
    net_pnl: float
    avg_win: float
    avg_loss: float
    gross_profit: float
    gross_loss: float
    trade_list: list[BacktestTrade] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "strategy": self.strategy,
            "trades": self.trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(self.win_rate, 4),
            "expectancy": round(self.expectancy, 4),
            "profit_factor": round(self.profit_factor, 4),
            "max_drawdown": round(self.max_drawdown, 4),
            "net_pnl": round(self.net_pnl, 4),
            "avg_win": round(self.avg_win, 4),
            "avg_loss": round(self.avg_loss, 4),
            "gross_profit": round(self.gross_profit, 4),
            "gross_loss": round(self.gross_loss, 4),
        }


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    volume: float = 0.10  # fixed research size (risk engine not sizing here)
    max_hold_bars: int = 48
    one_position: bool = True


class Backtester:
    """Replay bars for one symbol/timeframe with trend_pullback_v1."""

    def __init__(
        self,
        *,
        strategy: TrendPullbackStrategy | None = None,
        costs: CostModel | None = None,
        config: BacktestConfig | None = None,
    ) -> None:
        self.strategy = strategy or TrendPullbackStrategy()
        self.costs = costs or CostModel()
        self.config = config or BacktestConfig()

    def run(
        self,
        symbol: str,
        timeframe: Timeframe | str,
        bars: list[Bar],
    ) -> BacktestReport:
        tf = timeframe.value if isinstance(timeframe, Timeframe) else str(timeframe)
        trades: list[BacktestTrade] = []
        i = 0
        n = len(bars)
        while i < n - 1:
            candidate = self.strategy.evaluate(symbol, bars, at_index=i)
            if candidate is None:
                i += 1
                continue
            entry_i = i + 1
            if entry_i >= n:
                break
            trade = self._simulate_trade(
                symbol=symbol,
                timeframe=tf,
                bars=bars,
                signal_index=i,
                entry_index=entry_i,
                candidate=candidate,
            )
            if trade is not None:
                trades.append(trade)
                if self.config.one_position:
                    i = trade.exit_index + 1
                    continue
            i += 1
        return _build_report(symbol, tf, self.strategy.name, trades)

    def _simulate_trade(
        self,
        *,
        symbol: str,
        timeframe: str,
        bars: list[Bar],
        signal_index: int,
        entry_index: int,
        candidate: TradeCandidate,
    ) -> BacktestTrade | None:
        side = side_from_direction(candidate.direction)
        entry_bar = bars[entry_index]
        entry = self.costs.apply_entry_price(
            entry_bar.open,
            side,
            symbol,
            bar_spread=entry_bar.spread,
        )
        # Rebuild stop/target from filled entry preserving original risk distance.
        risk = abs(candidate.entry - candidate.stop)
        if risk <= 0:
            return None
        if candidate.direction == "LONG":
            stop = entry - risk
            target = entry + self.strategy.config.risk_reward * risk
        else:
            stop = entry + risk
            target = entry - self.strategy.config.risk_reward * risk

        volume = self.config.volume
        last_i = min(len(bars) - 1, entry_index + self.config.max_hold_bars)
        exit_price = entry
        exit_i = last_i
        exit_reason = "TIME"

        for j in range(entry_index, last_i + 1):
            bar = bars[j]
            hit_stop, hit_target = _path_hits(
                side=side,
                bar=bar,
                stop=stop,
                target=target,
            )
            # Conservative: if both touched in same bar, assume stop first.
            if hit_stop:
                exit_price = self.costs.apply_exit_price(
                    stop, side, symbol, bar_spread=bar.spread
                )
                exit_i = j
                exit_reason = "STOP"
                break
            if hit_target:
                exit_price = self.costs.apply_exit_price(
                    target, side, symbol, bar_spread=bar.spread
                )
                exit_i = j
                exit_reason = "TARGET"
                break
        else:
            # Time exit at last bar close.
            exit_bar = bars[exit_i]
            exit_price = self.costs.apply_exit_price(
                exit_bar.close, side, symbol, bar_spread=exit_bar.spread
            )
            exit_reason = "TIME"

        hold = exit_i - entry_index
        pnl = self.costs.pnl_usd(
            symbol,
            side,
            volume,
            entry,
            exit_price,
            hold_bars=hold,
            timeframe=timeframe,
        )
        return BacktestTrade(
            symbol=symbol,
            timeframe=timeframe,
            direction=candidate.direction,
            signal_index=signal_index,
            entry_index=entry_index,
            exit_index=exit_i,
            entry_time=entry_bar.time,
            exit_time=bars[exit_i].time,
            entry_price=entry,
            exit_price=exit_price,
            stop=stop,
            target=target,
            volume=volume,
            pnl=pnl,
            bars_held=hold,
            exit_reason=exit_reason,
            setup_score=candidate.setup_score,
        )


def _path_hits(
    *,
    side: Side,
    bar: Bar,
    stop: float,
    target: float,
) -> tuple[bool, bool]:
    if side == Side.BUY:
        return bar.low <= stop, bar.high >= target
    return bar.high >= stop, bar.low <= target


def _build_report(
    symbol: str,
    timeframe: str,
    strategy: str,
    trades: list[BacktestTrade],
) -> BacktestReport:
    if not trades:
        return BacktestReport(
            symbol=symbol,
            timeframe=timeframe,
            strategy=strategy,
            trades=0,
            wins=0,
            losses=0,
            win_rate=0.0,
            expectancy=0.0,
            profit_factor=0.0,
            max_drawdown=0.0,
            net_pnl=0.0,
            avg_win=0.0,
            avg_loss=0.0,
            gross_profit=0.0,
            gross_loss=0.0,
            trade_list=[],
        )

    pnls = [t.pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    net = sum(pnls)
    pf = gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
    # Cap inf for JSON-friendly report.
    if pf == float("inf"):
        pf = 999.0
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    return BacktestReport(
        symbol=symbol,
        timeframe=timeframe,
        strategy=strategy,
        trades=len(trades),
        wins=len(wins),
        losses=len(losses),
        win_rate=len(wins) / len(trades),
        expectancy=net / len(trades),
        profit_factor=pf,
        max_drawdown=max_dd,
        net_pnl=net,
        avg_win=(sum(wins) / len(wins)) if wins else 0.0,
        avg_loss=(sum(losses) / len(losses)) if losses else 0.0,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        trade_list=trades,
    )


def run_universe_backtest(
    bars_by_key: dict[tuple[str, str], list[Bar]],
    *,
    strategy_config: TrendPullbackConfig | None = None,
    cost_config: CostConfig | None = None,
    backtest_config: BacktestConfig | None = None,
) -> list[BacktestReport]:
    bt = Backtester(
        strategy=TrendPullbackStrategy(strategy_config),
        costs=CostModel(cost_config),
        config=backtest_config or BacktestConfig(),
    )
    reports: list[BacktestReport] = []
    for (symbol, tf), bars in sorted(bars_by_key.items()):
        reports.append(bt.run(symbol, tf, bars))
    return reports
