"""Broker vs journal reconciliation — broker is source of truth for positions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from brokers.base import BrokerPort
from trading.types import Position


@dataclass
class ReconcileReport:
    """Result of comparing broker positions to journaled open positions."""

    broker_positions: list[Position]
    journal_tickets: set[str]
    unexpected_broker: list[Position] = field(default_factory=list)
    missing_on_broker: list[str] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)
    new_trades_should_lock: bool = False
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "broker_count": len(self.broker_positions),
            "journal_count": len(self.journal_tickets),
            "matched": list(self.matched),
            "unexpected_broker": [p.model_dump(mode="json") for p in self.unexpected_broker],
            "missing_on_broker": list(self.missing_on_broker),
            "new_trades_should_lock": self.new_trades_should_lock,
            "message": self.message,
        }


class BrokerReconciler:
    """Sync broker open positions against journal tickets before new orders.

    Unexpected broker positions → lock new trades (caller applies RiskEngine lock).
    """

    def __init__(self, broker: BrokerPort) -> None:
        self.broker = broker

    def reconcile(self, journal_open_tickets: set[str] | list[str]) -> ReconcileReport:
        if not self.broker.is_connected():
            self.broker.connect()

        journal = set(journal_open_tickets)
        broker_positions = self.broker.positions()
        broker_tickets = {p.ticket for p in broker_positions}

        matched = sorted(journal & broker_tickets)
        missing = sorted(journal - broker_tickets)
        unexpected = [p for p in broker_positions if p.ticket not in journal]

        lock = bool(unexpected)
        msg = "ok"
        if unexpected:
            msg = (
                f"unexpected broker positions: "
                f"{[p.ticket for p in unexpected]} — lock new trades"
            )
        elif missing:
            msg = f"journal tickets missing on broker (closed?): {missing}"

        return ReconcileReport(
            broker_positions=broker_positions,
            journal_tickets=journal,
            unexpected_broker=unexpected,
            missing_on_broker=missing,
            matched=matched,
            new_trades_should_lock=lock,
            message=msg,
        )
