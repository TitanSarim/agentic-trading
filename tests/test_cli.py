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

    demo = runner.invoke(app, ["demo-roundtrip"])
    assert demo.exit_code == 0
    assert "APPROVE" in demo.stdout or "approved" in demo.stdout.lower()

    tags = runner.invoke(app, ["ollama-tags"])
    assert tags.exit_code == 2  # unreachable — skippable in CI
