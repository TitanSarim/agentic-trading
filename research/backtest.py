"""Offline backtest engine — deterministic strategy + cost model + optional risk.

No live orders. Entry fills on the *next* bar open after a signal on a closed
bar (avoids same-bar look-ahead). Exits at stop / target using bar high/low
with cost-adjusted prices.

When a RiskEngine is attached, candidates are sized/rejected by risk (boss).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from research.costs import CostModel, CostConfig, side_from_direction
from trading.risk.context import RiskMarketContext, RiskPortfolioContext
from trading.risk.engine import RiskEngine
from trading.strategies.trend_pullback import TrendPullbackConfig, TrendPullbackStrategy
from trading.types import (
    AccountState,
    AnalystDecision,
    Bar,
    Position,
    Side,
    Timeframe,
    TradeCandidate,
)


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
    risk_reason: str = "APPROVED"


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
    risk_rejects: int = 0
    ending_equity: float | None = None
    trade_list: list[BacktestTrade] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
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
            "risk_rejects": self.risk_rejects,
        }
        if self.ending_equity is not None:
            out["ending_equity"] = round(self.ending_equity, 4)
        return out


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    volume: float = 0.10  # fixed research size when risk engine is off
    max_hold_bars: int = 48
    one_position: bool = True
    use_risk_engine: bool = False
    starting_equity: float = 1000.0
    # Research backtests treat analyst as APPROVE with full modifier.
    analyst_risk_modifier: float = 1.0
    # Freshness: treat signal bar as "now" so historical replay is not stale.
    # Set max_data_age_seconds on RiskSettings; we pass bar_time == now.
    respect_spread_gate: bool = True


class Backtester:
    """Replay bars for one symbol/timeframe with trend_pullback_v1."""

    def __init__(
        self,
        *,
        strategy: TrendPullbackStrategy | None = None,
        costs: CostModel | None = None,
        config: BacktestConfig | None = None,
        risk: RiskEngine | None = None,
    ) -> None:
        self.strategy = strategy or TrendPullbackStrategy()
        self.costs = costs or CostModel()
        self.config = config or BacktestConfig()
        self.risk = risk

    def run(
        self,
        symbol: str,
        timeframe: Timeframe | str,
        bars: list[Bar],
    ) -> BacktestReport:
        tf = timeframe.value if isinstance(timeframe, Timeframe) else str(timeframe)
        trades: list[BacktestTrade] = []
        risk_rejects = 0
        equity = self.config.starting_equity
        day_start = equity
        week_start = equity
        consecutive_losses = 0
        open_positions: list[Position] = []

        i = 0
        n = len(bars)
        while i < n - 1:
            candidate = self.strategy.evaluate(symbol, bars, at_index=i)
            if candidate is None:
                i += 1
                continue

            volume = self.config.volume
            risk_reason = "FIXED_VOLUME"
            if self.config.use_risk_engine and self.risk is not None:
                decision = self._risk_decide(
                    candidate=candidate,
                    signal_bar=bars[i],
                    equity=equity,
                    day_start=day_start,
                    week_start=week_start,
                    consecutive_losses=consecutive_losses,
                    open_positions=open_positions,
                )
                if not decision.approved:
                    risk_rejects += 1
                    i += 1
                    continue
                volume = decision.volume
                risk_reason = decision.reason_code

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
                volume=volume,
                risk_reason=risk_reason,
            )
            if trade is not None:
                trades.append(trade)
                equity += trade.pnl
                if trade.pnl < 0:
                    consecutive_losses += 1
                else:
                    consecutive_losses = 0
                if self.risk is not None:
                    self.risk.record_closed_trade(trade.pnl)
                if self.config.one_position:
                    i = trade.exit_index + 1
                    continue
            i += 1
        report = _build_report(symbol, tf, self.strategy.name, trades)
        return BacktestReport(
            symbol=report.symbol,
            timeframe=report.timeframe,
            strategy=report.strategy,
            trades=report.trades,
            wins=report.wins,
            losses=report.losses,
            win_rate=report.win_rate,
            expectancy=report.expectancy,
            profit_factor=report.profit_factor,
            max_drawdown=report.max_drawdown,
            net_pnl=report.net_pnl,
            avg_win=report.avg_win,
            avg_loss=report.avg_loss,
            gross_profit=report.gross_profit,
            gross_loss=report.gross_loss,
            risk_rejects=risk_rejects,
            ending_equity=equity if self.config.use_risk_engine else None,
            trade_list=report.trade_list,
        )

    def _risk_decide(
        self,
        *,
        candidate: TradeCandidate,
        signal_bar: Bar,
        equity: float,
        day_start: float,
        week_start: float,
        consecutive_losses: int,
        open_positions: list[Position],
    ):
        assert self.risk is not None
        account = AccountState(
            balance=equity,
            equity=equity,
            account_mode="demo",
        )
        portfolio = RiskPortfolioContext(
            day_start_equity=day_start,
            week_start_equity=week_start,
            consecutive_losses=consecutive_losses,
            open_positions=list(open_positions),
        )
        # Historical replay: bar_time == now so freshness gate passes;
        # live path must pass wall-clock now vs bar time.
        bar_time = signal_bar.time
        if bar_time.tzinfo is None:
            bar_time = bar_time.replace(tzinfo=timezone.utc)
        market = RiskMarketContext(
            now=bar_time,
            bar_time=bar_time,
            spread=signal_bar.spread if self.config.respect_spread_gate else None,
        )
        analyst = AnalystDecision(
            decision="APPROVE",
            confidence=1.0,
            risk_modifier=self.config.analyst_risk_modifier,
            reason_code="BACKTEST",
        )
        return self.risk.validate_and_size(
            candidate,
            analyst,
            account,
            market=market,
            portfolio=portfolio,
        )

    def _simulate_trade(
        self,
        *,
        symbol: str,
        timeframe: str,
        bars: list[Bar],
        signal_index: int,
        entry_index: int,
        candidate: TradeCandidate,
        volume: float,
        risk_reason: str,
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
            risk_reason=risk_reason,
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
    risk: RiskEngine | None = None,
) -> list[BacktestReport]:
    bt = Backtester(
        strategy=TrendPullbackStrategy(strategy_config),
        costs=CostModel(cost_config),
        config=backtest_config or BacktestConfig(),
        risk=risk,
    )
    reports: list[BacktestReport] = []
    for (symbol, tf), bars in sorted(bars_by_key.items()):
        reports.append(bt.run(symbol, tf, bars))
    return reports
