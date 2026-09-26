"""Broker factory — prefer mock in CI/cloud; mt5 only on Windows LAN host."""

from __future__ import annotations

from brokers.base import BrokerPort
from brokers.mock import MockBroker
from brokers.mt5 import Mt5Broker
from trading.config import Settings


def create_broker(settings: Settings) -> BrokerPort:
    backend = settings.broker.backend
    mode = settings.broker.mode or settings.mt5.account_mode
    allow_live = settings.execution.allow_live
    if backend == "mock":
        return MockBroker(
            account_mode=mode,
            allow_live=allow_live,
            require_take_profit=settings.execution.require_attached_stop,
        )
    if backend == "mt5":
        return Mt5Broker(
            terminal_path=settings.mt5.terminal_path,
            account_mode=mode,
            allow_live=allow_live,
        )
    raise ValueError(f"Unknown broker backend: {backend}")
