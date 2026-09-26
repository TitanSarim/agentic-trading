"""Demo validation checklist — offline suite + optional LAN probes.

Cloud-safe by default: offline steps always run; Ollama/MT5 probes record
reachability without failing the offline package when LAN is blocked.
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

from research.backtest import BacktestConfig
from research.costs import CostConfig
from research.dod_report import build_dod_report, write_dod_report
from research.soak import SoakConfig, run_soak
from research.stress import run_stress_suite
from research.walk_forward import WalkForwardConfig, run_walk_forward
from trading.config import Settings
from trading.data.mock import MockMarketData
from trading.journal.db import JournalDB
from trading.llm.factory import create_llm
from trading.risk.engine import RiskEngine
from trading.strategies.trend_pullback import TrendPullbackConfig
from trading.types import Timeframe


@dataclass
class CheckResult:
    id: str
    name: str
    status: str  # PASS | FAIL | SKIP | BLOCKED
    scope: str  # offline | lan | live
    elapsed_sec: float = 0.0
    exit_code: int | None = None
    evidence: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DemoValidationReport:
    generated_at: str
    host: str
    platform: str
    offline_passed: bool
    lan_reachable: bool
    mt5_available: bool
    checks: list[CheckResult] = field(default_factory=list)
    note: str = (
        "Demo validation: offline suite is authoritative on cloud. "
        "LAN Ollama/MT5 rows require Windows on the IC Markets demo host."
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "host": self.host,
            "platform": self.platform,
            "offline_passed": self.offline_passed,
            "lan_reachable": self.lan_reachable,
            "mt5_available": self.mt5_available,
            "checks": [c.to_dict() for c in self.checks],
            "note": self.note,
        }

    def to_markdown(self) -> str:
        lines = [
            "# Demo validation checklist",
            "",
            f"- Generated: `{self.generated_at}`",
            f"- Host: `{self.host}` (`{self.platform}`)",
            f"- Offline package: **{'PASS' if self.offline_passed else 'FAIL'}**",
            f"- LAN Ollama: **{'reachable' if self.lan_reachable else 'blocked'}**",
            f"- MT5: **{'available' if self.mt5_available else 'unavailable'}**",
            "",
            self.note,
            "",
            "## Checks",
            "",
            "| ID | Status | Scope | Evidence |",
            "|----|--------|-------|----------|",
        ]
        for c in self.checks:
            evid = (c.evidence or "").replace("|", "/")[:120]
            lines.append(
                f"| `{c.id}` | {c.status} | {c.scope} | {evid} |"
            )
        lines.append("")
        return "\n".join(lines)


def _strategy_cfg(settings: Settings) -> TrendPullbackConfig:
    s = settings.strategy
    return TrendPullbackConfig(
        ema_fast=s.ema_fast,
        ema_slow=s.ema_slow,
        atr_period=s.atr_period,
        rsi_period=s.rsi_period,
        swing_lookback=s.swing_lookback,
        pullback_atr_frac=s.pullback_atr_frac,
        stop_atr_mult=s.stop_atr_mult,
        risk_reward=s.risk_reward,
        min_setup_score=s.min_setup_score,
        ema_touch_atr_frac=s.ema_touch_atr_frac,
    )


def _timed(fn: Callable[[], Any]) -> tuple[Any, float]:
    start = time.perf_counter()
    result = fn()
    return result, time.perf_counter() - start


def probe_ollama(
    base_url: str,
    *,
    connect_timeout: float = 3.0,
    read_timeout: float = 5.0,
) -> CheckResult:
    """HTTP GET /api/tags with short timeouts; never raises."""
    url = base_url.rstrip("/") + "/api/tags"
    start = time.perf_counter()
    try:
        with httpx.Client(
            timeout=httpx.Timeout(read_timeout, connect=connect_timeout)
        ) as client:
            resp = client.get(url)
            elapsed = time.perf_counter() - start
            models: list[str] = []
            if resp.status_code == 200:
                body = resp.json()
                models = [m.get("name", "") for m in body.get("models", [])]
                return CheckResult(
                    id="ollama_tags",
                    name="Ollama /api/tags",
                    status="PASS",
                    scope="lan",
                    elapsed_sec=round(elapsed, 3),
                    exit_code=0,
                    evidence=f"http={resp.status_code} models={models}",
                    detail={"models": models, "base_url": base_url},
                )
            return CheckResult(
                id="ollama_tags",
                name="Ollama /api/tags",
                status="FAIL",
                scope="lan",
                elapsed_sec=round(elapsed, 3),
                exit_code=1,
                evidence=f"http={resp.status_code}",
                detail={"base_url": base_url},
            )
    except Exception as exc:  # noqa: BLE001 — probe must never crash suite
        elapsed = time.perf_counter() - start
        return CheckResult(
            id="ollama_tags",
            name="Ollama /api/tags",
            status="BLOCKED",
            scope="lan",
            elapsed_sec=round(elapsed, 3),
            exit_code=28 if "timeout" in str(exc).lower() else 1,
            evidence=f"unreachable: {exc}",
            detail={"base_url": base_url, "error": str(exc)},
        )


def run_offline_package(
    settings: Settings,
    *,
    symbol: str = "EURUSD",
    timeframe: str = "M5",
    bars: int | None = None,
    ticks: int | None = None,
    output_dir: Path | None = None,
) -> tuple[CheckResult, dict[str, Any]]:
    """Walk-forward + stress + soak DoD package (same as ``trader validate``)."""
    v = settings.validation
    count = bars or v.default_bars
    n_ticks = ticks or v.soak_ticks

    def _run() -> dict[str, Any]:
        series = MockMarketData(scenario=v.scenario, seed=42).get_bars(
            symbol, Timeframe(timeframe), count=count
        )
        costs = CostConfig(
            commission_per_lot=settings.costs.commission_per_lot,
            slippage_pips=settings.costs.slippage_pips,
            swap_per_lot_per_day=settings.costs.swap_per_lot_per_day,
        )
        risk = RiskEngine(settings.risk) if settings.backtest.use_risk_engine else None
        wf = run_walk_forward(
            symbol,
            timeframe,
            series,
            strategy_config=_strategy_cfg(settings),
            cost_config=costs,
            backtest_config=BacktestConfig(
                volume=settings.backtest.volume,
                max_hold_bars=settings.backtest.max_hold_bars,
                one_position=settings.backtest.one_position,
                use_risk_engine=settings.backtest.use_risk_engine,
                starting_equity=settings.backtest.starting_equity,
            ),
            walk_config=WalkForwardConfig(
                train_bars=v.train_bars,
                test_bars=v.test_bars,
                step_bars=v.step_bars,
                mode=v.mode,
                min_folds=v.min_folds,
                min_oos_trades=v.min_oos_trades,
                max_oos_drawdown=v.max_oos_drawdown,
                require_non_negative_expectancy=v.require_non_negative_expectancy,
            ),
            risk=risk,
        )
        stress = run_stress_suite(
            symbol,
            timeframe,
            series,
            risk_settings=settings.risk.model_copy(
                update={"max_spread_pips": 50.0, "max_data_age_seconds": 0}
            ),
            base_cost=costs,
            backtest_config=BacktestConfig(
                volume=settings.backtest.volume,
                max_hold_bars=settings.backtest.max_hold_bars,
                one_position=True,
                use_risk_engine=True,
                starting_equity=settings.backtest.starting_equity,
            ),
            spread_mult=v.spread_shock_mult,
            slippage_pips=v.slippage_shock_pips,
        )
        soak = run_soak(
            db_path=Path("data/soak.db"),
            config=SoakConfig(
                ticks=n_ticks,
                symbol=symbol,
                timeframe=timeframe,
                bars=v.soak_bars,
                halt_at_tick=max(1, n_ticks // 4),
                kill_at_tick=max(2, n_ticks // 2),
                resume_at_tick=max(3, (3 * n_ticks) // 4),
            ),
            risk_settings=settings.risk.model_copy(
                update={"max_spread_pips": 50.0, "max_data_age_seconds": 0}
            ),
            stale_after_seconds=settings.monitoring.heartbeat_stale_seconds,
        )
        dod = build_dod_report(walk_forward=wf, stress=stress, soak=soak)
        out = output_dir or Path(v.report_dir)
        jp, mp = write_dod_report(dod, output_dir=out, stem="p8-validation")
        payload = dod.to_dict()
        payload["report_json"] = str(jp)
        payload["report_md"] = str(mp)
        return payload

    payload, elapsed = _timed(_run)
    ok = bool(payload.get("overall_passed"))
    return (
        CheckResult(
            id="offline_validate",
            name="trader validate (walk-forward + stress + soak)",
            status="PASS" if ok else "FAIL",
            scope="offline",
            elapsed_sec=round(elapsed, 3),
            exit_code=0 if ok else 1,
            evidence=(
                f"overall_passed={ok} "
                f"reports={payload.get('report_json')}"
            ),
            detail={
                "overall_passed": ok,
                "report_json": payload.get("report_json"),
                "report_md": payload.get("report_md"),
                "check_ids": [c["id"] for c in payload.get("checks", [])],
            },
        ),
        payload,
    )


def run_analyze_mock(settings: Settings) -> CheckResult:
    from trading.strategies.trend_pullback import TrendPullbackStrategy
    from trading.types import TradeCandidate

    def _run() -> dict[str, Any]:
        market = MockMarketData(scenario=settings.scanner.scenario)
        series = market.get_bars("EURUSD", Timeframe.M5, count=120)
        strat = TrendPullbackStrategy(_strategy_cfg(settings))
        candidate = None
        for i in range(len(series) - 1, 20, -1):
            candidate = strat.evaluate("EURUSD", series, at_index=i)
            if candidate is not None:
                break
        if candidate is None:
            candidate = TradeCandidate(
                symbol="EURUSD",
                strategy=settings.strategy.name,
                direction="LONG",
                entry=1.1000,
                stop=1.0980,
                target=1.1040,
                risk_reward=2.0,
                regime="trend",
                setup_score=75,
            )
        llm = create_llm(settings, force_mock=True)
        analyst = llm.validate_candidate(
            candidate, market_context={"symbol": "EURUSD", "timeframe": "M5"}
        )
        return {
            "decision": analyst.decision,
            "model": analyst.model,
            "risk_modifier": analyst.risk_modifier,
            "reason_code": analyst.reason_code,
        }

    detail, elapsed = _timed(_run)
    ok = detail.get("decision") in ("APPROVE", "REJECT")
    return CheckResult(
        id="analyze_mock",
        name="trader analyze --mock",
        status="PASS" if ok else "FAIL",
        scope="offline",
        elapsed_sec=round(elapsed, 3),
        exit_code=0 if ok else 1,
        evidence=(
            f"decision={detail.get('decision')} model={detail.get('model')} "
            f"modifier={detail.get('risk_modifier')}"
        ),
        detail=detail,
    )


def run_execute_demo_mock(settings: Settings) -> CheckResult:
    from brokers.mock import MockBroker
    from trading.execution.pipeline import run_demo_pipeline

    def _run() -> dict[str, Any]:
        db = JournalDB(settings.database.path)
        db.migrate()
        broker = MockBroker(account_mode=settings.mt5.account_mode)
        llm = create_llm(settings, force_mock=True)
        try:
            result = run_demo_pipeline(
                settings,
                broker=broker,
                db=db,
                llm=llm,
                symbol="EURUSD",
                timeframe="M5",
                bars=120,
                submit=False,
                confirm_demo=False,
                analyze=True,
            )
            return result.to_dict()
        finally:
            if broker.is_connected():
                broker.disconnect()
            db.close()

    try:
        detail, elapsed = _timed(_run)
        ok = detail.get("mode") == "dry-run" and detail.get("execution") is not None
        return CheckResult(
            id="execute_demo_mock",
            name="execute-demo --backend mock --dry-run",
            status="PASS" if ok else "FAIL",
            scope="offline",
            elapsed_sec=round(elapsed, 3),
            exit_code=0 if ok else 1,
            evidence=detail.get("message", ""),
            detail={
                "mode": detail.get("mode"),
                "account_mode": detail.get("account_mode"),
                "risk_approved": (detail.get("risk") or {}).get("approved"),
            },
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            id="execute_demo_mock",
            name="execute-demo --backend mock --dry-run",
            status="FAIL",
            scope="offline",
            evidence=str(exc),
        )


def run_execute_demo_mt5_probe(settings: Settings) -> CheckResult:
    """Attempt MT5 dry-run; BLOCKED on non-Windows or missing package."""
    if platform.system() != "Windows":
        return CheckResult(
            id="execute_demo_mt5",
            name="execute-demo --backend mt5 --dry-run",
            status="BLOCKED",
            scope="lan",
            evidence=f"Mt5Broker requires Windows (platform={platform.system().lower()})",
            detail={"platform": platform.system()},
        )

    from trading.execution.pipeline import run_demo_pipeline

    def _run() -> dict[str, Any]:
        from brokers.factory import create_broker

        mt5_settings = settings.model_copy(
            update={
                "broker": settings.broker.model_copy(update={"backend": "mt5"})
            }
        )
        db = JournalDB(mt5_settings.database.path)
        db.migrate()
        broker = create_broker(mt5_settings)
        llm = create_llm(mt5_settings, force_mock=True)
        try:
            result = run_demo_pipeline(
                mt5_settings,
                broker=broker,
                db=db,
                llm=llm,
                symbol="EURUSD",
                timeframe="M5",
                bars=120,
                submit=False,
                confirm_demo=False,
                analyze=True,
            )
            return result.to_dict()
        finally:
            if broker.is_connected():
                broker.disconnect()
            db.close()

    try:
        detail, elapsed = _timed(_run)
        ok = detail.get("mode") == "dry-run"
        return CheckResult(
            id="execute_demo_mt5",
            name="execute-demo --backend mt5 --dry-run",
            status="PASS" if ok else "FAIL",
            scope="lan",
            elapsed_sec=round(elapsed, 3),
            exit_code=0 if ok else 1,
            evidence=detail.get("message", ""),
            detail={
                "mode": detail.get("mode"),
                "account_mode": detail.get("account_mode"),
            },
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            id="execute_demo_mt5",
            name="execute-demo --backend mt5 --dry-run",
            status="BLOCKED",
            scope="lan",
            evidence=str(exc),
            detail={"error": str(exc)},
        )


def run_pytest() -> CheckResult:
    start = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=str(Path.cwd()),
        capture_output=True,
        text=True,
    )
    elapsed = time.perf_counter() - start
    tail = (proc.stdout or proc.stderr or "").strip().splitlines()
    summary = tail[-1] if tail else f"exit={proc.returncode}"
    return CheckResult(
        id="pytest",
        name="pytest",
        status="PASS" if proc.returncode == 0 else "FAIL",
        scope="offline",
        elapsed_sec=round(elapsed, 3),
        exit_code=proc.returncode,
        evidence=summary[:200],
        detail={"stdout_tail": "\n".join(tail[-5:])},
    )


def run_demo_validation(
    settings: Settings,
    *,
    symbol: str = "EURUSD",
    timeframe: str = "M5",
    bars: int | None = None,
    ticks: int | None = None,
    run_tests: bool = True,
    probe_lan: bool = True,
    output_dir: Path | None = None,
) -> DemoValidationReport:
    """Full demo checklist used by ``trader validate-demo``."""
    checks: list[CheckResult] = []

    if run_tests:
        checks.append(run_pytest())

    checks.append(run_analyze_mock(settings))
    checks.append(run_execute_demo_mock(settings))
    offline_check, _dod = run_offline_package(
        settings,
        symbol=symbol,
        timeframe=timeframe,
        bars=bars,
        ticks=ticks,
        output_dir=output_dir,
    )
    checks.append(offline_check)

    lan_reachable = False
    mt5_available = False
    if probe_lan:
        ollama = probe_ollama(settings.ollama.base_url)
        checks.append(ollama)
        lan_reachable = ollama.status == "PASS"

        if lan_reachable:
            llm = create_llm(settings, force_mock=False, backend="ollama")
            start = time.perf_counter()
            try:
                healthy = llm.health()
                models = llm.list_models() if healthy else []
                elapsed = time.perf_counter() - start
                checks.append(
                    CheckResult(
                        id="analyze_live_ready",
                        name="Ollama models listed (live analyze ready)",
                        status="PASS" if healthy else "FAIL",
                        scope="lan",
                        elapsed_sec=round(elapsed, 3),
                        evidence=f"models={models}",
                        detail={"models": models},
                    )
                )
            except Exception as exc:  # noqa: BLE001
                checks.append(
                    CheckResult(
                        id="analyze_live_ready",
                        name="Ollama models listed (live analyze ready)",
                        status="FAIL",
                        scope="lan",
                        evidence=str(exc),
                    )
                )
        else:
            checks.append(
                CheckResult(
                    id="analyze_live",
                    name="trader analyze --live",
                    status="BLOCKED",
                    scope="lan",
                    evidence="Skipped — Ollama unreachable from this host",
                )
            )

        mt5 = run_execute_demo_mt5_probe(settings)
        checks.append(mt5)
        mt5_available = mt5.status == "PASS"
    else:
        checks.append(
            CheckResult(
                id="lan_probes",
                name="LAN Ollama/MT5 probes",
                status="SKIP",
                scope="lan",
                evidence="--no-lan-probe",
            )
        )

    offline_ids = {"pytest", "analyze_mock", "execute_demo_mock", "offline_validate"}
    offline_checks = [c for c in checks if c.id in offline_ids]
    offline_passed = all(c.status == "PASS" for c in offline_checks) and bool(
        offline_checks
    )

    report = DemoValidationReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        host=platform.node(),
        platform=f"{platform.system()} {platform.release()} ({platform.machine()})",
        offline_passed=offline_passed,
        lan_reachable=lan_reachable,
        mt5_available=mt5_available,
        checks=checks,
    )

    db = JournalDB(settings.database.path)
    db.migrate()
    db.record_system_event("validate_demo_complete", report.to_dict())
    db.close()

    return report


def write_demo_validation_report(
    report: DemoValidationReport,
    output_dir: Path,
    stem: str = "demo-validation",
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    jp = output_dir / f"{stem}.json"
    mp = output_dir / f"{stem}.md"
    jp.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    mp.write_text(report.to_markdown(), encoding="utf-8")
    return jp, mp
