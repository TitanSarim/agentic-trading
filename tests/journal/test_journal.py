"""Journal migrations and system_events."""

from __future__ import annotations

from pathlib import Path

from trading.journal.db import JournalDB
from trading.llm.mock import MockLlm
from trading.types import AccountState, TradeCandidate


def test_migrate_and_system_events(tmp_path: Path) -> None:
    db = JournalDB(tmp_path / "journal.db")
    applied = db.migrate()
    assert 1 in applied
    assert 2 in applied
    assert 3 in applied
    assert 4 in applied
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


def test_record_llm_decision_with_hash(tmp_path: Path) -> None:
    db = JournalDB(tmp_path / "llm.db")
    db.migrate()
    candidate = TradeCandidate(
        symbol="EURUSD",
        strategy="trend_pullback_v1",
        direction="LONG",
        entry=1.1,
        stop=1.09,
        target=1.12,
        risk_reward=2.0,
    )
    cid = db.record_candidate(candidate)
    decision = MockLlm().validate_candidate(candidate)
    lid = db.record_llm_decision(decision, candidate_id=cid)
    assert lid >= 1
    row = db._conn.execute(
        "SELECT input_hash, validated_json, decision FROM llm_decisions WHERE id = ?",
        (lid,),
    ).fetchone()
    assert row["input_hash"]
    assert row["decision"] == "APPROVE"
    assert row["validated_json"]
    db.close()
