"""CLI smoke tests (no network)."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from apps.trader.cli import app

runner = CliRunner()


def test_config_print() -> None:
    result = runner.invoke(app, ["config-print"])
    assert result.exit_code == 0
    assert "qwen3.8:27b" in result.stdout
    assert "192.168.8.22" in result.stdout


def test_health_and_demo_roundtrip(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "cli.db"
    monkeypatch.setenv("TRADER_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADER_BROKER_BACKEND", "mock")
    # Force unreachable Ollama so health does not depend on LAN.
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:9")

    health = runner.invoke(app, ["health"])
    assert health.exit_code == 0
    assert "health ok" in health.stdout
    assert "Phase 8" in health.stdout

    demo = runner.invoke(app, ["demo-roundtrip"])
    assert demo.exit_code == 0
    assert "APPROVE" in demo.stdout or "approved" in demo.stdout.lower()

    tags = runner.invoke(app, ["ollama-tags"])
    assert tags.exit_code == 2  # unreachable — skippable in CI

    bt = runner.invoke(
        app,
        ["backtest", "--symbol", "EURUSD", "--timeframe", "M5", "--bars", "250", "--json"],
    )
    assert bt.exit_code == 0
    assert "trend_pullback_v1" in bt.stdout
    assert "expectancy" in bt.stdout

    scan = runner.invoke(
        app,
        ["scan", "--symbol", "EURUSD", "--timeframe", "M5", "--bars", "120", "--json"],
    )
    assert scan.exit_code == 0
    assert "opportunity_score" in scan.stdout
    assert "never opens a trade" in scan.stdout.lower() or "RiskEngine" in scan.stdout

    analyze = runner.invoke(
        app,
        ["analyze", "--mock", "--symbol", "EURUSD", "--timeframe", "M5", "--json"],
    )
    assert analyze.exit_code == 0
    assert "analyst" in analyze.stdout
    assert "risk_modifier" in analyze.stdout

    scan_analyze = runner.invoke(
        app,
        [
            "scan",
            "--symbol",
            "EURUSD",
            "--timeframe",
            "M5",
            "--bars",
            "120",
            "--analyze",
            "--mock-llm",
            "--json",
        ],
    )
    assert scan_analyze.exit_code == 0

    dry = runner.invoke(
        app,
        [
            "execute-demo",
            "--backend",
            "mock",
            "--dry-run",
            "--mock-llm",
            "--symbol",
            "EURUSD",
            "--timeframe",
            "M5",
            "--json",
        ],
    )
    assert dry.exit_code == 0
    assert "dry-run" in dry.stdout

    # --submit without --confirm-demo must fail
    bad = runner.invoke(
        app,
        ["execute-demo", "--backend", "mock", "--submit", "--mock-llm", "--json"],
    )
    assert bad.exit_code == 1

    submit = runner.invoke(
        app,
        [
            "execute-demo",
            "--backend",
            "mock",
            "--submit",
            "--confirm-demo",
            "--mock-llm",
            "--symbol",
            "EURUSD",
            "--timeframe",
            "M5",
            "--json",
        ],
    )
    assert submit.exit_code == 0
    assert "submit" in submit.stdout or "FILLED" in submit.stdout
