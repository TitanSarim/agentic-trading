"""Phase 6 execution + MT5 adapter tests (offline mocks; no MetaTrader5 required)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from brokers.mock import MockBroker
from brokers.mt5 import Mt5Broker
from trading.config import ExecutionSettings, load_settings
from trading.execution.engine import (
    DemoConfirmRequired,
    ExecutionEngine,
    LiveTradingRefused,
)
from trading.execution.ids import make_client_order_id
from trading.execution.pipeline import run_demo_pipeline
from trading.execution.reconcile import BrokerReconciler
from trading.journal.db import JournalDB
from trading.llm.mock import MockLlm
from trading.types import (
    OrderRequest,
    OrderStatus,
    Position,
    RiskDecision,
    Side,
    TradeCandidate,
)


def _candidate() -> TradeCandidate:
    return TradeCandidate(
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


def _risk(volume: float = 0.10) -> RiskDecision:
    return RiskDecision(approved=True, volume=volume, reason_code="OK", risk_dollars=3.5)


def test_client_order_id_stable() -> None:
    from datetime import datetime, timezone

    ts = datetime(2024, 6, 1, 12, 30, tzinfo=timezone.utc)
    a = make_client_order_id(
        symbol="EURUSD",
        side="BUY",
        strategy="trend_pullback_v1",
        candidate_id=7,
        created_at=ts,
    )
    b = make_client_order_id(
        symbol="EURUSD",
        side="BUY",
        strategy="trend_pullback_v1",
        candidate_id=7,
        created_at=ts,
    )
    assert a == b
    assert len(a) <= 31
    assert a.startswith("at6_")


def test_execution_dry_run_default() -> None:
    broker = MockBroker()
    broker.connect()
    engine = ExecutionEngine(broker, ExecutionSettings(), account_mode="demo")
    plan = engine.build_plan(_candidate(), _risk(), candidate_id=1)
    result = engine.execute(plan, submit=False)
    assert result.dry_run is True
    assert result.submitted is False
    assert result.order is not None
    assert result.order.status == OrderStatus.PENDING
    assert broker.positions() == []


def test_execution_submit_requires_confirm_demo() -> None:
    broker = MockBroker()
    broker.connect()
    engine = ExecutionEngine(broker, ExecutionSettings(), account_mode="demo")
    plan = engine.build_plan(_candidate(), _risk(), candidate_id=1)
    with pytest.raises(DemoConfirmRequired):
        engine.execute(plan, submit=True, confirm_demo=False)


def test_execution_submit_demo_fill_and_idempotent() -> None:
    broker = MockBroker()
    broker.connect()
    engine = ExecutionEngine(broker, ExecutionSettings(), account_mode="demo")
    plan = engine.build_plan(_candidate(), _risk(), candidate_id=42)
    first = engine.execute(plan, submit=True, confirm_demo=True)
    second = engine.execute(plan, submit=True, confirm_demo=True)
    assert first.submitted and first.order is not None
    assert first.order.status == OrderStatus.FILLED
    assert second.order is not None
    assert first.order.broker_ticket == second.order.broker_ticket
    assert len(broker.positions()) == 1


def test_execution_refuses_live_without_allow_live() -> None:
    broker = MockBroker(account_mode="live", allow_live=False)
    engine = ExecutionEngine(
        broker, ExecutionSettings(allow_live=False), account_mode="live"
    )
    plan = engine.build_plan(_candidate(), _risk())
    with pytest.raises(LiveTradingRefused):
        engine.execute(plan, submit=True, confirm_demo=True)


def test_mock_requires_take_profit() -> None:
    broker = MockBroker(require_take_profit=True)
    broker.connect()
    # Bypass OrderRequest validation by constructing with take_profit=None
    req = OrderRequest(
        symbol="EURUSD",
        side=Side.BUY,
        volume=0.1,
        stop_loss=1.09,
        take_profit=None,
        client_order_id="cid-tp",
    )
    result = broker.submit_order(req)
    assert result.status == OrderStatus.REJECTED
    assert "take_profit" in result.message


def test_reconcile_locks_on_unexpected() -> None:
    broker = MockBroker()
    broker.connect()
    broker.inject_position(
        Position(
            ticket="999",
            symbol="EURUSD",
            side=Side.BUY,
            volume=0.1,
            entry_price=1.1,
            client_order_id="foreign",
        )
    )
    report = BrokerReconciler(broker).reconcile(set())
    assert report.new_trades_should_lock is True
    assert len(report.unexpected_broker) == 1


def test_pipeline_dry_run_journals(tmp_path) -> None:
    settings = load_settings("config/default.yaml", load_dotenv_file=False)
    settings = settings.model_copy(
        update={"database": settings.database.model_copy(update={"path": str(tmp_path / "t.db")})}
    )
    db = JournalDB(settings.database.path)
    db.migrate()
    broker = MockBroker(account_mode="demo")
    llm = MockLlm()
    result = run_demo_pipeline(
        settings,
        broker=broker,
        db=db,
        llm=llm,
        submit=False,
        confirm_demo=False,
        analyze=True,
    )
    assert result.mode == "dry-run"
    assert result.execution is not None
    assert result.execution.dry_run is True
    assert db.count_orders() >= 1
    db.close()


def test_pipeline_submit_mock(tmp_path) -> None:
    settings = load_settings("config/default.yaml", load_dotenv_file=False)
    settings = settings.model_copy(
        update={"database": settings.database.model_copy(update={"path": str(tmp_path / "t.db")})}
    )
    db = JournalDB(settings.database.path)
    db.migrate()
    broker = MockBroker(account_mode="demo")
    result = run_demo_pipeline(
        settings,
        broker=broker,
        db=db,
        llm=MockLlm(),
        submit=True,
        confirm_demo=True,
        analyze=True,
    )
    assert result.execution is not None
    assert result.execution.submitted is True
    assert result.execution.order is not None
    assert result.execution.order.status == OrderStatus.FILLED
    assert len(db.open_position_tickets()) == 1
    db.close()


class _FakeMT5:
    """Minimal MetaTrader5 stand-in for Mt5Broker unit tests."""

    __agentic_fake__ = True
    TRADE_ACTION_DEAL = 1
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_RETURN = 2

    def __init__(self) -> None:
        self._positions: list[Any] = []
        self._orders: dict[str, Any] = {}
        self._seq = 5000
        self.initialized = False

    def initialize(self, path: str | None = None) -> bool:
        self.initialized = True
        return True

    def shutdown(self) -> None:
        self.initialized = False

    def login(self, *args: Any, **kwargs: Any) -> bool:
        return True

    def last_error(self) -> tuple[int, str]:
        return (0, "ok")

    def account_info(self) -> Any:
        return SimpleNamespace(
            balance=1000.0,
            equity=1000.0,
            margin=0.0,
            margin_free=1000.0,
            currency="USD",
            trade_mode=0,  # demo
        )

    def positions_get(self) -> list[Any]:
        return list(self._positions)

    def symbol_select(self, symbol: str, enable: bool) -> bool:
        return True

    def symbol_info(self, symbol: str) -> Any:
        return SimpleNamespace(filling_mode=2)

    def symbol_info_tick(self, symbol: str) -> Any:
        return SimpleNamespace(ask=1.1005, bid=1.1003)

    def order_check(self, request: dict) -> Any:
        return SimpleNamespace(retcode=0, comment="ok")

    def order_send(self, request: dict) -> Any:
        self._seq += 1
        ticket = self._seq
        pos = SimpleNamespace(
            ticket=ticket,
            symbol=request["symbol"],
            type=0 if request["type"] == self.ORDER_TYPE_BUY else 1,
            volume=request["volume"],
            price_open=request["price"],
            sl=request.get("sl", 0.0),
            tp=request.get("tp", 0.0),
            profit=0.0,
            comment=request.get("comment", ""),
        )
        # Close path uses position key
        if "position" in request:
            self._positions = [
                p for p in self._positions if int(p.ticket) != int(request["position"])
            ]
            return SimpleNamespace(retcode=10009, order=ticket, deal=ticket, price=request["price"], comment="closed")
        self._positions.append(pos)
        return SimpleNamespace(
            retcode=10009, order=ticket, deal=ticket, price=request["price"], comment="done"
        )


def test_mt5_broker_with_fake_module_demo_order() -> None:
    fake = _FakeMT5()
    broker = Mt5Broker(
        terminal_path=r"C:\Program Files\MetaTrader 5 IC Markets Global\terminal64.exe",
        account_mode="demo",
        allow_live=False,
        mt5_module=fake,
    )
    broker.connect()
    account = broker.account_state()
    assert account.account_mode == "demo"
    assert account.equity == 1000.0

    req = OrderRequest(
        symbol="EURUSD",
        side=Side.BUY,
        volume=0.1,
        stop_loss=1.0980,
        take_profit=1.1040,
        client_order_id="at6_EURUSD_testid01",
    )
    first = broker.submit_order(req)
    second = broker.submit_order(req)
    assert first.status == OrderStatus.FILLED
    assert first.broker_ticket == second.broker_ticket
    assert len(broker.positions()) == 1

    cancelled = broker.cancel_order(req.client_order_id)
    assert cancelled.status == OrderStatus.CANCELLED
    assert broker.positions() == []
    broker.disconnect()


def test_mt5_refuses_live_without_allow_live() -> None:
    fake = _FakeMT5()
    broker = Mt5Broker(account_mode="live", allow_live=False, mt5_module=fake)
    with pytest.raises(RuntimeError, match="allow_live"):
        broker.connect()


def test_config_broker_mode_demo() -> None:
    settings = load_settings("config/default.yaml", load_dotenv_file=False)
    assert settings.broker.mode == "demo"
    assert settings.mt5.account_mode == "demo"
    assert settings.execution.allow_live is False
