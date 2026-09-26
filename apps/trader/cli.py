"""Minimal trader CLI — health, config-print, migrate, download-bars, ollama-tags.

No live order placement in Phase 1.
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
from trading.config import load_settings
from trading.data.historical import HistoricalDownloader
from trading.data.mock import MockMarketData
from trading.journal.db import JournalDB
from trading.llm.factory import create_llm
from trading.logging_setup import configure_logging, get_logger
from trading.risk.engine import RiskEngine

app = typer.Typer(
    name="trader",
    help="Agentic trading foundation CLI (Phase 1). No live trades.",
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
    console.print("[green]health ok[/green] (Phase 1 foundation)")


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

    candidate = TradeCandidate(
        symbol="EURUSD",
        strategy="stub_v0",
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
            client_order_id=f"p1-demo-{cid}",
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
        },
    )
    db.close()
    console.print(
        {
            "analyst": analyst.model_dump(),
            "risk": decision.model_dump(),
            "order": result.model_dump() if result else None,
        }
    )


if __name__ == "__main__":
    app()
