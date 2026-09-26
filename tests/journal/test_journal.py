"""Journal migrations and system_events."""

from __future__ import annotations

from pathlib import Path

from trading.journal.db import JournalDB
from trading.types import AccountState


def test_migrate_and_system_events(tmp_path: Path) -> None:
    db = JournalDB(tmp_path / "journal.db")
    applied = db.migrate()
    assert 1 in applied
    # Idempotent
    assert db.migrate() == []

    eid = db.record_system_event("boot", {"phase": 1})
    assert eid >= 1
    snap = db.record_account_snapshot(
        AccountState(balance=1000, equity=1000, account_mode="demo")
    )
    assert snap >= 1
    events = db.recent_system_events()
    assert events[0]["event_type"] == "boot"
    db.close()
