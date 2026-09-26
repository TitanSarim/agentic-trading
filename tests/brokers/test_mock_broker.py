"""Mock broker round-trip tests."""

from __future__ import annotations

import pytest

from brokers.factory import create_broker
from brokers.mock import MockBroker
from trading.config import load_settings
from trading.types import OrderRequest, OrderStatus, Side


def test_mock_broker_connect_account_and_idempotent_order() -> None:
    broker = MockBroker(balance=500.0, account_mode="demo")
    with pytest.raises(RuntimeError):
        broker.account_state()

    broker.connect()
    assert broker.is_connected()
    account = broker.account_state()
    assert account.equity == 500.0
    assert account.account_mode == "demo"

    req = OrderRequest(
        symbol="EURUSD",
        side=Side.BUY,
        volume=0.1,
        stop_loss=1.09,
        take_profit=1.12,
        client_order_id="cid-1",
    )
    first = broker.submit_order(req)
    second = broker.submit_order(req)
    assert first.status == OrderStatus.FILLED
    assert first.broker_ticket == second.broker_ticket
    assert len(broker.positions()) == 1

    cancelled = broker.cancel_order("cid-1")
    assert cancelled.status == OrderStatus.CANCELLED
    assert broker.positions() == []
    broker.disconnect()


def test_factory_returns_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    settings = load_settings("config/default.yaml", load_dotenv_file=False)
    broker = create_broker(settings)
    assert isinstance(broker, MockBroker)
