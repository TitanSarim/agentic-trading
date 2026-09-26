"""Phase 8 — walk-forward, stress, soak, DoD report (offline)."""

from __future__ import annotations

from pathlib import Path

from research.backtest import BacktestConfig
from research.costs import CostConfig
from research.dod_report import build_dod_report, write_dod_report
from research.soak import SoakConfig, run_soak
from research.stress import run_stress_suite
from research.walk_forward import WalkForwardConfig, run_walk_forward
from trading.config import RiskSettings, load_settings
from trading.data.mock import MockMarketData
from trading.risk.engine import RiskEngine
from trading.types import Timeframe


def _bars(symbol: str = "EURUSD", count: int = 400):
    return MockMarketData(scenario="trend_pullback", seed=42).get_bars(
        symbol, Timeframe.M5, count=count
    )


def test_walk_forward_produces_oos_folds() -> None:
    bars = _bars(count=400)
    report = run_walk_forward(
        "EURUSD",
        "M5",
        bars,
        backtest_config=BacktestConfig(use_risk_engine=False, volume=0.1),
        walk_config=WalkForwardConfig(
            train_bars=200,
            test_bars=80,
            step_bars=80,
            mode="rolling",
            min_folds=2,
            min_oos_trades=1,
        ),
    )
    assert len(report.folds) >= 2
    assert report.oos_trades >= 1
    assert report.passed
    d = report.to_dict()
    assert d["fold_count"] == len(report.folds)
    # Chronological: each fold's test starts after train.
    for f in report.folds:
        assert f.test_start == f.train_end
        assert f.test_end > f.test_start


def test_walk_forward_expanding_mode() -> None:
    bars = _bars(count=400)
    report = run_walk_forward(
        "EURUSD",
        Timeframe.M5,
        bars,
        backtest_config=BacktestConfig(use_risk_engine=False),
        walk_config=WalkForwardConfig(
            train_bars=150,
            test_bars=60,
            step_bars=60,
            mode="expanding",
            min_folds=2,
            min_oos_trades=0,
        ),
    )
    assert len(report.folds) >= 2
    assert report.folds[0].train_start == 0
    assert report.folds[1].train_start == 0
    assert report.folds[1].train_end > report.folds[0].train_end


def test_stress_suite_passes_and_trips_consecutive_lock() -> None:
    bars = _bars(count=300)
    report = run_stress_suite(
        "EURUSD",
        "M5",
        bars,
        risk_settings=RiskSettings(
            consecutive_loss_lock=3,
            max_spread_pips=50.0,
            max_data_age_seconds=0,
        ),
        base_cost=CostConfig(slippage_pips=0.5),
        backtest_config=BacktestConfig(
            use_risk_engine=True,
            starting_equity=1000.0,
            one_position=True,
        ),
        spread_mult=5.0,
        slippage_pips=3.0,
    )
    names = {s.name for s in report.scenarios}
    assert names >= {
        "baseline",
        "spread_shock",
        "slippage_shock",
        "gap_shock",
        "consecutive_losses",
    }
    consec = next(s for s in report.scenarios if s.name == "consecutive_losses")
    assert consec.passed
    assert consec.extras.get("approved") is False
    assert report.passed


def test_soak_halt_kill_resume_and_heartbeats(tmp_path: Path) -> None:
    db = tmp_path / "soak.db"
    report = run_soak(
        db_path=db,
        config=SoakConfig(
            ticks=24,
            symbol="EURUSD",
            timeframe="M5",
            bars=200,
            halt_at_tick=6,
            kill_at_tick=12,
            resume_at_tick=18,
        ),
        risk_settings=RiskSettings(
            max_spread_pips=50.0,
            max_data_age_seconds=0,
        ),
    )
    assert report.passed
    assert report.halt_injected
    assert report.kill_injected
    assert report.resume_verified
    assert report.control_blocks >= 1
    assert report.heartbeats >= 1
    assert db.is_file()


def test_dod_report_writes_json_and_markdown(tmp_path: Path) -> None:
    bars = _bars(count=400)
    wf = run_walk_forward(
        "EURUSD",
        "M5",
        bars,
        backtest_config=BacktestConfig(use_risk_engine=False),
        walk_config=WalkForwardConfig(
            train_bars=200,
            test_bars=80,
            step_bars=80,
            min_folds=2,
            min_oos_trades=1,
        ),
    )
    stress = run_stress_suite(
        "EURUSD",
        "M5",
        bars,
        risk_settings=RiskSettings(max_spread_pips=50.0, max_data_age_seconds=0),
        backtest_config=BacktestConfig(use_risk_engine=True, starting_equity=1000.0),
    )
    soak = run_soak(
        db_path=tmp_path / "soak.db",
        config=SoakConfig(ticks=20, halt_at_tick=5, kill_at_tick=10, resume_at_tick=15),
    )
    dod = build_dod_report(walk_forward=wf, stress=stress, soak=soak)
    assert dod.overall_passed
    jp, mp = write_dod_report(dod, output_dir=tmp_path / "reports", stem="p8-validation")
    assert jp.is_file()
    assert mp.is_file()
    text = mp.read_text(encoding="utf-8")
    assert "PASS" in text
    assert "out_of_scope_p8" in text or "out of P8" in text.lower() or "P9" in text


def test_validation_settings_load() -> None:
    settings = load_settings("config/default.yaml")
    assert settings.validation.train_bars == 200
    assert settings.validation.report_dir == "reports"
    assert settings.validation.mode == "rolling"


def test_cli_walk_forward_and_soak(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from apps.trader.cli import app

    runner = CliRunner()
    db = tmp_path / "trader.db"
    env = {"TRADER_DB_PATH": str(db), "OLLAMA_BACKEND": "mock"}

    r1 = runner.invoke(
        app,
        ["walk-forward", "--symbol", "EURUSD", "--bars", "400", "--json"],
        env=env,
    )
    assert r1.exit_code == 0, r1.output
    assert "oos_trades" in r1.output

    r2 = runner.invoke(
        app,
        [
            "soak",
            "--ticks",
            "20",
            "--db",
            str(tmp_path / "soak.db"),
            "--json",
        ],
        env=env,
    )
    assert r2.exit_code == 0, r2.output
    assert "heartbeats" in r2.output

    r3 = runner.invoke(
        app,
        [
            "validate",
            "--bars",
            "400",
            "--ticks",
            "20",
            "--output-dir",
            str(tmp_path / "reports"),
            "--json",
        ],
        env=env,
    )
    assert r3.exit_code == 0, r3.output
    assert (tmp_path / "reports" / "p8-validation.json").is_file()
    assert (tmp_path / "reports" / "p8-validation.md").is_file()
