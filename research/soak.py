"""Accelerated offline soak simulation (Phase 8).

Runs many autonomous-loop ticks in-process (not multi-day wall clock). Exercises
strategy → risk → control/halt/kill → heartbeats. No live broker orders.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research.backtest import BacktestConfig, Backtester
from research.costs import CostConfig, CostModel
from trading.config import RiskSettings
from trading.data.mock import MockMarketData
from trading.journal.db import JournalDB
from trading.monitoring.control import ControlStore
from trading.monitoring.heartbeat import HeartbeatStore
from trading.risk.context import RiskMarketContext, RiskPortfolioContext
from trading.risk.engine import RiskEngine
from trading.strategies.trend_pullback import TrendPullbackStrategy
from trading.types import (
    AccountState,
    AnalystDecision,
    Timeframe,
    TradeCandidate,
)


@dataclass(frozen=True, slots=True)
class SoakTick:
    tick: int
    action: str
    blocked: bool
    reason: str = ""
    heartbeat_ok: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "tick": self.tick,
            "action": self.action,
            "blocked": self.blocked,
            "reason": self.reason,
            "heartbeat_ok": self.heartbeat_ok,
        }


@dataclass(frozen=True, slots=True)
class SoakReport:
    ticks: int
    candidates_seen: int
    risk_approved: int
    risk_rejected: int
    control_blocks: int
    heartbeats: int
    halt_injected: bool
    kill_injected: bool
    resume_verified: bool
    backtest_trades: int
    passed: bool
    fail_reasons: list[str] = field(default_factory=list)
    sample_ticks: list[SoakTick] = field(default_factory=list)
    note: str = (
        "Accelerated offline soak — not a multi-day unattended demo. "
        "No live trades. Demo path remains gated."
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticks": self.ticks,
            "candidates_seen": self.candidates_seen,
            "risk_approved": self.risk_approved,
            "risk_rejected": self.risk_rejected,
            "control_blocks": self.control_blocks,
            "heartbeats": self.heartbeats,
            "halt_injected": self.halt_injected,
            "kill_injected": self.kill_injected,
            "resume_verified": self.resume_verified,
            "backtest_trades": self.backtest_trades,
            "passed": self.passed,
            "fail_reasons": list(self.fail_reasons),
            "sample_ticks": [t.to_dict() for t in self.sample_ticks],
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class SoakConfig:
    ticks: int = 40
    symbol: str = "EURUSD"
    timeframe: str = "M5"
    bars: int = 200
    halt_at_tick: int | None = 10
    kill_at_tick: int | None = 20
    resume_at_tick: int | None = 30
    heartbeat_every: int = 1
    starting_equity: float = 1000.0


def run_soak(
    *,
    db_path: str | Path,
    config: SoakConfig | None = None,
    risk_settings: RiskSettings | None = None,
    stale_after_seconds: float = 120.0,
) -> SoakReport:
    """Accelerated soak loop with control + heartbeat interaction."""
    cfg = config or SoakConfig()
    rs = risk_settings or RiskSettings(
        max_spread_pips=50.0,
        max_data_age_seconds=0,
        consecutive_loss_lock=3,
    )

    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = JournalDB(str(path))
    db.migrate()
    control = ControlStore(db)
    heartbeats = HeartbeatStore(db, stale_after_seconds=stale_after_seconds)
    risk = RiskEngine(rs)
    control.apply_to_risk(risk)

    market = MockMarketData(scenario="trend_pullback", seed=42)
    tf = Timeframe(cfg.timeframe)
    bars = market.get_bars(cfg.symbol, tf, count=cfg.bars)
    strategy = TrendPullbackStrategy()

    # Warm-up: one offline backtest slice to confirm strategy+risk path.
    bt = Backtester(
        strategy=strategy,
        costs=CostModel(CostConfig()),
        config=BacktestConfig(
            use_risk_engine=True,
            starting_equity=cfg.starting_equity,
            one_position=True,
        ),
        risk=RiskEngine(rs),
    )
    bt_report = bt.run(cfg.symbol, tf, bars)

    candidates_seen = 0
    risk_approved = 0
    risk_rejected = 0
    control_blocks = 0
    hb_count = 0
    halt_injected = False
    kill_injected = False
    resume_verified = False
    sample: list[SoakTick] = []
    fail: list[str] = []

    # Cursor into bar history — advance each tick (accelerated time).
    cursor = max(64, len(bars) // 4)
    equity = cfg.starting_equity
    consecutive_losses = 0

    for tick in range(cfg.ticks):
        # Injected control events (operator simulation).
        if cfg.halt_at_tick is not None and tick == cfg.halt_at_tick:
            control.halt("SOAK_HALT", source="system")
            control.apply_to_risk(risk)
            halt_injected = True
        if cfg.kill_at_tick is not None and tick == cfg.kill_at_tick:
            control.kill("SOAK_KILL", source="system")
            control.apply_to_risk(risk)
            kill_injected = True
        if cfg.resume_at_tick is not None and tick == cfg.resume_at_tick:
            control.resume(clear_kill=True, source="system")
            # Rebuild risk from store (clear in-memory locks).
            risk = RiskEngine(rs)
            control.apply_to_risk(risk)
            resume_verified = not control.get().new_entries_blocked

        if tick % cfg.heartbeat_every == 0:
            heartbeats.beat("soak", {"tick": tick})
            hb_count += 1

        state = control.get()
        action = "idle"
        blocked = state.new_entries_blocked
        reason = state.block_reason

        if blocked:
            control_blocks += 1
            action = "blocked"
            # Fail-closed: do not evaluate new candidates for entry.
            sample.append(
                SoakTick(
                    tick=tick,
                    action=action,
                    blocked=True,
                    reason=reason,
                    heartbeat_ok=True,
                )
            )
            cursor = min(cursor + 1, len(bars) - 2)
            continue

        # Evaluate strategy at cursor.
        window = bars[: cursor + 1]
        candidate = strategy.evaluate(cfg.symbol, window, at_index=cursor)
        if candidate is None:
            action = "no_setup"
        else:
            candidates_seen += 1
            decision = _risk_decide(
                risk,
                candidate,
                signal_bar_time=window[cursor].time,
                spread=window[cursor].spread,
                equity=equity,
                consecutive_losses=consecutive_losses,
            )
            if decision.approved:
                risk_approved += 1
                action = "approved"
                # Simulated tiny scratch PnL for soak bookkeeping (not a fill).
                # Alternate win/loss to exercise consecutive counter lightly.
                pnl = 0.5 if tick % 5 else -0.8
                equity += pnl
                if pnl < 0:
                    consecutive_losses += 1
                else:
                    consecutive_losses = 0
                risk.record_closed_trade(pnl)
            else:
                risk_rejected += 1
                action = f"rejected:{decision.reason_code}"
                if decision.reason_code in (
                    "DAILY_LOSS_LOCK",
                    "WEEKLY_DRAWDOWN_LOCK",
                    "CONSECUTIVE_LOSS_LOCK",
                ):
                    # Mirror P7 auto-halt behavior for account locks.
                    control.halt(decision.reason_code, source="auto")
                    control.apply_to_risk(risk)

        if tick < 8 or tick in {
            cfg.halt_at_tick,
            cfg.kill_at_tick,
            cfg.resume_at_tick,
            cfg.ticks - 1,
        }:
            sample.append(
                SoakTick(
                    tick=tick,
                    action=action,
                    blocked=blocked,
                    reason=reason,
                    heartbeat_ok=True,
                )
            )

        cursor = min(cursor + 1, len(bars) - 2)

    # Pass criteria for accelerated soak.
    if cfg.halt_at_tick is not None and not halt_injected:
        fail.append("halt_not_injected")
    if cfg.kill_at_tick is not None and not kill_injected:
        fail.append("kill_not_injected")
    if cfg.resume_at_tick is not None and not resume_verified:
        fail.append("resume_not_verified")
    if hb_count < 1:
        fail.append("no_heartbeats")
    if cfg.halt_at_tick is not None and control_blocks < 1:
        fail.append("halt_did_not_block_entries")
    # After resume, control should not be blocked.
    if cfg.resume_at_tick is not None and control.get().new_entries_blocked:
        fail.append("still_blocked_after_resume")

    db.record_system_event(
        "soak_complete",
        {
            "ticks": cfg.ticks,
            "candidates_seen": candidates_seen,
            "risk_approved": risk_approved,
            "risk_rejected": risk_rejected,
            "control_blocks": control_blocks,
            "heartbeats": hb_count,
            "passed": len(fail) == 0,
        },
    )
    db.close()

    return SoakReport(
        ticks=cfg.ticks,
        candidates_seen=candidates_seen,
        risk_approved=risk_approved,
        risk_rejected=risk_rejected,
        control_blocks=control_blocks,
        heartbeats=hb_count,
        halt_injected=halt_injected,
        kill_injected=kill_injected,
        resume_verified=resume_verified,
        backtest_trades=bt_report.trades,
        passed=len(fail) == 0,
        fail_reasons=fail,
        sample_ticks=sample,
    )


def _risk_decide(
    risk: RiskEngine,
    candidate: TradeCandidate,
    *,
    signal_bar_time: datetime,
    spread: float | None,
    equity: float,
    consecutive_losses: int,
):
    bar_time = signal_bar_time
    if bar_time.tzinfo is None:
        bar_time = bar_time.replace(tzinfo=timezone.utc)
    account = AccountState(balance=equity, equity=equity, account_mode="demo")
    portfolio = RiskPortfolioContext(
        day_start_equity=equity,  # soak keeps day flat for simplicity
        week_start_equity=equity,
        consecutive_losses=consecutive_losses,
        open_positions=[],
    )
    market = RiskMarketContext(now=bar_time, bar_time=bar_time, spread=spread)
    analyst = AnalystDecision(
        decision="APPROVE",
        confidence=1.0,
        risk_modifier=1.0,
        reason_code="SOAK",
    )
    return risk.validate_and_size(
        candidate,
        analyst,
        account,
        market=market,
        portfolio=portfolio,
    )
