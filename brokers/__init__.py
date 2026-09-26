"""Broker adapters for MetaTrader5 (Windows) and mocks (CI/cloud)."""

from brokers.factory import create_broker
from brokers.mock import MockBroker
from brokers.mt5 import Mt5Broker

__all__ = ["MockBroker", "Mt5Broker", "create_broker"]
