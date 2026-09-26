"""Demo validation checklist tests (offline / cloud-safe)."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from apps.trader.cli import app
from research.demo_validation import (
    probe_ollama,
    run_analyze_mock,
    run_demo_validation,
    run_execute_demo_mock,
    write_demo_validation_report,
)
from trading.config import load_settings

runner = CliRunner()


def test_probe_ollama_blocked_on_dead_port() -> None:
    result = probe_ollama("http://127.0.0.1:9", connect_timeout=0.2, read_timeout=0.5)
    assert result.status == "BLOCKED"
    assert result.scope == "lan"
    assert "127.0.0.1" in result.detail["base_url"]


def test_analyze_and_execute_mock(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRADER_DB_PATH", str(tmp_path / "demo.db"))
    monkeypatch.setenv("TRADER_BROKER_BACKEND", "mock")
    settings = load_settings()
    a = run_analyze_mock(settings)
    assert a.status == "PASS"
    assert a.detail["decision"] in ("APPROVE", "REJECT")
    e = run_execute_demo_mock(settings)
    assert e.status == "PASS"
    assert e.detail["mode"] == "dry-run"


def test_run_demo_validation_offline(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRADER_DB_PATH", str(tmp_path / "demo.db"))
    monkeypatch.setenv("TRADER_BROKER_BACKEND", "mock")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:9")
    settings = load_settings()
    out = tmp_path / "reports"
    report = run_demo_validation(
        settings,
        bars=400,
        ticks=20,
        run_tests=False,
        probe_lan=True,
        output_dir=out,
    )
    assert report.offline_passed
    assert report.lan_reachable is False
    assert report.mt5_available is False
    statuses = {c.id: c.status for c in report.checks}
    assert statuses["analyze_mock"] == "PASS"
    assert statuses["execute_demo_mock"] == "PASS"
    assert statuses["offline_validate"] == "PASS"
    assert statuses["ollama_tags"] == "BLOCKED"
    assert statuses["execute_demo_mt5"] == "BLOCKED"
    jp, mp = write_demo_validation_report(report, out)
    assert jp.exists() and mp.exists()
    assert "BLOCKED" in mp.read_text(encoding="utf-8")


def test_validate_demo_cli(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRADER_DB_PATH", str(tmp_path / "cli.db"))
    monkeypatch.setenv("TRADER_BROKER_BACKEND", "mock")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:9")
    out = tmp_path / "reports"
    result = runner.invoke(
        app,
        [
            "validate-demo",
            "--no-pytest",
            "--bars",
            "400",
            "--ticks",
            "20",
            "--output-dir",
            str(out),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "offline_passed" in result.stdout
    assert (out / "demo-validation.json").exists()
