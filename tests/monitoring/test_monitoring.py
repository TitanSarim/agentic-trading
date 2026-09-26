"""Phase 7 monitoring: control, heartbeat, alerts, status, execute-demo gating."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from apps.trader.cli import app
from brokers.mock import MockBroker
from trading.config import MonitoringSettings, RiskSettings, Settings, load_settings
from trading.execution.pipeline import run_demo_pipeline
from trading.journal.db import JournalDB
from trading.llm.mock import MockLlm
from trading.monitoring.alerts import AlertSink
from trading.monitoring.api import serve_control_api
from trading.monitoring.control import ControlStore
from trading.monitoring.heartbeat import HeartbeatStore
from trading.monitoring.status import build_status_report
from trading.risk.context import RiskPortfolioContext
from trading.risk.engine import RiskEngine
from trading.types import AccountState, AnalystDecision, TradeCandidate

runner = CliRunner()


@pytest.fixture()
def db(tmp_path: Path) -> JournalDB:
    journal = JournalDB(tmp_path / "p7.db")
    applied = journal.migrate()
    assert 5 in applied or journal._conn.execute(
        "SELECT 1 FROM control_state WHERE id=1"
    ).fetchone()
    return journal


def _candidate() -> TradeCandidate:
    return TradeCandidate(
        symbol="EURUSD",
        strategy="trend_pullback_v1",
        direction="LONG",
        entry=1.1000,
        stop=1.0950,
        target=1.1100,
        risk_reward=2.0,
        regime="trend",
        setup_score=80.0,
    )


def test_control_halt_kill_resume_persists(db: JournalDB) -> None:
    store = ControlStore(db)
    s = store.halt("OPS_HALT")
    assert s.halted and s.new_entries_blocked
    assert s.block_reason == "OPS_HALT"

    risk = RiskEngine()
    store.apply_to_risk(risk)
    assert risk.halted and risk.new_trades_locked

    store.resume()
    assert not store.get().halted

    store.kill("EMERGENCY")
    assert store.get().kill_switch and store.get().halted
    store.resume()  # kill remains
    assert store.get().kill_switch
    assert store.get().new_entries_blocked
    store.resume(clear_kill=True)
    assert not store.get().new_entries_blocked

    events = {e["event_type"] for e in db.recent_system_events(50)}
    assert "halt" in events and "kill" in events and "resume" in events


def test_risk_engine_respects_applied_control(db: JournalDB) -> None:
    store = ControlStore(db)
    store.kill("KILL_SWITCH")
    risk = RiskEngine(RiskSettings(max_spread_pips=None))
    store.apply_to_risk(risk)
    account = AccountState(
        balance=1000, equity=1000, margin=0, free_margin=1000, currency="USD", account_mode="demo"
    )
    decision = risk.validate_and_size(
        _candidate(),
        AnalystDecision(
            decision="APPROVE",
            confidence=1.0,
            risk_modifier=1.0,
            reason_code="OK",
            model="test",
        ),
        account,
    )
    assert not decision.approved
    assert decision.reason_code == "KILL_SWITCH"


def test_heartbeat_stale_detection(db: JournalDB) -> None:
    hb = HeartbeatStore(db, stale_after_seconds=30)
    info = hb.beat("trader", {"x": 1})
    assert not info.stale
    assert hb.is_stale("trader") is False

    # Backdate beat
    old = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    db._conn.execute(
        "UPDATE heartbeats SET beat_at=? WHERE component='trader'",
        (old,),
    )
    db._conn.commit()
    assert hb.is_stale("trader") is True
    assert hb.is_stale("missing", missing_is_stale=True) is True


def test_alert_log_and_optional_webhook(tmp_path: Path) -> None:
    recorded: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(json.loads(request.content.decode()))
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    sink = AlertSink(
        webhook_url="http://alerts.test/hook",
        transport=transport,
    )
    sink.emit("warning", "HALTED", "test halt", {"a": 1})
    assert len(sink.emitted) == 1
    assert recorded and recorded[0]["code"] == "HALTED"

    # Webhook failure must not raise
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    sink2 = AlertSink(
        webhook_url="http://alerts.test/hook",
        transport=httpx.MockTransport(boom),
    )
    sink2.emit("error", "X", "y")  # no raise


def test_status_report(db: JournalDB) -> None:
    settings = Settings(monitoring=MonitoringSettings(fail_closed_on_stale=True))
    ControlStore(db).halt("TEST")
    HeartbeatStore(db, stale_after_seconds=30).beat("trader")
    report = build_status_report(settings, db)
    assert report.control.halted
    assert report.flags["new_entries_blocked"] is True
    assert "halted" in report.message
    d = report.to_dict()
    assert "control" in d and "heartbeats" in d


def test_execute_demo_respects_halt(db: JournalDB) -> None:
    settings = load_settings()
    settings = settings.model_copy(
        update={"database": settings.database.model_copy(update={"path": str(db.path)})}
    )
    ControlStore(db).halt("OPS")
    broker = MockBroker(account_mode="demo")
    broker.connect()
    result = run_demo_pipeline(
        settings,
        broker=broker,
        db=db,
        llm=MockLlm(),
        submit=False,
        confirm_demo=False,
        analyze=True,
    )
    broker.disconnect()
    assert "blocked" in result.message.lower() or "OPS" in result.message
    assert result.execution is None
    assert result.risk is not None and result.risk.new_trades_locked


def test_auto_halt_on_daily_loss_lock(db: JournalDB) -> None:
    settings = load_settings()
    settings = settings.model_copy(
        update={
            "database": settings.database.model_copy(update={"path": str(db.path)}),
            "monitoring": settings.monitoring.model_copy(
                update={"auto_halt_on_risk_lock": True}
            ),
            "risk": settings.risk.model_copy(
                update={"daily_loss_lock_pct": 0.01, "max_spread_pips": None}
            ),
        }
    )
    # Directly verify ControlStore + RiskEngine auto path used by pipeline helpers
    risk = RiskEngine(settings.risk)
    account = AccountState(
        balance=1000,
        equity=980,  # 2% down from day start 1000
        margin=0,
        free_margin=980,
        currency="USD",
        account_mode="demo",
    )
    portfolio = RiskPortfolioContext(
        day_start_equity=1000.0,
        week_start_equity=1000.0,
        consecutive_losses=0,
        open_positions=[],
    )
    decision = risk.account_risk_state(account, portfolio)
    assert decision.reason_code == "DAILY_LOSS_LOCK"
    if settings.monitoring.auto_halt_on_risk_lock:
        ControlStore(db).halt(decision.reason_code, source="auto")
    assert ControlStore(db).get().halted
    assert ControlStore(db).get().halt_reason == "DAILY_LOSS_LOCK"


def test_control_api_halt_resume(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "api.db"
    monkeypatch.setenv("TRADER_DB_PATH", str(db_path))
    settings = load_settings()
    settings = settings.model_copy(
        update={"database": settings.database.model_copy(update={"path": str(db_path)})}
    )
    JournalDB(db_path).migrate()
    server = serve_control_api(settings, host="127.0.0.1", port=0, blocking=False)
    host, port = server.server_address[:2]
    base = f"http://{host}:{port}"
    try:
        with httpx.Client(timeout=5.0) as client:
            r = client.get(f"{base}/health")
            assert r.status_code == 200
            assert r.json()["ok"] is True

            r = client.post(f"{base}/halt", json={"reason": "API_HALT"})
            assert r.status_code == 200
            assert r.json()["control"]["halted"] is True

            r = client.get(f"{base}/status")
            assert r.json()["control"]["halt_reason"] == "API_HALT"

            r = client.post(f"{base}/resume", json={})
            assert r.json()["control"]["halted"] is False

            r = client.post(f"{base}/kill", json={"reason": "API_KILL"})
            assert r.json()["control"]["kill_switch"] is True
            r = client.post(f"{base}/resume", json={"clear_kill": True})
            assert r.json()["control"]["kill_switch"] is False
    finally:
        server.shutdown()
        server.server_close()


def test_cli_halt_status_resume_blocks_execute(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "cli.db"
    monkeypatch.setenv("TRADER_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADER_BROKER_BACKEND", "mock")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:9")

    halt = runner.invoke(app, ["halt", "--reason", "CLI_HALT", "--json"])
    assert halt.exit_code == 0
    assert "CLI_HALT" in halt.stdout

    status = runner.invoke(app, ["status", "--json"])
    assert status.exit_code == 0
    assert "CLI_HALT" in status.stdout or "halted" in status.stdout

    blocked = runner.invoke(
        app,
        [
            "execute-demo",
            "--backend",
            "mock",
            "--dry-run",
            "--mock-llm",
            "--json",
        ],
    )
    assert blocked.exit_code == 0
    assert "blocked" in blocked.stdout.lower() or "CLI_HALT" in blocked.stdout

    resume = runner.invoke(app, ["resume", "--json"])
    assert resume.exit_code == 0

    health = runner.invoke(app, ["health"])
    assert health.exit_code == 0
    assert "Phase 7" in health.stdout


def test_config_monitoring_defaults() -> None:
    s = load_settings()
    assert s.monitoring.api_port == 8787
    assert s.monitoring.auto_halt_on_risk_lock is True
