"""Stress scenarios for research + RiskEngine (Phase 8).

Offline only. Scenarios exercise cost shocks and risk locks — not live orders.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from research.backtest import BacktestConfig, BacktestReport, Backtester
from research.costs import CostConfig, CostModel
from trading.config import RiskSettings
from trading.risk.context import RiskMarketContext, RiskPortfolioContext
from trading.risk.engine import RiskEngine
from trading.strategies.trend_pullback import TrendPullbackStrategy
from trading.types import (
    AccountState,
    AnalystDecision,
    Bar,
    Timeframe,
    TradeCandidate,
)

ScenarioName = Literal[
    "baseline",
    "spread_shock",
    "slippage_shock",
    "gap_shock",
    "consecutive_losses",
]


@dataclass(frozen=True, slots=True)
class StressScenarioResult:
    name: str
    passed: bool
    fail_reasons: list[str] = field(default_factory=list)
    backtest: dict[str, Any] | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "fail_reasons": list(self.fail_reasons),
            "backtest": self.backtest,
            "extras": dict(self.extras),
        }


@dataclass(frozen=True, slots=True)
class StressReport:
    symbol: str
    timeframe: str
    scenarios: list[StressScenarioResult] = field(default_factory=list)
    passed: bool = False
    fail_reasons: list[str] = field(default_factory=list)
    note: str = (
        "Stress scenarios on synthetic bars. Offline — not live fill statistics."
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "scenario_count": len(self.scenarios),
            "passed": self.passed,
            "fail_reasons": list(self.fail_reasons),
            "scenarios": [s.to_dict() for s in self.scenarios],
            "note": self.note,
        }


def _widen_spreads(bars: list[Bar], mult: float) -> list[Bar]:
    out: list[Bar] = []
    for b in bars:
        spread = (b.spread or 0.0) * mult
        out.append(b.model_copy(update={"spread": spread}))
    return out


def _inject_gaps(bars: list[Bar], *, every: int = 40, gap_frac: float = 0.008) -> list[Bar]:
    """Force open gaps vs prior close on selected bars."""
    if not bars:
        return []
    out: list[Bar] = [bars[0]]
    for i in range(1, len(bars)):
        b = bars[i]
        if i % every == 0:
            prior = out[-1].close
            gap = prior * gap_frac
            # Gap down then continue — stresses gap eligibility / stop paths.
            open_ = prior - gap
            high = max(open_, b.high, b.close)
            low = min(open_, b.low, b.close)
            out.append(
                b.model_copy(
                    update={
                        "open": open_,
                        "high": high,
                        "low": low,
                        # Keep close path but ensure OHLC consistency.
                        "close": b.close,
                    }
                )
            )
        else:
            out.append(b)
    return out


def _run_backtest(
    symbol: str,
    timeframe: str,
    bars: list[Bar],
    *,
    costs: CostModel,
    risk: RiskEngine | None,
    bt_cfg: BacktestConfig,
) -> BacktestReport:
    return Backtester(
        strategy=TrendPullbackStrategy(),
        costs=costs,
        config=bt_cfg,
        risk=risk,
    ).run(symbol, timeframe, bars)


def run_stress_suite(
    symbol: str,
    timeframe: Timeframe | str,
    bars: list[Bar],
    *,
    risk_settings: RiskSettings | None = None,
    base_cost: CostConfig | None = None,
    backtest_config: BacktestConfig | None = None,
    spread_mult: float = 5.0,
    slippage_pips: float = 3.0,
    scenarios: list[ScenarioName] | None = None,
) -> StressReport:
    """Run baseline + shock scenarios; assert RiskEngine still gates losses."""
    tf = timeframe.value if isinstance(timeframe, Timeframe) else str(timeframe)
    rs = risk_settings or RiskSettings(
        max_spread_pips=50.0,
        max_data_age_seconds=0,
    )
    cost_cfg = base_cost or CostConfig()
    bt_cfg = backtest_config or BacktestConfig(
        use_risk_engine=True,
        starting_equity=1000.0,
        one_position=True,
        max_hold_bars=48,
    )
    wanted: list[ScenarioName] = scenarios or [
        "baseline",
        "spread_shock",
        "slippage_shock",
        "gap_shock",
        "consecutive_losses",
    ]

    results: list[StressScenarioResult] = []

    if "baseline" in wanted:
        risk = RiskEngine(rs)
        report = _run_backtest(
            symbol,
            tf,
            bars,
            costs=CostModel(cost_cfg),
            risk=risk if bt_cfg.use_risk_engine else None,
            bt_cfg=bt_cfg,
        )
        results.append(
            StressScenarioResult(
                name="baseline",
                passed=True,
                backtest=report.to_dict(),
                extras={"trades": report.trades, "risk_rejects": report.risk_rejects},
            )
        )

    if "spread_shock" in wanted:
        shocked = _widen_spreads(bars, spread_mult)
        # Tight spread gate so wide spreads should reject or degrade.
        tight = rs.model_copy(update={"max_spread_pips": 2.0})
        risk = RiskEngine(tight)
        shock_bt = BacktestConfig(
            volume=bt_cfg.volume,
            max_hold_bars=bt_cfg.max_hold_bars,
            one_position=bt_cfg.one_position,
            use_risk_engine=True,
            starting_equity=bt_cfg.starting_equity,
            analyst_risk_modifier=bt_cfg.analyst_risk_modifier,
            respect_spread_gate=True,
        )
        report = _run_backtest(
            symbol,
            tf,
            shocked,
            costs=CostModel(cost_cfg),
            risk=risk,
            bt_cfg=shock_bt,
        )
        # Pass if risk rejected some candidates OR expectancy not magically better.
        base_exp = 0.0
        for prev in results:
            if prev.name == "baseline" and prev.backtest:
                base_exp = float(prev.backtest.get("expectancy", 0.0))
        fail: list[str] = []
        # With tight gate, expect rejects or worse net expectancy vs baseline.
        if report.risk_rejects == 0 and report.expectancy > base_exp + 1e-9:
            fail.append("spread_shock_did_not_degrade_or_reject")
        results.append(
            StressScenarioResult(
                name="spread_shock",
                passed=len(fail) == 0,
                fail_reasons=fail,
                backtest=report.to_dict(),
                extras={
                    "spread_mult": spread_mult,
                    "max_spread_pips": tight.max_spread_pips,
                    "risk_rejects": report.risk_rejects,
                },
            )
        )

    if "slippage_shock" in wanted:
        slip_cfg = CostConfig(
            commission_per_lot=cost_cfg.commission_per_lot,
            slippage_pips=slippage_pips,
            swap_per_lot_per_day=cost_cfg.swap_per_lot_per_day,
        )
        risk = RiskEngine(rs)
        report = _run_backtest(
            symbol,
            tf,
            bars,
            costs=CostModel(slip_cfg),
            risk=risk if bt_cfg.use_risk_engine else None,
            bt_cfg=bt_cfg,
        )
        base_net = 0.0
        for prev in results:
            if prev.name == "baseline" and prev.backtest:
                base_net = float(prev.backtest.get("net_pnl", 0.0))
        fail = []
        # Higher slippage should not improve net PnL vs baseline (same path).
        if report.net_pnl > base_net + 1e-6:
            fail.append("slippage_shock_improved_net_pnl")
        results.append(
            StressScenarioResult(
                name="slippage_shock",
                passed=len(fail) == 0,
                fail_reasons=fail,
                backtest=report.to_dict(),
                extras={"slippage_pips": slippage_pips, "baseline_net_pnl": base_net},
            )
        )

    if "gap_shock" in wanted:
        gapped = _inject_gaps(bars)
        risk = RiskEngine(rs)
        report = _run_backtest(
            symbol,
            tf,
            gapped,
            costs=CostModel(cost_cfg),
            risk=risk if bt_cfg.use_risk_engine else None,
            bt_cfg=bt_cfg,
        )
        # Gap path must complete without exception; report metrics present.
        fail = []
        if "max_drawdown" not in report.to_dict():
            fail.append("gap_shock_missing_metrics")
        results.append(
            StressScenarioResult(
                name="gap_shock",
                passed=len(fail) == 0,
                fail_reasons=fail,
                backtest=report.to_dict(),
                extras={"bars": len(gapped)},
            )
        )

    if "consecutive_losses" in wanted:
        # Direct RiskEngine exercise: record losses until lock trips.
        risk = RiskEngine(rs)
        lock_n = rs.consecutive_loss_lock
        for _ in range(lock_n):
            risk.record_closed_trade(-1.0)
        # Build a dummy candidate and ask risk to size — must reject.
        candidate = TradeCandidate(
            symbol=symbol,
            strategy="trend_pullback_v1",
            direction="LONG",
            entry=1.1000,
            stop=1.0980,
            target=1.1040,
            risk_reward=2.0,
            regime="trend",
            setup_score=80,
        )
        account = AccountState(balance=1000.0, equity=1000.0, account_mode="demo")
        from datetime import datetime, timezone

        now = datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)
        market = RiskMarketContext(now=now, bar_time=now, spread=0.00012)
        portfolio = RiskPortfolioContext(
            day_start_equity=1000.0,
            week_start_equity=1000.0,
            consecutive_losses=risk.consecutive_losses,
            open_positions=[],
        )
        analyst = AnalystDecision(
            decision="APPROVE",
            confidence=1.0,
            risk_modifier=1.0,
            reason_code="STRESS",
        )
        decision = risk.validate_and_size(
            candidate,
            analyst,
            account,
            market=market,
            portfolio=portfolio,
        )
        fail = []
        if decision.approved:
            fail.append("consecutive_loss_lock_did_not_reject")
        if risk.consecutive_losses < lock_n:
            fail.append("consecutive_loss_counter_short")
        results.append(
            StressScenarioResult(
                name="consecutive_losses",
                passed=len(fail) == 0,
                fail_reasons=fail,
                backtest=None,
                extras={
                    "consecutive_losses": risk.consecutive_losses,
                    "lock_n": lock_n,
                    "approved": decision.approved,
                    "reason_code": decision.reason_code,
                },
            )
        )

    all_fail = [r for r in results if not r.passed]
    top_fail = [f"{r.name}:{','.join(r.fail_reasons)}" for r in all_fail]
    return StressReport(
        symbol=symbol,
        timeframe=tf,
        scenarios=results,
        passed=len(all_fail) == 0,
        fail_reasons=top_fail,
    )
