"""Cost model unit tests."""

from __future__ import annotations

from research.costs import CostConfig, CostModel
from trading.types import Side


def test_entry_exit_worse_than_mid() -> None:
    costs = CostModel(CostConfig(slippage_pips=1.0, commission_per_lot=7.0))
    mid = 1.1000
    buy_entry = costs.apply_entry_price(mid, Side.BUY, "EURUSD")
    sell_exit = costs.apply_exit_price(mid, Side.BUY, "EURUSD")
    assert buy_entry > mid
    assert sell_exit < mid


def test_roundtrip_cost_positive() -> None:
    costs = CostModel()
    est = costs.roundtrip_cost_estimate("EURUSD", volume=1.0)
    assert est > 0


def test_pnl_includes_commission() -> None:
    costs = CostModel(CostConfig(commission_per_lot=10.0, slippage_pips=0.0))
    # Zero price move after cost-adjusted prices → loss ≈ commission.
    pnl = costs.pnl_usd("EURUSD", Side.BUY, 1.0, 1.10, 1.10)
    assert pnl == -10.0
