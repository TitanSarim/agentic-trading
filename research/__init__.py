"""Research: costs, backtests, walk-forward, stress, soak (Phase 8). Offline only."""

from research.backtest import BacktestReport, Backtester, run_universe_backtest
from research.costs import CostConfig, CostModel
from research.dod_report import DodReport, build_dod_report, write_dod_report
from research.soak import SoakConfig, SoakReport, run_soak
from research.stress import StressReport, run_stress_suite
from research.walk_forward import (
    WalkForwardConfig,
    WalkForwardReport,
    WalkForwardRunner,
    run_walk_forward,
)

__all__ = [
    "BacktestReport",
    "Backtester",
    "CostConfig",
    "CostModel",
    "DodReport",
    "SoakConfig",
    "SoakReport",
    "StressReport",
    "WalkForwardConfig",
    "WalkForwardReport",
    "WalkForwardRunner",
    "build_dod_report",
    "run_soak",
    "run_stress_suite",
    "run_universe_backtest",
    "run_walk_forward",
    "write_dod_report",
]
