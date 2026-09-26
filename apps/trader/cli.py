"""Trader CLI — health, migrate, download-bars, backtest, scan, analyze, ollama-tags.

No live order placement. Backtests/scans/analyze-mock are offline (cloud-safe).
Live Ollama calls are optional and skippable without LAN.
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
from trading.config import load_settings
from trading.data.historical import HistoricalDownloader
from trading.data.mock import MockMarketData
from trading.journal.db import JournalDB
from trading.llm.factory import create_llm
from trading.llm.pipeline import analyze_candidates
from trading.logging_setup import configure_logging, get_logger
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
    help="Agentic trading CLI (Phase 5 Qwen/Ollama). No live trades.",
    add_completion=False,
)
console = Console()


def _settings(config: Optional[Path]) -> object:
    return load_settings(config)


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
    console.print(f"[bold]account_mode[/bold]: {settings.mt5.account_mode}")

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
                "MetaTrader5; skipping live connect in health (use mock in CI)"
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
    console.print(
        f"[bold]scanner[/bold]: top_n={settings.scanner.top_n} "
        "(score ranks only — RiskEngine still must approve)"
    )
    console.print(
        f"[bold]llm[/bold]: analyst={settings.ollama.analyst_model} "
        f"fail_closed={settings.ollama.fail_closed_on_error} "
        "(risk_modifier may only reduce size)"
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
        {"ok": ok, "ollama_reachable": reachable, "broker": settings.broker.backend},
    )
    db.close()

    if not ok:
        raise typer.Exit(code=1)
    console.print("[green]health ok[/green] (Phase 5 Qwen/Ollama)")


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
