"""Broker adapter protocol — MT5 real path is Windows-only; mock for CI/cloud."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from trading.types import AccountState, OrderRequest, OrderResult, Position


@runtime_checkable
class BrokerPort(Protocol):
    """Authority: executor only. Risk must approve before submit is called."""

    name: str

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def is_connected(self) -> bool: ...

    def account_state(self) -> AccountState: ...

    def positions(self) -> list[Position]: ...

    def submit_order(self, request: OrderRequest) -> OrderResult:
        """Submit a risk-approved order. P1 mock only — no live trading CLI."""
        ...

    def cancel_order(self, client_order_id: str) -> OrderResult: ...
