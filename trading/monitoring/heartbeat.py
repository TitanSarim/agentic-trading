"""Heartbeat writer + stale-process detection hooks."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from trading.journal.db import JournalDB
from trading.types import utc_now


@dataclass(frozen=True)
class HeartbeatInfo:
    component: str
    beat_at: str
    age_seconds: float | None
    stale: bool
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "beat_at": self.beat_at,
            "age_seconds": self.age_seconds,
            "stale": self.stale,
            "payload": self.payload,
        }


class HeartbeatStore:
    """Persist last-seen heartbeats per component; detect staleness."""

    def __init__(
        self,
        db: JournalDB,
        *,
        stale_after_seconds: float = 120.0,
    ) -> None:
        self.db = db
        self.stale_after_seconds = stale_after_seconds

    def beat(
        self,
        component: str = "trader",
        payload: dict[str, Any] | None = None,
    ) -> HeartbeatInfo:
        now = utc_now()
        self.db._conn.execute(
            """
            INSERT INTO heartbeats(component, beat_at, payload_json)
            VALUES (?, ?, ?)
            ON CONFLICT(component) DO UPDATE SET
                beat_at=excluded.beat_at,
                payload_json=excluded.payload_json
            """,
            (component, now.isoformat(), json.dumps(payload or {})),
        )
        self.db._conn.commit()
        return HeartbeatInfo(
            component=component,
            beat_at=now.isoformat(),
            age_seconds=0.0,
            stale=False,
            payload=payload or {},
        )

    def get(
        self,
        component: str = "trader",
        *,
        now: datetime | None = None,
    ) -> HeartbeatInfo | None:
        row = self.db._conn.execute(
            "SELECT component, beat_at, payload_json FROM heartbeats WHERE component = ?",
            (component,),
        ).fetchone()
        if row is None:
            return None
        return self._to_info(row, now=now)

    def list_all(self, *, now: datetime | None = None) -> list[HeartbeatInfo]:
        rows = self.db._conn.execute(
            "SELECT component, beat_at, payload_json FROM heartbeats ORDER BY component"
        ).fetchall()
        return [self._to_info(r, now=now) for r in rows]

    def is_stale(
        self,
        component: str = "trader",
        *,
        now: datetime | None = None,
        missing_is_stale: bool = True,
    ) -> bool:
        info = self.get(component, now=now)
        if info is None:
            return missing_is_stale
        return info.stale

    def _to_info(self, row: Any, *, now: datetime | None) -> HeartbeatInfo:
        clock = now or utc_now()
        if clock.tzinfo is None:
            clock = clock.replace(tzinfo=timezone.utc)
        beat_raw = str(row["beat_at"])
        try:
            beat_at = datetime.fromisoformat(beat_raw)
            if beat_at.tzinfo is None:
                beat_at = beat_at.replace(tzinfo=timezone.utc)
            age = max(0.0, (clock - beat_at).total_seconds())
        except ValueError:
            age = None
        stale = age is None or age > self.stale_after_seconds
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        return HeartbeatInfo(
            component=str(row["component"]),
            beat_at=beat_raw,
            age_seconds=age,
            stale=stale,
            payload=payload if isinstance(payload, dict) else {},
        )
