"""Persistent kill / halt / resume controls (fail-closed for new entries).

Stored in SQLite so operator actions survive process restarts and are shared
by CLI, optional control HTTP, RiskEngine, and execute-demo.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from trading.journal.db import JournalDB
from trading.risk.engine import RiskEngine
from trading.types import utc_now

Source = Literal["operator", "auto", "api", "system", "migrate"]


@dataclass(frozen=True)
class ControlState:
    kill_switch: bool = False
    kill_reason: str = ""
    halted: bool = False
    halt_reason: str = ""
    updated_at: str = ""
    updated_by: str = "system"

    @property
    def new_entries_blocked(self) -> bool:
        return self.kill_switch or self.halted

    @property
    def block_reason(self) -> str:
        if self.kill_switch:
            return self.kill_reason or "KILL_SWITCH"
        if self.halted:
            return self.halt_reason or "HALTED"
        return ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kill_switch": self.kill_switch,
            "kill_reason": self.kill_reason,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "new_entries_blocked": self.new_entries_blocked,
            "block_reason": self.block_reason,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
        }


class ControlStore:
    """Read/write persistent control state and apply it to RiskEngine."""

    def __init__(self, db: JournalDB) -> None:
        self.db = db
        self._ensure_row()

    def _ensure_row(self) -> None:
        row = self.db._conn.execute(
            "SELECT id FROM control_state WHERE id = 1"
        ).fetchone()
        if row is None:
            self.db._conn.execute(
                """
                INSERT INTO control_state(
                    id, kill_switch, kill_reason, halted, halt_reason,
                    updated_at, updated_by
                ) VALUES (1, 0, '', 0, '', ?, 'system')
                """,
                (utc_now().isoformat(),),
            )
            self.db._conn.commit()

    def get(self) -> ControlState:
        self._ensure_row()
        row = self.db._conn.execute(
            """
            SELECT kill_switch, kill_reason, halted, halt_reason,
                   updated_at, updated_by
            FROM control_state WHERE id = 1
            """
        ).fetchone()
        assert row is not None
        return ControlState(
            kill_switch=bool(row["kill_switch"]),
            kill_reason=str(row["kill_reason"] or ""),
            halted=bool(row["halted"]),
            halt_reason=str(row["halt_reason"] or ""),
            updated_at=str(row["updated_at"] or ""),
            updated_by=str(row["updated_by"] or "system"),
        )

    def apply_to_risk(self, risk: RiskEngine) -> ControlState:
        """Mirror persistent controls onto a RiskEngine instance (fail-closed)."""
        state = self.get()
        if state.kill_switch:
            risk.engage_kill_switch(state.kill_reason or "KILL_SWITCH")
        if state.halted:
            risk.halt(state.halt_reason or "HALTED")
        return state

    def halt(
        self,
        reason: str = "HALTED",
        *,
        source: Source = "operator",
    ) -> ControlState:
        now = utc_now().isoformat()
        reason = reason or "HALTED"
        self.db._conn.execute(
            """
            UPDATE control_state
            SET halted = 1,
                halt_reason = ?,
                updated_at = ?,
                updated_by = ?
            WHERE id = 1
            """,
            (reason, now, source),
        )
        self.db._conn.commit()
        self.db.record_system_event(
            "halt",
            {"reason": reason, "source": source, "updated_at": now},
        )
        return self.get()

    def resume(
        self,
        *,
        clear_kill: bool = False,
        source: Source = "operator",
    ) -> ControlState:
        """Clear halt (and optionally kill). Open positions are unaffected."""
        now = utc_now().isoformat()
        if clear_kill:
            self.db._conn.execute(
                """
                UPDATE control_state
                SET halted = 0,
                    halt_reason = '',
                    kill_switch = 0,
                    kill_reason = '',
                    updated_at = ?,
                    updated_by = ?
                WHERE id = 1
                """,
                (now, source),
            )
        else:
            self.db._conn.execute(
                """
                UPDATE control_state
                SET halted = 0,
                    halt_reason = '',
                    updated_at = ?,
                    updated_by = ?
                WHERE id = 1
                """,
                (now, source),
            )
        self.db._conn.commit()
        self.db.record_system_event(
            "resume",
            {"clear_kill": clear_kill, "source": source, "updated_at": now},
        )
        return self.get()

    def kill(
        self,
        reason: str = "KILL_SWITCH",
        *,
        source: Source = "operator",
    ) -> ControlState:
        """Emergency kill: blocks new entries until clear_kill / resume --clear-kill."""
        now = utc_now().isoformat()
        reason = reason or "KILL_SWITCH"
        self.db._conn.execute(
            """
            UPDATE control_state
            SET kill_switch = 1,
                kill_reason = ?,
                halted = 1,
                halt_reason = ?,
                updated_at = ?,
                updated_by = ?
            WHERE id = 1
            """,
            (reason, reason, now, source),
        )
        self.db._conn.commit()
        self.db.record_system_event(
            "kill",
            {"reason": reason, "source": source, "updated_at": now},
        )
        return self.get()

    def clear_kill(self, *, source: Source = "operator") -> ControlState:
        now = utc_now().isoformat()
        self.db._conn.execute(
            """
            UPDATE control_state
            SET kill_switch = 0,
                kill_reason = '',
                updated_at = ?,
                updated_by = ?
            WHERE id = 1
            """,
            (now, source),
        )
        self.db._conn.commit()
        self.db.record_system_event(
            "clear_kill",
            {"source": source, "updated_at": now},
        )
        return self.get()
