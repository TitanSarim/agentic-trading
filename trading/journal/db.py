"""SQLite journal + migrations for Phase 1."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from trading.types import (
    AccountState,
    AnalystDecision,
    Bar,
    RiskDecision,
    TradeCandidate,
    utc_now,
)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


class JournalDB:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")

    def close(self) -> None:
        self._conn.close()

    def migrate(self) -> list[int]:
        applied: list[int] = []
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        existing = {
            row[0]
            for row in self._conn.execute("SELECT version FROM schema_migrations")
        }
        files = sorted(MIGRATIONS_DIR.glob("*.sql"))
        for sql_file in files:
            version = int(sql_file.name.split("_", 1)[0])
            if version in existing:
                continue
            script = sql_file.read_text(encoding="utf-8")
            self._conn.executescript(script)
            self._conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, utc_now().isoformat()),
            )
            self._conn.commit()
            applied.append(version)
        return applied

    def record_system_event(self, event_type: str, payload: dict[str, Any] | None = None) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO system_events(created_at, event_type, payload_json)
            VALUES (?, ?, ?)
            """,
            (utc_now().isoformat(), event_type, json.dumps(payload or {})),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def upsert_bars(self, bars: list[Bar]) -> int:
        count = 0
        for bar in bars:
            self._conn.execute(
                """
                INSERT INTO market_bars(
                    symbol, timeframe, bar_time, open, high, low, close, volume, spread
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, timeframe, bar_time) DO UPDATE SET
                    open=excluded.open,
                    high=excluded.high,
                    low=excluded.low,
                    close=excluded.close,
                    volume=excluded.volume,
                    spread=excluded.spread
                """,
                (
                    bar.symbol,
                    bar.timeframe.value,
                    bar.time.isoformat(),
                    bar.open,
                    bar.high,
                    bar.low,
                    bar.close,
                    bar.volume,
                    bar.spread,
                ),
            )
            count += 1
        self._conn.commit()
        return count

    def count_bars(self, symbol: str | None = None) -> int:
        if symbol:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM market_bars WHERE symbol = ?",
                (symbol,),
            ).fetchone()
        else:
            row = self._conn.execute("SELECT COUNT(*) FROM market_bars").fetchone()
        return int(row[0])

    def record_candidate(self, candidate: TradeCandidate) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO trade_candidates(
                created_at, symbol, strategy, direction, entry, stop, target,
                risk_reward, regime, setup_score, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now().isoformat(),
                candidate.symbol,
                candidate.strategy,
                candidate.direction,
                candidate.entry,
                candidate.stop,
                candidate.target,
                candidate.risk_reward,
                candidate.regime,
                candidate.setup_score,
                candidate.model_dump_json(),
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def record_llm_decision(
        self,
        decision: AnalystDecision,
        *,
        candidate_id: int | None = None,
    ) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO llm_decisions(
                created_at, candidate_id, model, prompt_version, decision,
                confidence, risk_modifier, reason_code, raw_response
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now().isoformat(),
                candidate_id,
                decision.model,
                decision.prompt_version,
                decision.decision,
                decision.confidence,
                decision.risk_modifier,
                decision.reason_code,
                decision.raw_response,
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def record_risk_decision(
        self,
        decision: RiskDecision,
        *,
        candidate_id: int | None = None,
    ) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO risk_decisions(
                created_at, candidate_id, approved, volume, reason_code,
                risk_dollars, new_trades_locked
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now().isoformat(),
                candidate_id,
                1 if decision.approved else 0,
                decision.volume,
                decision.reason_code,
                decision.risk_dollars,
                1 if decision.new_trades_locked else 0,
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def record_account_snapshot(self, account: AccountState) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO account_snapshots(
                created_at, balance, equity, margin, free_margin, currency, account_mode
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now().isoformat(),
                account.balance,
                account.equity,
                account.margin,
                account.free_margin,
                account.currency,
                account.account_mode,
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def recent_system_events(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT id, created_at, event_type, payload_json
            FROM system_events
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "created_at": r["created_at"],
                "event_type": r["event_type"],
                "payload": json.loads(r["payload_json"]),
            }
            for r in rows
        ]
