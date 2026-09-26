"""Walk-forward / out-of-sample backtest harness (Phase 8).

Chronological holdouts only — no shuffled validation. Strategy is rule-based
(no parameter fit on train), but OOS folds still guard against same-window
optimism and cost sensitivity. Offline / synthetic; no live trades.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from research.backtest import BacktestConfig, BacktestReport, Backtester
from research.costs import CostConfig, CostModel
from trading.risk.engine import RiskEngine
from trading.strategies.trend_pullback import TrendPullbackConfig, TrendPullbackStrategy
from trading.types import Bar, Timeframe


WindowMode = Literal["rolling", "expanding"]


@dataclass(frozen=True, slots=True)
class WalkForwardConfig:
    """Chronological walk-forward windows (bar counts)."""

    train_bars: int = 200
    test_bars: int = 80
    step_bars: int = 80  # advance between folds (often == test_bars)
    mode: WindowMode = "rolling"
    min_folds: int = 2
    # Soft pass gates for research DoD (not live guarantees).
    min_oos_trades: int = 1
    max_oos_drawdown: float | None = None  # absolute USD DD; None = skip
    require_non_negative_expectancy: bool = False


@dataclass(frozen=True, slots=True)
class WalkForwardFold:
    fold_index: int
    train_start: int
    train_end: int  # exclusive
    test_start: int
    test_end: int  # exclusive
    train_report: BacktestReport | None
    test_report: BacktestReport

    def to_dict(self) -> dict[str, Any]:
        return {
            "fold_index": self.fold_index,
            "train_start": self.train_start,
            "train_end": self.train_end,
            "test_start": self.test_start,
            "test_end": self.test_end,
            "train": self.train_report.to_dict() if self.train_report else None,
            "test": self.test_report.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class WalkForwardReport:
    symbol: str
    timeframe: str
    strategy: str
    mode: str
    folds: list[WalkForwardFold] = field(default_factory=list)
    oos_trades: int = 0
    oos_wins: int = 0
    oos_losses: int = 0
    oos_win_rate: float = 0.0
    oos_expectancy: float = 0.0
    oos_profit_factor: float = 0.0
    oos_max_drawdown: float = 0.0
    oos_net_pnl: float = 0.0
    passed: bool = False
    fail_reasons: list[str] = field(default_factory=list)
    note: str = (
        "Out-of-sample chronological folds only. Offline research — not live performance."
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "strategy": self.strategy,
            "mode": self.mode,
            "fold_count": len(self.folds),
            "oos_trades": self.oos_trades,
            "oos_wins": self.oos_wins,
            "oos_losses": self.oos_losses,
            "oos_win_rate": round(self.oos_win_rate, 4),
            "oos_expectancy": round(self.oos_expectancy, 4),
            "oos_profit_factor": round(self.oos_profit_factor, 4),
            "oos_max_drawdown": round(self.oos_max_drawdown, 4),
            "oos_net_pnl": round(self.oos_net_pnl, 4),
            "passed": self.passed,
            "fail_reasons": list(self.fail_reasons),
            "folds": [f.to_dict() for f in self.folds],
            "note": self.note,
        }


def _fold_windows(
    n_bars: int,
    cfg: WalkForwardConfig,
) -> list[tuple[int, int, int, int]]:
    """Chronological windows.

    rolling: train=[i, i+train), test=[i+train, i+train+test)
    expanding: train=[0, i+train), test=[i+train, i+train+test)
    Advance i by step_bars each fold.
    """
    windows: list[tuple[int, int, int, int]] = []
    i = 0
    while True:
        test_start = i + cfg.train_bars
        test_end = test_start + cfg.test_bars
        if test_end > n_bars:
            break
        if cfg.mode == "expanding":
            train_start, train_end = 0, test_start
        else:
            train_start, train_end = i, test_start
        windows.append((train_start, train_end, test_start, test_end))
        i += cfg.step_bars
    return windows


class WalkForwardRunner:
    """Run chronological train/test folds through the existing Backtester."""

    def __init__(
        self,
        *,
        strategy: TrendPullbackStrategy | None = None,
        costs: CostModel | None = None,
        backtest_config: BacktestConfig | None = None,
        risk: RiskEngine | None = None,
        config: WalkForwardConfig | None = None,
        run_train: bool = True,
    ) -> None:
        self.strategy = strategy or TrendPullbackStrategy()
        self.costs = costs or CostModel()
        self.backtest_config = backtest_config or BacktestConfig()
        self.risk = risk
        self.config = config or WalkForwardConfig()
        self.run_train = run_train

    def run(
        self,
        symbol: str,
        timeframe: Timeframe | str,
        bars: list[Bar],
    ) -> WalkForwardReport:
        tf = timeframe.value if isinstance(timeframe, Timeframe) else str(timeframe)
        windows = _fold_windows(len(bars), self.config)
        folds: list[WalkForwardFold] = []

        for idx, (t0, t1, s0, s1) in enumerate(windows):
            # Fresh risk engine per fold so locks don't bleed across OOS segments.
            risk = None
            if self.backtest_config.use_risk_engine and self.risk is not None:
                risk = RiskEngine(self.risk.settings)
            engine = Backtester(
                strategy=self.strategy,
                costs=self.costs,
                config=self.backtest_config,
                risk=risk,
            )
            train_report = None
            if self.run_train:
                train_report = engine.run(symbol, tf, bars[t0:t1])
            # New engine again for test (independent equity path).
            risk_oos = None
            if self.backtest_config.use_risk_engine and self.risk is not None:
                risk_oos = RiskEngine(self.risk.settings)
            engine_oos = Backtester(
                strategy=self.strategy,
                costs=self.costs,
                config=self.backtest_config,
                risk=risk_oos,
            )
            test_report = engine_oos.run(symbol, tf, bars[s0:s1])
            folds.append(
                WalkForwardFold(
                    fold_index=idx,
                    train_start=t0,
                    train_end=t1,
                    test_start=s0,
                    test_end=s1,
                    train_report=train_report,
                    test_report=test_report,
                )
            )

        return _aggregate(symbol, tf, self.strategy.name, self.config, folds)


def _aggregate(
    symbol: str,
    timeframe: str,
    strategy: str,
    cfg: WalkForwardConfig,
    folds: list[WalkForwardFold],
) -> WalkForwardReport:
    fail: list[str] = []
    if len(folds) < cfg.min_folds:
        fail.append(f"insufficient_folds:{len(folds)}<{cfg.min_folds}")

    # Concatenate OOS trade PNLs in chronological fold order for DD/expectancy.
    pnls: list[float] = []
    wins = 0
    losses = 0
    for fold in folds:
        for t in fold.test_report.trade_list:
            pnls.append(t.pnl)
            if t.pnl > 0:
                wins += 1
            else:
                losses += 1

    n = len(pnls)
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p <= 0))
    net = sum(pnls)
    expectancy = net / n if n else 0.0
    pf = (
        gross_profit / gross_loss
        if gross_loss > 0
        else (999.0 if gross_profit > 0 else 0.0)
    )
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    if n < cfg.min_oos_trades:
        fail.append(f"oos_trades:{n}<{cfg.min_oos_trades}")
    if cfg.require_non_negative_expectancy and expectancy < 0:
        fail.append(f"oos_expectancy_negative:{expectancy:.4f}")
    if cfg.max_oos_drawdown is not None and max_dd > cfg.max_oos_drawdown:
        fail.append(f"oos_drawdown:{max_dd:.4f}>{cfg.max_oos_drawdown}")

    return WalkForwardReport(
        symbol=symbol,
        timeframe=timeframe,
        strategy=strategy,
        mode=cfg.mode,
        folds=folds,
        oos_trades=n,
        oos_wins=wins,
        oos_losses=losses,
        oos_win_rate=(wins / n) if n else 0.0,
        oos_expectancy=expectancy,
        oos_profit_factor=pf if pf != float("inf") else 999.0,
        oos_max_drawdown=max_dd,
        oos_net_pnl=net,
        passed=len(fail) == 0,
        fail_reasons=fail,
    )


def run_walk_forward(
    symbol: str,
    timeframe: Timeframe | str,
    bars: list[Bar],
    *,
    strategy_config: TrendPullbackConfig | None = None,
    cost_config: CostConfig | None = None,
    backtest_config: BacktestConfig | None = None,
    walk_config: WalkForwardConfig | None = None,
    risk: RiskEngine | None = None,
) -> WalkForwardReport:
    runner = WalkForwardRunner(
        strategy=TrendPullbackStrategy(strategy_config),
        costs=CostModel(cost_config),
        backtest_config=backtest_config or BacktestConfig(),
        risk=risk,
        config=walk_config or WalkForwardConfig(),
    )
    return runner.run(symbol, timeframe, bars)
