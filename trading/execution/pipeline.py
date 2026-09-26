"""Demo execution pipeline: scan/strategy → risk → optional analyze → execute."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from brokers.base import BrokerPort
from trading.config import Settings
from trading.data.mock import MockMarketData
from trading.execution.engine import (
    DemoConfirmRequired,
    ExecutionEngine,
    ExecutionResult,
    LiveTradingRefused,
)
from trading.execution.reconcile import BrokerReconciler, ReconcileReport
from trading.journal.db import JournalDB
from trading.llm.base import LlmPort
from trading.llm.pipeline import analyze_candidates
from trading.monitoring.alerts import AlertSink
from trading.monitoring.control import ControlStore
from trading.monitoring.heartbeat import HeartbeatStore
from trading.risk.context import RiskMarketContext, RiskPortfolioContext
from trading.risk.engine import RiskEngine
from trading.scanner import (
    EligibilityContext,
    MarketScanner,
    MarketSnapshot,
    ScannerConfig,
)
from trading.strategies.trend_pullback import TrendPullbackConfig, TrendPullbackStrategy
from trading.types import (
    AnalystDecision,
    RiskDecision,
    Timeframe,
    TradeCandidate,
)


@dataclass
class DemoPipelineResult:
    mode: Literal["dry-run", "submit"]
    account_mode: str
    reconcile: ReconcileReport | None
    candidate: TradeCandidate | None
    candidate_id: int | None
    analyst: AnalystDecision | None
    risk: RiskDecision | None
    execution: ExecutionResult | None
    message: str = ""
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "account_mode": self.account_mode,
            "message": self.message,
            "reconcile": self.reconcile.to_dict() if self.reconcile else None,
            "candidate_id": self.candidate_id,
            "candidate": self.candidate.model_dump(mode="json") if self.candidate else None,
            "analyst": self.analyst.model_dump(mode="json") if self.analyst else None,
            "risk": self.risk.model_dump(mode="json") if self.risk else None,
            "execution": self.execution.to_dict() if self.execution else None,
            "extras": self.extras,
        }


def run_demo_pipeline(
    settings: Settings,
    *,
    broker: BrokerPort,
    db: JournalDB,
    llm: LlmPort | None = None,
    symbol: str = "EURUSD",
    timeframe: str = "M5",
    bars: int = 120,
    submit: bool = False,
    confirm_demo: bool = False,
    analyze: bool = True,
) -> DemoPipelineResult:
    """Wire scanner/strategy → optional LLM → risk → demo execute.

    Default is dry-run. Submit requires confirm_demo and account_mode=demo.
    """
    account_mode = settings.broker.mode or settings.mt5.account_mode
    mode: Literal["dry-run", "submit"] = "submit" if submit else "dry-run"

    risk_engine = RiskEngine(settings.risk)
    control = ControlStore(db)
    control_state = control.apply_to_risk(risk_engine)
    alerts = AlertSink(
        webhook_url=settings.monitoring.webhook_url,
        enabled=settings.monitoring.alerts_enabled,
    )
    HeartbeatStore(
        db,
        stale_after_seconds=settings.monitoring.heartbeat_stale_seconds,
    ).beat(
        "trader",
        {"pipeline": "execute-demo", "mode": mode, "symbol": symbol},
    )

    # Fail closed: persistent kill/halt blocks new entries (positions still managed elsewhere).
    if control_state.new_entries_blocked:
        reason = control_state.block_reason
        alerts.emit(
            "warning",
            reason or "ENTRIES_BLOCKED",
            f"execute-demo blocked by control: {reason}",
            control_state.to_dict(),
        )
        db.record_system_event(
            "execute_demo_blocked_control",
            {"reason": reason, "control": control_state.to_dict()},
        )
        return DemoPipelineResult(
            mode=mode,
            account_mode=account_mode,
            reconcile=None,
            candidate=None,
            candidate_id=None,
            analyst=None,
            risk=RiskDecision(
                approved=False,
                reason_code=reason or "ENTRIES_BLOCKED",
                new_trades_locked=True,
            ),
            execution=None,
            message=f"new entries blocked: {reason}",
            extras={"control": control_state.to_dict()},
        )

    engine = ExecutionEngine(
        broker,
        settings.execution,
        account_mode=account_mode,  # type: ignore[arg-type]
    )

    # Reconcile before any new order path.
    if not broker.is_connected():
        broker.connect()
    reconciler = BrokerReconciler(broker)
    report = reconciler.reconcile(db.open_position_tickets())
    db.record_system_event("reconcile", report.to_dict())
    if report.new_trades_should_lock:
        risk_engine.lock_new_trades("UNEXPECTED_BROKER_POSITION")
        if settings.monitoring.auto_halt_on_risk_lock:
            control.halt("UNEXPECTED_BROKER_POSITION", source="auto")
            alerts.emit(
                "error",
                "UNEXPECTED_BROKER_POSITION",
                report.message,
                report.to_dict(),
            )
        return DemoPipelineResult(
            mode=mode,
            account_mode=account_mode,
            reconcile=report,
            candidate=None,
            candidate_id=None,
            analyst=None,
            risk=None,
            execution=None,
            message=report.message,
        )

    account = broker.account_state()
    db.record_account_snapshot(account)

    # Build synthetic (or later MT5) bars for strategy — cloud-safe mock default.
    market = MockMarketData(
        symbols=[symbol],
        scenario=settings.scanner.scenario,
    )
    tf = Timeframe(timeframe)
    series = market.get_bars(symbol, tf, count=bars)
    as_of = datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)
    step = timedelta(minutes=5 if tf == Timeframe.M5 else 15)
    start = as_of - step * (len(series) - 1)
    anchored = [
        b.model_copy(update={"time": start + step * i}) for i, b in enumerate(series)
    ]

    scanner = MarketScanner(
        ScannerConfig(
            settings=settings.scanner,
            strategy=settings.strategy,
            evaluate_strategy=True,
            correlation_groups=list(settings.risk.correlation_groups),
            pip_size=dict(settings.risk.pip_size),
        ),
        risk_engine=risk_engine,
    )
    scan_report = scanner.rank(
        [MarketSnapshot(symbol=symbol, timeframe=tf, bars=anchored)],
        context=EligibilityContext(
            now=as_of,
            broker_available=True,
            api_healthy=True,
            market_open=True,
            risk_engine=risk_engine,
        ),
    )
    db.record_scan_report(scan_report)

    candidate: TradeCandidate | None = None
    for row in scan_report.selected:
        if row.candidate is not None:
            candidate = row.candidate
            break
    if candidate is None:
        # Fallback: evaluate strategy directly on bars.
        strategy = TrendPullbackStrategy(
            TrendPullbackConfig(
                ema_fast=settings.strategy.ema_fast,
                ema_slow=settings.strategy.ema_slow,
                atr_period=settings.strategy.atr_period,
                rsi_period=settings.strategy.rsi_period,
                swing_lookback=settings.strategy.swing_lookback,
                pullback_atr_frac=settings.strategy.pullback_atr_frac,
                stop_atr_mult=settings.strategy.stop_atr_mult,
                risk_reward=settings.strategy.risk_reward,
                min_setup_score=settings.strategy.min_setup_score,
                ema_touch_atr_frac=settings.strategy.ema_touch_atr_frac,
            )
        )
        for i in range(len(anchored) - 1, 20, -1):
            candidate = strategy.evaluate(symbol, anchored, at_index=i)
            if candidate is not None:
                break

    if candidate is None:
        return DemoPipelineResult(
            mode=mode,
            account_mode=account_mode,
            reconcile=report,
            candidate=None,
            candidate_id=None,
            analyst=None,
            risk=None,
            execution=None,
            message="no strategy candidate — nothing to execute",
            extras={"scan": scan_report.to_dict()},
        )

    cid = db.record_candidate(candidate)

    analyst: AnalystDecision | None = None
    if analyze and llm is not None:
        analyzed = analyze_candidates(llm, [candidate])
        analyst = analyzed[0].analyst
        db.record_llm_decision(analyst, candidate_id=cid)
        if analyst.decision != "APPROVE":
            return DemoPipelineResult(
                mode=mode,
                account_mode=account_mode,
                reconcile=report,
                candidate=candidate,
                candidate_id=cid,
                analyst=analyst,
                risk=None,
                execution=None,
                message=f"analyst rejected: {analyst.reason_code}",
            )
    else:
        # No LLM step — risk still sizes; synthetic pass-through (modifier=1.0).
        analyst = AnalystDecision(
            decision="APPROVE",
            confidence=1.0,
            risk_modifier=1.0,
            reason_code="NO_ANALYST_PASSTHROUGH",
            model="none",
        )

    last = anchored[-1]
    bar_time = last.time
    if bar_time.tzinfo is None:
        bar_time = bar_time.replace(tzinfo=timezone.utc)
    market_ctx = RiskMarketContext(
        now=bar_time,
        bar_time=bar_time,
        spread=last.spread if last.spread is not None else 0.00012,
    )
    portfolio = RiskPortfolioContext(
        day_start_equity=account.equity,
        week_start_equity=account.equity,
        consecutive_losses=0,
        open_positions=broker.positions(),
    )
    decision = risk_engine.validate_and_size(
        candidate,
        analyst,
        account,
        market=market_ctx,
        portfolio=portfolio,
    )
    db.record_risk_decision(decision, candidate_id=cid)

    if not decision.approved:
        # Auto-halt on account-level risk locks (daily/weekly/consecutive).
        lock_codes = {
            "DAILY_LOSS_LOCK",
            "WEEKLY_DRAWDOWN_LOCK",
            "CONSECUTIVE_LOSS_LOCK",
        }
        if (
            settings.monitoring.auto_halt_on_risk_lock
            and decision.reason_code in lock_codes
        ):
            control.halt(decision.reason_code, source="auto")
            alerts.emit(
                "error",
                decision.reason_code,
                f"auto halt on risk lock: {decision.reason_code}",
                decision.model_dump(mode="json"),
            )
        return DemoPipelineResult(
            mode=mode,
            account_mode=account_mode,
            reconcile=report,
            candidate=candidate,
            candidate_id=cid,
            analyst=analyst,
            risk=decision,
            execution=None,
            message=f"risk rejected: {decision.reason_code}",
        )

    try:
        plan = engine.build_plan(candidate, decision, candidate_id=cid)
        result = engine.execute(plan, submit=submit, confirm_demo=confirm_demo)
    except (LiveTradingRefused, DemoConfirmRequired, ValueError) as exc:
        db.record_system_event(
            "execute_demo_blocked",
            {"error": str(exc), "submit": submit, "account_mode": account_mode},
        )
        return DemoPipelineResult(
            mode=mode,
            account_mode=account_mode,
            reconcile=report,
            candidate=candidate,
            candidate_id=cid,
            analyst=analyst,
            risk=decision,
            execution=None,
            message=str(exc),
        )

    if result.order is not None:
        db.record_order_result(
            result.order,
            symbol=plan.candidate.symbol,
            side=plan.side.value,
            volume=plan.volume,
            stop_loss=plan.stop_loss,
            take_profit=plan.take_profit,
            dry_run=result.dry_run,
            account_mode=account_mode,
            candidate_id=cid,
            payload=result.to_dict(),
        )

    db.record_system_event("execute_demo_complete", result.to_dict())
    return DemoPipelineResult(
        mode=mode,
        account_mode=account_mode,
        reconcile=report,
        candidate=candidate,
        candidate_id=cid,
        analyst=analyst,
        risk=decision,
        execution=result,
        message=result.message,
    )
