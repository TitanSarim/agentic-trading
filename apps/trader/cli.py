"""Trader CLI — health, status, kill/halt, execute-demo, walk-forward, soak.

Demo execution is dry-run by default. Live (real-money) path is disabled.
Phase 8 adds offline walk-forward, stress, and accelerated soak validation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
import yaml
from rich.console import Console

from brokers.factory import create_broker
from research.backtest import BacktestConfig, Backtester
from research.costs import CostConfig, CostModel
from research.dod_report import build_dod_report, write_dod_report
from research.soak import SoakConfig, run_soak
from research.stress import run_stress_suite
from research.walk_forward import WalkForwardConfig, run_walk_forward
from trading.config import load_settings
from trading.data.historical import HistoricalDownloader
from trading.data.mock import MockMarketData
from trading.execution.pipeline import run_demo_pipeline
from trading.journal.db import JournalDB
from trading.llm.factory import create_llm
from trading.llm.pipeline import analyze_candidates
from trading.logging_setup import configure_logging, get_logger
from trading.monitoring.alerts import AlertSink
from trading.monitoring.api import serve_control_api
from trading.monitoring.control import ControlStore
from trading.monitoring.heartbeat import HeartbeatStore
from trading.monitoring.status import build_status_report
from trading.risk.engine import RiskEngine
from trading.scanner import (
    EligibilityContext,
    MarketScanner,
    MarketSnapshot,
    ScannerConfig,
)
from trading.strategies.trend_pullback import TrendPullbackConfig, TrendPullbackStrategy
from trading.types import Timeframe

app = typer.Typer(
    name="trader",
    help="Agentic trading CLI (Phase 8 validation). Live path disabled.",
    add_completion=False,
)
console = Console()


def _settings(config: Optional[Path]) -> object:
    return load_settings(config)


def _alerts(settings) -> AlertSink:
    return AlertSink(
        webhook_url=settings.monitoring.webhook_url,
        enabled=settings.monitoring.alerts_enabled,
    )


@app.callback()
def main(
    ctx: typer.Context,
    config: Optional[Path] = typer.Option(
        None,
        "--config",
        "-c",
        help="Path to YAML config (default: config/default.yaml or TRADER_CONFIG)",
        envvar="TRADER_CONFIG",
    ),
) -> None:
    settings = load_settings(config)
    configure_logging(
        level=settings.logging.level, json_logs=settings.logging.json_logs
    )
    ctx.ensure_object(dict)
    ctx.obj["settings"] = settings
    ctx.obj["log"] = get_logger("trader")


@app.command("config-print")
def config_print(ctx: typer.Context) -> None:
    """Print resolved settings (env overrides applied)."""
    settings = ctx.obj["settings"]
    payload = settings.model_dump(mode="json")
    console.print(yaml.safe_dump(payload, sort_keys=False))


@app.command()
def health(ctx: typer.Context) -> None:
    """Check config, SQLite, mock/MT5 broker wiring, and optional Ollama reachability."""
    settings = ctx.obj["settings"]
    log = ctx.obj["log"]
    ok = True

    console.print(f"[bold]config[/bold]: {settings.config_path}")
    console.print(f"[bold]ollama.base_url[/bold]: {settings.ollama.base_url}")
    console.print(f"[bold]analyst_model[/bold]: {settings.ollama.analyst_model}")
    console.print(f"[bold]ollama.backend[/bold]: {settings.ollama.backend}")
    console.print(
        f"[bold]universe[/bold]: {settings.universe.symbols} @ {settings.universe.timeframes}"
    )
    console.print(f"[bold]broker.backend[/bold]: {settings.broker.backend}")
    console.print(f"[bold]broker.mode[/bold]: {settings.broker.mode}")
    console.print(f"[bold]account_mode[/bold]: {settings.mt5.account_mode}")
    console.print(f"[bold]allow_live[/bold]: {settings.execution.allow_live}")

    db = JournalDB(settings.database.path)
    applied = db.migrate()
    db.record_system_event("health_check", {"applied_migrations": applied})
    console.print(
        f"[bold]database[/bold]: {settings.database.path} "
        f"(migrations={applied or 'up-to-date'})"
    )

    broker = create_broker(settings)
    try:
        if settings.broker.backend == "mt5":
            console.print(
                "[yellow]broker[/yellow]: mt5 selected — connect requires Windows + "
                "MetaTrader5 + running terminal64.exe; skipping live connect in health "
                "(use mock in CI; trader execute-demo --backend mt5 on Windows)"
            )
        else:
            broker.connect()
            account = broker.account_state()
            db.record_account_snapshot(account)
            console.print(
                f"[bold]broker[/bold]: mock connected equity={account.equity} "
                f"mode={account.account_mode}"
            )
            broker.disconnect()
    except Exception as exc:  # noqa: BLE001 — surface health failure
        ok = False
        console.print(f"[red]broker[/red]: {exc}")
        log.exception("broker_health_failed")

    RiskEngine(settings.risk)
    console.print(
        "[bold]risk[/bold]: engine loaded "
        f"(max_pos={settings.risk.max_positions}, "
        f"daily_lock={settings.risk.daily_loss_lock_pct}, "
        "LLM cannot raise caps)"
    )

    control = ControlStore(db).get()
    HeartbeatStore(
        db,
        stale_after_seconds=settings.monitoring.heartbeat_stale_seconds,
    ).beat("trader", {"cmd": "health"})
    console.print(
        f"[bold]control[/bold]: kill={control.kill_switch} "
        f"halted={control.halted} blocked={control.new_entries_blocked}"
        + (f" reason={control.block_reason}" if control.new_entries_blocked else "")
    )
    console.print(
        f"[bold]scanner[/bold]: top_n={settings.scanner.top_n} "
        "(score ranks only — RiskEngine still must approve)"
    )
    console.print(
        f"[bold]llm[/bold]: analyst={settings.ollama.analyst_model} "
        f"fail_closed={settings.ollama.fail_closed_on_error} "
        "(risk_modifier may only reduce size)"
    )
    console.print(
        "[bold]execution[/bold]: demo path available "
        f"(allow_live={settings.execution.allow_live}; dry-run default)"
    )

    llm = create_llm(settings)
    reachable = llm.health()
    if reachable:
        console.print(f"[green]ollama[/green]: reachable at {settings.ollama.base_url}")
    else:
        console.print(
            f"[yellow]ollama[/yellow]: not reachable from this host "
            f"({settings.ollama.base_url}). Expected on Windows LAN; "
            "cloud CI should skip live Ollama."
        )

    db.record_system_event(
        "health_complete",
        {
            "ok": ok,
            "ollama_reachable": reachable,
            "broker": settings.broker.backend,
            "broker_mode": settings.broker.mode,
        },
    )
    db.close()

    if not ok:
        raise typer.Exit(code=1)
    console.print("[green]health ok[/green] (Phase 8 validation)")


@app.command("status")
def status(
    ctx: typer.Context,
    json_out: bool = typer.Option(False, "--json", help="Machine-readable JSON"),
    beat: bool = typer.Option(
        True,
        "--beat/--no-beat",
        help="Write a trader heartbeat while reading status",
    ),
) -> None:
    """Operator status: control flags, heartbeats, equity, recent events."""
    settings = ctx.obj["settings"]
    db = JournalDB(settings.database.path)
    db.migrate()
    if beat:
        HeartbeatStore(
            db,
            stale_after_seconds=settings.monitoring.heartbeat_stale_seconds,
        ).beat("trader", {"cmd": "status"})
    report = build_status_report(settings, db)
    db.record_system_event("status_check", {"healthy": report.healthy, "message": report.message})
    payload = report.to_dict()
    db.close()

    if json_out:
        console.print_json(data=payload)
    else:
        console.print(
            f"[bold]status[/bold] healthy={report.healthy} message={report.message}"
        )
        c = report.control
        console.print(
            f"control: kill={c.kill_switch} halted={c.halted} "
            f"blocked={c.new_entries_blocked} reason={c.block_reason or '-'}"
        )
        console.print(
            f"equity={report.last_equity} mode={report.last_account_mode} "
            f"open_positions={report.open_positions} orders={report.order_count}"
        )
        if report.heartbeats:
            for hb in report.heartbeats:
                console.print(
                    f"heartbeat {hb['component']}: age={hb['age_seconds']}s "
                    f"stale={hb['stale']}"
                )


@app.command()
def halt(
    ctx: typer.Context,
    reason: str = typer.Option("HALTED", "--reason", "-r", help="Halt reason code"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Halt new entries (fail-closed). Open positions are not closed by this command."""
    settings = ctx.obj["settings"]
    db = JournalDB(settings.database.path)
    db.migrate()
    state = ControlStore(db).halt(reason, source="operator")
    _alerts(settings).emit("warning", "HALTED", reason, state.to_dict())
    payload = state.to_dict()
    db.close()
    if json_out:
        console.print_json(data=payload)
    else:
        console.print(f"[yellow]halted[/yellow] reason={state.halt_reason}")
        console.print("[dim]New entries blocked. Resume with: trader resume[/dim]")


@app.command()
def resume(
    ctx: typer.Context,
    clear_kill: bool = typer.Option(
        False,
        "--clear-kill",
        help="Also clear the emergency kill switch",
    ),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Clear halt (optionally clear kill). Does not resume if kill remains set."""
    settings = ctx.obj["settings"]
    db = JournalDB(settings.database.path)
    db.migrate()
    store = ControlStore(db)
    state = store.resume(clear_kill=clear_kill, source="operator")
    # If kill still active, new entries remain blocked.
    if state.kill_switch and not clear_kill:
        console.print(
            "[yellow]halt cleared but kill switch still active[/yellow] — "
            "use trader resume --clear-kill or trader clear-kill"
        )
    _alerts(settings).emit("info", "RESUMED", "operator resume", state.to_dict())
    payload = state.to_dict()
    db.close()
    if json_out:
        console.print_json(data=payload)
    else:
        console.print(
            f"[green]resume[/green] kill={state.kill_switch} "
            f"halted={state.halted} blocked={state.new_entries_blocked}"
        )


@app.command()
def kill(
    ctx: typer.Context,
    reason: str = typer.Option(
        "KILL_SWITCH", "--reason", "-r", help="Kill reason code"
    ),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Emergency kill switch — blocks new entries until clear-kill / resume --clear-kill."""
    settings = ctx.obj["settings"]
    db = JournalDB(settings.database.path)
    db.migrate()
    state = ControlStore(db).kill(reason, source="operator")
    _alerts(settings).emit("critical", "KILL_SWITCH", reason, state.to_dict())
    payload = state.to_dict()
    db.close()
    if json_out:
        console.print_json(data=payload)
    else:
        console.print(f"[red]kill switch engaged[/red] reason={state.kill_reason}")
        console.print(
            "[dim]Clear with: trader resume --clear-kill  (or trader clear-kill + trader resume)[/dim]"
        )


@app.command("clear-kill")
def clear_kill_cmd(
    ctx: typer.Context,
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Clear kill switch only (halt may still be active)."""
    settings = ctx.obj["settings"]
    db = JournalDB(settings.database.path)
    db.migrate()
    state = ControlStore(db).clear_kill(source="operator")
    payload = state.to_dict()
    db.close()
    if json_out:
        console.print_json(data=payload)
    else:
        console.print(
            f"[green]kill cleared[/green] halted={state.halted} "
            f"blocked={state.new_entries_blocked}"
        )


@app.command("serve-control")
def serve_control(
    ctx: typer.Context,
    host: Optional[str] = typer.Option(
        None, "--host", help="Bind host (default monitoring.api_host)"
    ),
    port: Optional[int] = typer.Option(
        None, "--port", help="Bind port (default monitoring.api_port / 8787)"
    ),
) -> None:
    """Minimal local control HTTP API: /health /status /halt /resume /kill.

    Bind defaults to 127.0.0.1:8787. No auth — operator LAN / localhost only.
    """
    settings = ctx.obj["settings"]
    if not settings.monitoring.api_enabled:
        console.print("[red]monitoring.api_enabled=false[/red]")
        raise typer.Exit(code=1)
    bind_host = host or settings.monitoring.api_host
    bind_port = port if port is not None else settings.monitoring.api_port
    db = JournalDB(settings.database.path)
    db.migrate()
    db.record_system_event(
        "control_api_start",
        {"host": bind_host, "port": bind_port},
    )
    db.close()
    console.print(
        f"[bold]control API[/bold] http://{bind_host}:{bind_port} "
        "(GET /health /status ; POST /halt /resume /kill /clear-kill)"
    )
    console.print("[dim]Ctrl+C to stop. Kill/halt persist in SQLite.[/dim]")
    try:
        serve_control_api(settings, host=bind_host, port=bind_port, blocking=True)
    except KeyboardInterrupt:
        console.print("\n[yellow]control API stopped[/yellow]")


@app.command("ollama-tags")
def ollama_tags(ctx: typer.Context) -> None:
    """List Ollama models when URL is reachable; exit 2 if unreachable (skip in CI)."""
    settings = ctx.obj["settings"]
    llm = create_llm(settings, force_mock=False, backend="ollama")
    if not llm.health():
        console.print(
            f"[yellow]Ollama unreachable[/yellow] at {settings.ollama.base_url}. "
            "Cloud agents cannot reach 192.168.8.22 — run this on the Windows LAN host."
        )
        raise typer.Exit(code=2)
    models = llm.list_models()
    console.print(
        json.dumps({"base_url": settings.ollama.base_url, "models": models}, indent=2)
    )
    expected = {settings.ollama.analyst_model, settings.ollama.screen_model}
    missing = [m for m in expected if not any(m in name for name in models)]
    if missing:
        console.print(f"[yellow]missing expected models[/yellow]: {missing}")


@app.command("migrate")
def migrate(ctx: typer.Context) -> None:
    """Apply SQLite journal migrations."""
    settings = ctx.obj["settings"]
    db = JournalDB(settings.database.path)
    applied = db.migrate()
    db.record_system_event("migrate", {"applied": applied})
    db.close()
    console.print({"database": settings.database.path, "applied": applied})


@app.command("download-bars")
def download_bars(
    ctx: typer.Context,
    count: int = typer.Option(100, help="Bars per symbol/timeframe"),
    use_mock: bool = typer.Option(
        True,
        "--mock/--no-mock",
        help="Use synthetic market data (required in cloud CI)",
    ),
) -> None:
    """Download historical bars for the V1 universe into SQLite."""
    settings = ctx.obj["settings"]
    if not use_mock:
        console.print(
            "[yellow]Live MT5 bar download not implemented in Phase 1 — "
            "using mock data.[/yellow]"
        )
    db = JournalDB(settings.database.path)
    db.migrate()
    market = MockMarketData(symbols=list(settings.universe.symbols))
    downloader = HistoricalDownloader(market, db)
    written = downloader.download(
        list(settings.universe.symbols),
        list(settings.universe.timeframes),
        count=count,
    )
    total = db.count_bars()
    db.close()
    console.print({"bars_written": written, "bars_total": total})


@app.command("demo-roundtrip")
def demo_roundtrip(ctx: typer.Context) -> None:
    """Mock broker + mock LLM + risk stub round-trip (no live orders)."""
    from brokers.mock import MockBroker
    from trading.llm.mock import MockLlm
    from trading.types import Side, TradeCandidate

    settings = ctx.obj["settings"]
    db = JournalDB(settings.database.path)
    db.migrate()

    broker = MockBroker(account_mode=settings.mt5.account_mode)
    broker.connect()
    account = broker.account_state()

    market = MockMarketData(scenario="trend_pullback")
    bars = market.get_bars("EURUSD", Timeframe.M5, count=120)
    strategy = TrendPullbackStrategy(_strategy_cfg(settings))
    candidate = None
    for i in range(len(bars) - 1, 20, -1):
        candidate = strategy.evaluate("EURUSD", bars, at_index=i)
        if candidate is not None:
            break
    if candidate is None:
        candidate = TradeCandidate(
            symbol="EURUSD",
            strategy="trend_pullback_v1",
            direction="LONG",
            entry=1.1000,
            stop=1.0980,
            target=1.1040,
            risk_reward=2.0,
            regime="trend",
            setup_score=80,
        )
    cid = db.record_candidate(candidate)

    llm = MockLlm()
    analyst = llm.validate_candidate(candidate)
    db.record_llm_decision(analyst, candidate_id=cid)

    risk = RiskEngine(settings.risk)
    from datetime import timezone

    from trading.risk.context import RiskMarketContext, RiskPortfolioContext

    last_bar = bars[-1] if bars else None
    bar_time = last_bar.time if last_bar else None
    if bar_time is not None and bar_time.tzinfo is None:
        bar_time = bar_time.replace(tzinfo=timezone.utc)
    market_ctx = (
        RiskMarketContext(
            now=bar_time,
            bar_time=bar_time,
            spread=last_bar.spread if last_bar else 0.00012,
        )
        if bar_time is not None
        else None
    )
    portfolio = RiskPortfolioContext(
        day_start_equity=account.equity,
        week_start_equity=account.equity,
        consecutive_losses=0,
        open_positions=[],
    )
    decision = risk.validate_and_size(
        candidate,
        analyst,
        account,
        market=market_ctx,
        portfolio=portfolio,
    )
    db.record_risk_decision(decision, candidate_id=cid)

    result = None
    if decision.approved:
        from trading.types import OrderRequest

        order = OrderRequest(
            symbol=candidate.symbol,
            side=Side.BUY if candidate.direction == "LONG" else Side.SELL,
            volume=decision.volume,
            stop_loss=candidate.stop,
            take_profit=candidate.target,
            client_order_id=f"p5-demo-{cid}",
        )
        result = broker.submit_order(order)

    broker.disconnect()
    db.record_system_event(
        "demo_roundtrip",
        {
            "candidate_id": cid,
            "analyst": analyst.decision,
            "risk_approved": decision.approved,
            "order_status": result.status.value if result else None,
            "strategy": candidate.strategy,
        },
    )
    db.close()
    console.print(
        {
            "candidate": candidate.model_dump(),
            "analyst": analyst.model_dump(),
            "risk": decision.model_dump(),
            "order": result.model_dump() if result else None,
        }
    )


@app.command("execute-demo")
def execute_demo(
    ctx: typer.Context,
    symbol: str = typer.Option("EURUSD", "--symbol", "-s"),
    timeframe: str = typer.Option("M5", "--timeframe", "-t"),
    bars: int = typer.Option(120, "--bars", "-n"),
    submit: bool = typer.Option(
        False,
        "--submit/--dry-run",
        help="Actually submit to broker. Default is dry-run (no order sent).",
    ),
    confirm_demo: bool = typer.Option(
        False,
        "--confirm-demo",
        help="Required with --submit to acknowledge IC Markets DEMO account.",
    ),
    analyze: bool = typer.Option(
        True,
        "--analyze/--no-analyze",
        help="Run mock/config LLM analyst before risk (advisory).",
    ),
    live_llm: bool = typer.Option(
        False,
        "--live-llm/--mock-llm",
        help="With analyze: call real Ollama (LAN). Default mock is CI-safe.",
    ),
    backend: Optional[str] = typer.Option(
        None,
        "--backend",
        help="Override broker backend for this run: mock | mt5",
    ),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable JSON"),
) -> None:
    """Demo execution path: scan → strategy → risk → (optional) analyze → execute.

    Default is **dry-run** (plan only). To place a demo order on Windows::

        trader execute-demo --backend mt5 --submit --confirm-demo

    Never places live/real-money orders in P6 (``execution.allow_live=false``).
    """
    settings = ctx.obj["settings"]
    if timeframe not in ("M5", "M15"):
        console.print("[red]timeframe must be M5 or M15[/red]")
        raise typer.Exit(code=1)

    if backend is not None:
        if backend not in ("mock", "mt5"):
            console.print("[red]backend must be mock or mt5[/red]")
            raise typer.Exit(code=1)
        settings = settings.model_copy(
            update={"broker": settings.broker.model_copy(update={"backend": backend})}
        )

    if settings.broker.mode != "demo" and settings.mt5.account_mode != "demo":
        console.print(
            "[red]execute-demo refuses non-demo account_mode[/red]. "
            "Set broker.mode=demo / mt5.account_mode=demo."
        )
        raise typer.Exit(code=1)

    if submit and not confirm_demo:
        console.print(
            "[red]--submit requires --confirm-demo[/red] "
            "(acknowledge IC Markets DEMO account)."
        )
        raise typer.Exit(code=1)

    if submit and settings.execution.allow_live and settings.broker.mode == "live":
        console.print(
            "[red]Live trading path is out of scope for execute-demo (P9).[/red]"
        )
        raise typer.Exit(code=1)

    db = JournalDB(settings.database.path)
    db.migrate()
    broker = create_broker(settings)
    llm = None
    if analyze:
        llm = create_llm(settings, force_mock=not live_llm)
        if live_llm and not llm.health():
            console.print(
                f"[yellow]Ollama unreachable[/yellow] at {settings.ollama.base_url} — "
                "use --mock-llm or fix LAN."
            )
            raise typer.Exit(code=2)

    try:
        result = run_demo_pipeline(
            settings,
            broker=broker,
            db=db,
            llm=llm,
            symbol=symbol,
            timeframe=timeframe,
            bars=bars,
            submit=submit,
            confirm_demo=confirm_demo,
            analyze=analyze,
        )
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]execute-demo failed[/red]: {exc}")
        db.record_system_event("execute_demo_error", {"error": str(exc)})
        db.close()
        raise typer.Exit(code=1) from exc
    finally:
        if broker.is_connected():
            broker.disconnect()

    payload = result.to_dict()
    db.close()

    if json_out:
        console.print_json(data=payload)
    else:
        console.print(
            f"[bold]execute-demo[/bold] mode={result.mode} "
            f"backend={settings.broker.backend} account={result.account_mode}"
        )
        console.print(f"message: {result.message}")
        if result.execution is not None:
            order = result.execution.order
            console.print(
                f"order: dry_run={result.execution.dry_run} "
                f"submitted={result.execution.submitted} "
                f"status={order.status.value if order else None} "
                f"cid={result.execution.plan.client_order_id}"
            )
        console.print(
            "[dim]P6 demo only — no live/real-money path. "
            "RiskEngine is final authority; Qwen only reduces size.[/dim]"
        )

    # Non-zero if submit was requested but nothing was submitted successfully.
    if submit and (
        result.execution is None
        or not result.execution.submitted
        or (
            result.execution.order
            and result.execution.order.status.value == "REJECTED"
        )
    ):
        raise typer.Exit(code=1)


@app.command("analyze")
def analyze(
    ctx: typer.Context,
    symbol: str = typer.Option("EURUSD", "--symbol", "-s"),
    timeframe: str = typer.Option("M5", "--timeframe", "-t"),
    bars: int = typer.Option(120, "--bars", "-n"),
    live: bool = typer.Option(
        False,
        "--live/--mock",
        help="--live calls Ollama on LAN; default --mock is offline/CI-safe",
    ),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable JSON"),
) -> None:
    """Dry analyst call on a strategy candidate (no orders).

    Default uses MockLlm (cloud-safe). Pass ``--live`` on Windows LAN to hit
    Ollama at OLLAMA_BASE_URL / config default. Exit 2 if --live and unreachable.
    """
    settings = ctx.obj["settings"]
    if timeframe not in ("M5", "M15"):
        console.print("[red]timeframe must be M5 or M15[/red]")
        raise typer.Exit(code=1)

    from trading.types import TradeCandidate

    market = MockMarketData(scenario=settings.scanner.scenario)
    tf = Timeframe(timeframe)
    series = market.get_bars(symbol, tf, count=bars)
    strategy = TrendPullbackStrategy(_strategy_cfg(settings))
    candidate = None
    for i in range(len(series) - 1, 20, -1):
        candidate = strategy.evaluate(symbol, series, at_index=i)
        if candidate is not None:
            break
    if candidate is None:
        candidate = TradeCandidate(
            symbol=symbol,
            strategy=settings.strategy.name,
            direction="LONG",
            entry=1.1000,
            stop=1.0980,
            target=1.1040,
            risk_reward=2.0,
            regime="trend",
            setup_score=75,
        )

    llm = create_llm(settings, force_mock=not live)
    if live and not llm.health():
        console.print(
            f"[yellow]Ollama unreachable[/yellow] at {settings.ollama.base_url}. "
            "Skip --live without LAN; use default --mock for offline tests."
        )
        raise typer.Exit(code=2)

    market_context = {
        "symbol": symbol,
        "timeframe": timeframe,
        "bars": bars,
        "last_close": series[-1].close if series else None,
        "spread": series[-1].spread if series else None,
    }
    analyst = llm.validate_candidate(candidate, market_context=market_context)

    db = JournalDB(settings.database.path)
    db.migrate()
    cid = db.record_candidate(candidate)
    lid = db.record_llm_decision(analyst, candidate_id=cid)
    payload = {
        "mode": "live" if live else "mock",
        "base_url": settings.ollama.base_url,
        "analyst_model": settings.ollama.analyst_model,
        "candidate_id": cid,
        "llm_decision_id": lid,
        "candidate": candidate.model_dump(mode="json"),
        "analyst": analyst.model_dump(mode="json"),
        "note": (
            "Advisory only — RiskEngine must still approve; "
            "risk_modifier may only reduce size. No orders placed."
        ),
    }
    db.record_system_event("analyze_complete", payload)
    db.close()

    if json_out:
        console.print_json(data=payload)
    else:
        console.print(
            f"[bold]analyze[/bold] mode={'live' if live else 'mock'} "
            f"model={analyst.model} decision={analyst.decision} "
            f"modifier={analyst.risk_modifier} reason={analyst.reason_code}"
        )
        console.print(payload)
        console.print(
            "[dim]Fail-closed on timeout/bad JSON. "
            "RiskEngine remains boss. No live orders.[/dim]"
        )


@app.command("backtest")
def backtest(
    ctx: typer.Context,
    symbol: Optional[str] = typer.Option(
        None,
        "--symbol",
        "-s",
        help="Symbol (default: all universe symbols)",
    ),
    timeframe: str = typer.Option(
        "M5",
        "--timeframe",
        "-t",
        help="M5 or M15",
    ),
    bars: Optional[int] = typer.Option(
        None,
        "--bars",
        "-n",
        help="Synthetic bars to generate (default from config)",
    ),
    json_out: bool = typer.Option(
        False,
        "--json",
        help="Print machine-readable JSON only",
    ),
) -> None:
    """Run offline backtest with costs on synthetic trend_pullback bars.

    No live broker, no real-money orders. Cloud-safe.
    """
    settings = ctx.obj["settings"]
    if timeframe not in ("M5", "M15"):
        console.print("[red]timeframe must be M5 or M15[/red]")
        raise typer.Exit(code=1)

    symbols = [symbol] if symbol else list(settings.universe.symbols)
    count = bars or settings.backtest.default_bars
    tf = Timeframe(timeframe)

    strategy = TrendPullbackStrategy(_strategy_cfg(settings))
    costs = CostModel(
        CostConfig(
            commission_per_lot=settings.costs.commission_per_lot,
            slippage_pips=settings.costs.slippage_pips,
            swap_per_lot_per_day=settings.costs.swap_per_lot_per_day,
        )
    )
    risk_engine = None
    if settings.backtest.use_risk_engine:
        risk_engine = RiskEngine(settings.risk)
    engine = Backtester(
        strategy=strategy,
        costs=costs,
        config=BacktestConfig(
            volume=settings.backtest.volume,
            max_hold_bars=settings.backtest.max_hold_bars,
            one_position=settings.backtest.one_position,
            use_risk_engine=settings.backtest.use_risk_engine,
            starting_equity=settings.backtest.starting_equity,
        ),
        risk=risk_engine,
    )

    market = MockMarketData(
        symbols=symbols,
        scenario=settings.backtest.scenario,
    )
    reports = []
    for sym in symbols:
        series = market.get_bars(sym, tf, count=count)
        report = engine.run(sym, tf, series)
        reports.append(report)

    payload = {
        "strategy": strategy.name,
        "scenario": settings.backtest.scenario,
        "bars": count,
        "timeframe": timeframe,
        "use_risk_engine": settings.backtest.use_risk_engine,
        "reports": [r.to_dict() for r in reports],
    }

    db = JournalDB(settings.database.path)
    db.migrate()
    db.record_system_event("backtest_complete", payload)
    db.close()

    if json_out:
        console.print_json(data=payload)
    else:
        console.print(
            f"[bold]strategy[/bold]={strategy.name}  "
            f"[bold]scenario[/bold]={settings.backtest.scenario}  "
            f"[bold]bars[/bold]={count}  [bold]tf[/bold]={timeframe}"
        )
        for r in reports:
            console.print(r.to_dict())
        console.print(
            "[dim]Offline research only — not live performance. "
            "Risk engine sizes/rejects; Qwen cannot raise caps. No live orders.[/dim]"
        )


@app.command("scan")
def scan(
    ctx: typer.Context,
    symbol: Optional[str] = typer.Option(
        None,
        "--symbol",
        "-s",
        help="Limit to one symbol (default: full universe)",
    ),
    timeframe: Optional[str] = typer.Option(
        None,
        "--timeframe",
        "-t",
        help="M5, M15, or omit for both configured timeframes",
    ),
    bars: Optional[int] = typer.Option(
        None,
        "--bars",
        "-n",
        help="Synthetic bars per market (default: scanner.default_bars)",
    ),
    top_n: Optional[int] = typer.Option(
        None,
        "--top-n",
        help="Override scanner.top_n",
    ),
    lock_risk: bool = typer.Option(
        False,
        "--lock-risk",
        help="Simulate risk lock (all markets ineligible) — fail-closed demo",
    ),
    do_analyze: bool = typer.Option(
        False,
        "--analyze/--no-analyze",
        help="Run mock/config LLM on strategy candidates (advisory; no orders)",
    ),
    live_llm: bool = typer.Option(
        False,
        "--live-llm/--mock-llm",
        help="With --analyze: call real Ollama (LAN). Default mock is CI-safe.",
    ),
    json_out: bool = typer.Option(
        False,
        "--json",
        help="Print machine-readable JSON only",
    ),
) -> None:
    """Rank configured symbols on M5/M15 by opportunity score (offline mock).

    Score never alone trades — top-N may emit strategy candidates, but RiskEngine
    must still approve before any order path. No live broker orders.
    """
    settings = ctx.obj["settings"]
    if timeframe is not None and timeframe not in ("M5", "M15"):
        console.print("[red]timeframe must be M5 or M15[/red]")
        raise typer.Exit(code=1)

    symbols = [symbol] if symbol else list(settings.universe.symbols)
    timeframes = (
        [timeframe] if timeframe else list(settings.universe.timeframes)
    )
    count = bars or settings.scanner.default_bars
    run_analyze = do_analyze or settings.scanner.analyze_candidates

    scanner_settings = settings.scanner.model_copy(
        update={"top_n": top_n} if top_n is not None else {}
    )
    risk = RiskEngine(settings.risk)
    if lock_risk:
        risk.lock_new_trades("CLI_LOCK_RISK_DEMO")

    market = MockMarketData(
        symbols=symbols,
        scenario=settings.scanner.scenario,
    )
    from datetime import datetime, timedelta, timezone

    as_of = datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)
    snapshots: list[MarketSnapshot] = []
    for sym in symbols:
        for tf_name in timeframes:
            tf = Timeframe(tf_name)
            series = market.get_bars(sym, tf, count=count)
            step = timedelta(minutes=5 if tf == Timeframe.M5 else 15)
            start = as_of - step * (len(series) - 1)
            anchored = [
                b.model_copy(update={"time": start + step * i})
                for i, b in enumerate(series)
            ]
            snapshots.append(MarketSnapshot(symbol=sym, timeframe=tf, bars=anchored))

    scanner = MarketScanner(
        ScannerConfig(
            settings=scanner_settings,
            strategy=settings.strategy,
            evaluate_strategy=settings.scanner.evaluate_strategy,
            correlation_groups=list(settings.risk.correlation_groups),
            pip_size=dict(settings.risk.pip_size),
        ),
        risk_engine=risk,
    )
    report = scanner.rank(
        snapshots,
        context=EligibilityContext(
            now=as_of,
            broker_available=True,
            api_healthy=True,
            market_open=True,
            risk_engine=risk,
        ),
    )

    db = JournalDB(settings.database.path)
    db.migrate()
    score_ids = db.record_scan_report(report)
    candidate_ids: list[int] = []
    candidates = []
    for row in report.selected:
        if row.candidate is not None:
            candidate_ids.append(db.record_candidate(row.candidate))
            candidates.append(row.candidate)

    analyzed_payload = None
    if run_analyze and candidates:
        llm = create_llm(settings, force_mock=not live_llm)
        if live_llm and not llm.health():
            console.print(
                f"[yellow]Ollama unreachable[/yellow] at {settings.ollama.base_url} — "
                "skipping live analyze; use --mock-llm."
            )
            raise typer.Exit(code=2)
        analyzed = analyze_candidates(llm, candidates)
        llm_ids = []
        for analyzed_row, cid in zip(analyzed, candidate_ids, strict=False):
            llm_ids.append(
                db.record_llm_decision(analyzed_row.analyst, candidate_id=cid)
            )
        analyzed_payload = {
            "mode": "live" if live_llm else "mock",
            "llm_decision_ids": llm_ids,
            "results": [a.to_dict() for a in analyzed],
        }

    payload = report.to_dict()
    payload["score_row_ids"] = score_ids
    payload["candidate_ids"] = candidate_ids
    if analyzed_payload is not None:
        payload["analyst"] = analyzed_payload
    db.record_system_event("scan_complete", payload)
    db.close()

    if json_out:
        console.print_json(data=payload)
    else:
        console.print(
            f"[bold]scanner[/bold] top_n={report.top_n}  "
            f"symbols={symbols}  tfs={timeframes}  bars={count}"
        )
        if report.risk_locked:
            console.print(
                f"[yellow]risk locked[/yellow]: {report.risk_lock_reason} "
                "(all markets ineligible — fail closed)"
            )
        console.print("[bold]selected (top-N)[/bold]:")
        if not report.selected:
            console.print("  (none)")
        for row in report.selected:
            cand = (
                f" candidate={row.candidate.direction}@{row.candidate.setup_score}"
                if row.candidate
                else " candidate=none"
            )
            console.print(
                f"  #{row.rank} {row.symbol} {row.timeframe.value} "
                f"score={row.opportunity_score:.2f} regime={row.regime}{cand}"
            )
        if analyzed_payload is not None:
            console.print("[bold]analyst (advisory)[/bold]:")
            for item in analyzed_payload["results"]:
                a = item["analyst"]
                console.print(
                    f"  {item['candidate']['symbol']} → {a['decision']} "
                    f"modifier={a['risk_modifier']} reason={a['reason_code']}"
                )
        console.print("[bold]all markets[/bold]:")
        for row in report.markets:
            flag = "OK" if row.eligible else row.reason_code
            console.print(
                f"  {row.symbol} {row.timeframe.value} "
                f"eligible={row.eligible} ({flag}) score={row.opportunity_score:.2f}"
            )
        console.print(f"[dim]{report.note} No live orders.[/dim]")


@app.command("walk-forward")
def walk_forward_cmd(
    ctx: typer.Context,
    symbol: str = typer.Option("EURUSD", "--symbol", "-s"),
    timeframe: str = typer.Option("M5", "--timeframe", "-t"),
    bars: Optional[int] = typer.Option(
        None, "--bars", "-n", help="Synthetic bars (default validation.default_bars)"
    ),
    train_bars: Optional[int] = typer.Option(None, "--train-bars"),
    test_bars: Optional[int] = typer.Option(None, "--test-bars"),
    step_bars: Optional[int] = typer.Option(None, "--step-bars"),
    mode: Optional[str] = typer.Option(None, "--mode", help="rolling | expanding"),
    write_report: bool = typer.Option(
        False,
        "--write-report/--no-write-report",
        help="Also write a partial DoD JSON/MD under reports/",
    ),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Chronological walk-forward / out-of-sample backtest (offline)."""
    settings = ctx.obj["settings"]
    if timeframe not in ("M5", "M15"):
        console.print("[red]timeframe must be M5 or M15[/red]")
        raise typer.Exit(code=1)
    v = settings.validation
    wf_mode = mode or v.mode
    if wf_mode not in ("rolling", "expanding"):
        console.print("[red]mode must be rolling or expanding[/red]")
        raise typer.Exit(code=1)

    count = bars or v.default_bars
    market = MockMarketData(scenario=v.scenario, seed=42)
    series = market.get_bars(symbol, Timeframe(timeframe), count=count)
    costs = CostModel(
        CostConfig(
            commission_per_lot=settings.costs.commission_per_lot,
            slippage_pips=settings.costs.slippage_pips,
            swap_per_lot_per_day=settings.costs.swap_per_lot_per_day,
        )
    )
    risk = RiskEngine(settings.risk) if settings.backtest.use_risk_engine else None
    report = run_walk_forward(
        symbol,
        timeframe,
        series,
        strategy_config=_strategy_cfg(settings),
        cost_config=costs.config,
        backtest_config=BacktestConfig(
            volume=settings.backtest.volume,
            max_hold_bars=settings.backtest.max_hold_bars,
            one_position=settings.backtest.one_position,
            use_risk_engine=settings.backtest.use_risk_engine,
            starting_equity=settings.backtest.starting_equity,
        ),
        walk_config=WalkForwardConfig(
            train_bars=train_bars or v.train_bars,
            test_bars=test_bars or v.test_bars,
            step_bars=step_bars or v.step_bars,
            mode=wf_mode,  # type: ignore[arg-type]
            min_folds=v.min_folds,
            min_oos_trades=v.min_oos_trades,
            max_oos_drawdown=v.max_oos_drawdown,
            require_non_negative_expectancy=v.require_non_negative_expectancy,
        ),
        risk=risk,
    )
    payload = report.to_dict()

    db = JournalDB(settings.database.path)
    db.migrate()
    db.record_system_event("walk_forward_complete", payload)
    db.close()

    if write_report:
        dod = build_dod_report(walk_forward=report)
        jp, mp = write_dod_report(
            dod, output_dir=v.report_dir, stem="p8-walk-forward"
        )
        payload["report_json"] = str(jp)
        payload["report_md"] = str(mp)

    if json_out:
        console.print_json(data=payload)
    else:
        mark = "[green]PASS[/green]" if report.passed else "[red]FAIL[/red]"
        console.print(
            f"[bold]walk-forward[/bold] {mark} folds={len(report.folds)} "
            f"oos_trades={report.oos_trades} expectancy={report.oos_expectancy:.4f} "
            f"dd={report.oos_max_drawdown:.4f}"
        )
        if report.fail_reasons:
            console.print(f"fail_reasons: {report.fail_reasons}")
        console.print(f"[dim]{report.note}[/dim]")

    if not report.passed:
        raise typer.Exit(code=1)


@app.command("stress")
def stress_cmd(
    ctx: typer.Context,
    symbol: str = typer.Option("EURUSD", "--symbol", "-s"),
    timeframe: str = typer.Option("M5", "--timeframe", "-t"),
    bars: Optional[int] = typer.Option(None, "--bars", "-n"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Run offline stress scenarios (spread, slippage, gap, consecutive losses)."""
    settings = ctx.obj["settings"]
    if timeframe not in ("M5", "M15"):
        console.print("[red]timeframe must be M5 or M15[/red]")
        raise typer.Exit(code=1)
    v = settings.validation
    count = bars or v.default_bars
    series = MockMarketData(scenario=v.scenario, seed=42).get_bars(
        symbol, Timeframe(timeframe), count=count
    )
    report = run_stress_suite(
        symbol,
        timeframe,
        series,
        risk_settings=settings.risk.model_copy(
            update={"max_spread_pips": 50.0, "max_data_age_seconds": 0}
        ),
        base_cost=CostConfig(
            commission_per_lot=settings.costs.commission_per_lot,
            slippage_pips=settings.costs.slippage_pips,
            swap_per_lot_per_day=settings.costs.swap_per_lot_per_day,
        ),
        backtest_config=BacktestConfig(
            volume=settings.backtest.volume,
            max_hold_bars=settings.backtest.max_hold_bars,
            one_position=settings.backtest.one_position,
            use_risk_engine=True,
            starting_equity=settings.backtest.starting_equity,
        ),
        spread_mult=v.spread_shock_mult,
        slippage_pips=v.slippage_shock_pips,
    )
    payload = report.to_dict()
    db = JournalDB(settings.database.path)
    db.migrate()
    db.record_system_event("stress_complete", payload)
    db.close()

    if json_out:
        console.print_json(data=payload)
    else:
        mark = "[green]PASS[/green]" if report.passed else "[red]FAIL[/red]"
        console.print(f"[bold]stress[/bold] {mark}")
        for s in report.scenarios:
            sm = "PASS" if s.passed else "FAIL"
            console.print(f"  {s.name}: {sm} {s.fail_reasons or ''}")
        console.print(f"[dim]{report.note}[/dim]")

    if not report.passed:
        raise typer.Exit(code=1)


@app.command("soak")
def soak_cmd(
    ctx: typer.Context,
    ticks: Optional[int] = typer.Option(None, "--ticks", help="Accelerated loop ticks"),
    symbol: str = typer.Option("EURUSD", "--symbol", "-s"),
    timeframe: str = typer.Option("M5", "--timeframe", "-t"),
    db_path: Optional[Path] = typer.Option(
        None,
        "--db",
        help="SQLite path for soak control/heartbeats (default: data/soak.db)",
    ),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Accelerated offline soak: heartbeats + halt/kill/resume (not multi-day wall clock)."""
    settings = ctx.obj["settings"]
    if timeframe not in ("M5", "M15"):
        console.print("[red]timeframe must be M5 or M15[/red]")
        raise typer.Exit(code=1)
    v = settings.validation
    path = db_path or Path("data/soak.db")
    n_ticks = ticks or v.soak_ticks
    report = run_soak(
        db_path=path,
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
    payload = report.to_dict()
    payload["db"] = str(path)

    # Journal into primary DB as well for operator trail.
    db = JournalDB(settings.database.path)
    db.migrate()
    db.record_system_event("soak_complete", payload)
    db.close()

    if json_out:
        console.print_json(data=payload)
    else:
        mark = "[green]PASS[/green]" if report.passed else "[red]FAIL[/red]"
        console.print(
            f"[bold]soak[/bold] {mark} ticks={report.ticks} "
            f"blocks={report.control_blocks} hb={report.heartbeats} "
            f"halt={report.halt_injected} kill={report.kill_injected} "
            f"resume={report.resume_verified}"
        )
        if report.fail_reasons:
            console.print(f"fail_reasons: {report.fail_reasons}")
        console.print(f"[dim]{report.note}[/dim]")

    if not report.passed:
        raise typer.Exit(code=1)


@app.command("validate")
def validate_cmd(
    ctx: typer.Context,
    symbol: str = typer.Option("EURUSD", "--symbol", "-s"),
    timeframe: str = typer.Option("M5", "--timeframe", "-t"),
    bars: Optional[int] = typer.Option(None, "--bars", "-n"),
    ticks: Optional[int] = typer.Option(None, "--ticks"),
    output_dir: Optional[Path] = typer.Option(
        None, "--output-dir", "-o", help="Report directory (default reports/)"
    ),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Run walk-forward + stress + soak and write DoD pass/fail artifacts.

    Offline only. Use before tiny live (P9); LAN demo soak still required on Windows.
    """
    settings = ctx.obj["settings"]
    if timeframe not in ("M5", "M15"):
        console.print("[red]timeframe must be M5 or M15[/red]")
        raise typer.Exit(code=1)
    v = settings.validation
    count = bars or v.default_bars
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
    n_ticks = ticks or v.soak_ticks
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

    db = JournalDB(settings.database.path)
    db.migrate()
    db.record_system_event("validate_complete", payload)
    db.close()

    if json_out:
        console.print_json(data=payload)
    else:
        mark = "[green]PASS[/green]" if dod.overall_passed else "[red]FAIL[/red]"
        console.print(f"[bold]validate[/bold] {mark}")
        console.print(f"wrote {jp}")
        console.print(f"wrote {mp}")
        console.print(
            "[dim]Offline P8 slice only — LAN demo soak + §9 LAN checks still required "
            "before tiny live (P9).[/dim]"
        )

    if not dod.overall_passed:
        raise typer.Exit(code=1)


def _strategy_cfg(settings: object) -> TrendPullbackConfig:
    s = settings.strategy  # type: ignore[attr-defined]
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


if __name__ == "__main__":
    app()
