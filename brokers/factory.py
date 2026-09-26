"""Broker factory — prefer mock in CI/cloud; mt5 only on Windows LAN host."""

from __future__ import annotations

from brokers.base import BrokerPort
from brokers.mock import MockBroker
from brokers.mt5 import Mt5Broker
from trading.config import Settings


def create_broker(settings: Settings) -> BrokerPort:
    backend = settings.broker.backend
    if backend == "mock":
        return MockBroker(account_mode=settings.mt5.account_mode)
    if backend == "mt5":
        return Mt5Broker(
            terminal_path=settings.mt5.terminal_path,
            account_mode=settings.mt5.account_mode,
        )
    raise ValueError(f"Unknown broker backend: {backend}")
