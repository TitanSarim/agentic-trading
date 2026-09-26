"""Aggregate operator status: control, heartbeats, recent journal, health flags."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from trading.config import Settings
from trading.journal.db import JournalDB
from trading.monitoring.control import ControlState, ControlStore
from trading.monitoring.heartbeat import HeartbeatStore


@dataclass
class StatusReport:
    healthy: bool
    control: ControlState
    heartbeats: list[dict[str, Any]] = field(default_factory=list)
    recent_events: list[dict[str, Any]] = field(default_factory=list)
    recent_candidates: list[dict[str, Any]] = field(default_factory=list)
    open_positions: int = 0
    order_count: int = 0
    last_equity: float | None = None
    last_account_mode: str | None = None
    flags: dict[str, Any] = field(default_factory=dict)
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "healthy": self.healthy,
            "message": self.message,
            "control": self.control.to_dict(),
            "heartbeats": self.heartbeats,
            "recent_events": self.recent_events,
            "recent_candidates": self.recent_candidates,
            "open_positions": self.open_positions,
            "order_count": self.order_count,
            "last_equity": self.last_equity,
            "last_account_mode": self.last_account_mode,
            "flags": self.flags,
        }


def build_status_report(
    settings: Settings,
    db: JournalDB,
    *,
    heartbeat_component: str = "trader",
) -> StatusReport:
    control = ControlStore(db).get()
    hb = HeartbeatStore(
        db,
        stale_after_seconds=settings.monitoring.heartbeat_stale_seconds,
    )
    heartbeats = [h.to_dict() for h in hb.list_all()]
    trader_hb = hb.get(heartbeat_component)
    events = db.recent_system_events(limit=10)
    candidates = _recent_candidates(db, limit=5)
    open_positions = len(db.open_position_tickets())
    order_count = db.count_orders()
    equity, mode = _last_account(db)

    stale = trader_hb is None or trader_hb.stale
    blocked = control.new_entries_blocked
    flags = {
        "new_entries_blocked": blocked,
        "kill_switch": control.kill_switch,
        "halted": control.halted,
        "heartbeat_stale": stale,
        "heartbeat_missing": trader_hb is None,
        "broker_backend": settings.broker.backend,
        "broker_mode": settings.broker.mode,
        "allow_live": settings.execution.allow_live,
        "control_api_enabled": settings.monitoring.api_enabled,
    }

    healthy = not control.kill_switch and not (stale and settings.monitoring.fail_closed_on_stale)
    if control.kill_switch:
        message = f"kill switch active: {control.block_reason}"
    elif control.halted:
        message = f"halted: {control.block_reason}"
    elif stale:
        message = "heartbeat stale or missing"
    else:
        message = "ok"

    return StatusReport(
        healthy=healthy,
        control=control,
        heartbeats=heartbeats,
        recent_events=events,
        recent_candidates=candidates,
        open_positions=open_positions,
        order_count=order_count,
        last_equity=equity,
        last_account_mode=mode,
        flags=flags,
        message=message,
    )


def _recent_candidates(db: JournalDB, *, limit: int) -> list[dict[str, Any]]:
    rows = db._conn.execute(
        """
        SELECT id, created_at, symbol, strategy, direction, setup_score
        FROM trade_candidates
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "created_at": r["created_at"],
            "symbol": r["symbol"],
            "strategy": r["strategy"],
            "direction": r["direction"],
            "setup_score": r["setup_score"],
        }
        for r in rows
    ]


def _last_account(db: JournalDB) -> tuple[float | None, str | None]:
    row = db._conn.execute(
        """
        SELECT equity, account_mode
        FROM account_snapshots
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None, None
    return float(row["equity"]), str(row["account_mode"] or "") or None
