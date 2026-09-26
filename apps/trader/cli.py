"""Trader CLI — health, migrate, download-bars, backtest, ollama-tags.

No live order placement. Backtests are offline (mock/sample bars only).
"""

from __future__ import annotations

import json
import sys
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
from trading.logging_setup import configure_logging, get_logger
from trading.risk.engine import RiskEngine
from trading.strategies.trend_pullback import TrendPullbackConfig, TrendPullbackStrategy
from trading.types import Timeframe

app = typer.Typer(
    name="trader",
    help="Agentic trading CLI (Phase 2 research). No live trades.",
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
    console.print(f"[bold]universe[/bold]: {settings.universe.symbols} @ {settings.universe.timeframes}")
    console.print(f"[bold]broker.backend[/bold]: {settings.broker.backend}")
    console.print(f"[bold]account_mode[/bold]: {settings.mt5.account_mode}")

    # DB migrate + event
    db = JournalDB(settings.database.path)
    applied = db.migrate()
    db.record_system_event("health_check", {"applied_migrations": applied})
    console.print(f"[bold]database[/bold]: {settings.database.path} (migrations={applied or 'up-to-date'})")

    # Broker
    broker = create_broker(settings)
    try:
        if settings.broker.backend == "mt5":
            console.print(
                "[yellow]broker[/yellow]: mt5 selected — connect requires Windows + MetaTrader5; "
                "skipping live connect in health (use mock in CI)"
            )
        else:
            broker.connect()
            account = broker.account_state()
            db.record_account_snapshot(account)
            console.print(
                f"[bold]broker[/bold]: mock connected equity={account.equity} mode={account.account_mode}"
            )
            broker.disconnect()
    except Exception as exc:  # noqa: BLE001 — surface health failure
        ok = False
        console.print(f"[red]broker[/red]: {exc}")
        log.exception("broker_health_failed")

    # Risk stub boots
    RiskEngine(settings.risk)
    console.print("[bold]risk[/bold]: engine stub loaded (boss; LLM cannot raise caps)")

    # Ollama optional — fail soft in health (cloud cannot reach .22)
    llm = create_llm(settings)
    reachable = llm.health()
    if reachable:
        console.print(f"[green]ollama[/green]: reachable at {settings.ollama.base_url}")
    else:
        console.print(
            f"[yellow]ollama[/yellow]: not reachable from this host ({settings.ollama.base_url}). "
            "Expected on Windows LAN; cloud CI should skip live Ollama."
        )

    db.record_system_event(
        "health_complete",
        {"ok": ok, "ollama_reachable": reachable, "broker": settings.broker.backend},
    )
    db.close()

    if not ok:
        raise typer.Exit(code=1)
    console.print("[green]health ok[/green] (Phase 2 research foundation)")


@app.command("ollama-tags")
def ollama_tags(ctx: typer.Context) -> None:
    """List Ollama models when URL is reachable; exit 2 if unreachable (skip in CI)."""
    settings = ctx.obj["settings"]
    llm = create_llm(settings)
    if not llm.health():
        console.print(
            f"[yellow]Ollama unreachable[/yellow] at {settings.ollama.base_url}. "
            "Cloud agents cannot reach 192.168.8.22 — run this on the Windows LAN host."
        )
        raise typer.Exit(code=2)
    models = llm.list_models()
    console.print(json.dumps({"base_url": settings.ollama.base_url, "models": models}, indent=2))
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
            "[yellow]Live MT5 bar download not implemented in Phase 1 — using mock data.[/yellow]"
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
    from trading.types import Side, TradeCandidate
    from brokers.mock import MockBroker
    from trading.llm.mock import MockLlm

    settings = ctx.obj["settings"]
    db = JournalDB(settings.database.path)
    db.migrate()

    broker = MockBroker(account_mode=settings.mt5.account_mode)
    broker.connect()
    account = broker.account_state()

    # Prefer a real strategy candidate when synthetic bars allow; else stub.
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
    decision = risk.validate_and_size(candidate, analyst, account)
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
            client_order_id=f"p2-demo-{cid}",
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
    engine = Backtester(
        strategy=strategy,
        costs=costs,
        config=BacktestConfig(
            volume=settings.backtest.volume,
            max_hold_bars=settings.backtest.max_hold_bars,
            one_position=settings.backtest.one_position,
        ),
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
            "Risk engine / Qwen boundaries unchanged.[/dim]"
        )


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
