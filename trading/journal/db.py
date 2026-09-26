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
    OrderResult,
    Position,
    RiskDecision,
    Side,
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
        validated = decision.validated_response
        validated_json = (
            json.dumps(validated) if validated is not None else None
        )
        cur = self._conn.execute(
            """
            INSERT INTO llm_decisions(
                created_at, candidate_id, model, prompt_version, decision,
                confidence, risk_modifier, reason_code, raw_response,
                input_hash, validated_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                decision.input_hash,
                validated_json,
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

    def record_opportunity_score(self, market: Any) -> int:
        """Persist one scanner row (ScannedMarket-like)."""
        components = getattr(market, "components", None)
        comps = components.as_dict() if components is not None else {}
        cur = self._conn.execute(
            """
            INSERT INTO opportunity_scores(
                created_at, symbol, timeframe, eligible, reason_code,
                opportunity_score, rank, selected_for_strategy,
                trend_score, volatility_score, momentum_score, setup_quality,
                liquidity_score, spread_penalty, abnormal_volatility_penalty,
                correlation_penalty, regime, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now().isoformat(),
                market.symbol,
                market.timeframe.value
                if hasattr(market.timeframe, "value")
                else str(market.timeframe),
                1 if market.eligible else 0,
                market.reason_code,
                market.opportunity_score,
                market.rank,
                1 if market.selected_for_strategy else 0,
                comps.get("trend_score"),
                comps.get("volatility_score"),
                comps.get("momentum_score"),
                comps.get("setup_quality"),
                comps.get("liquidity_score"),
                comps.get("spread_penalty"),
                comps.get("abnormal_volatility_penalty"),
                comps.get("correlation_penalty"),
                getattr(market, "regime", None),
                json.dumps(
                    market.to_dict() if hasattr(market, "to_dict") else comps
                ),
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def record_scan_report(self, report: Any) -> list[int]:
        """Persist all markets from a ScanReport; return row ids."""
        return [self.record_opportunity_score(m) for m in report.markets]

    def count_opportunity_scores(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM opportunity_scores"
        ).fetchone()
        return int(row[0])

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

    def record_order(
        self,
        *,
        client_order_id: str,
        symbol: str,
        side: str,
        volume: float,
        status: str,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        broker_ticket: str | None = None,
        fill_price: float | None = None,
        message: str = "",
        dry_run: bool = False,
        account_mode: str = "demo",
        candidate_id: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO orders(
                created_at, client_order_id, candidate_id, symbol, side, volume,
                stop_loss, take_profit, status, broker_ticket, fill_price, message,
                dry_run, account_mode, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(client_order_id) DO UPDATE SET
                status=excluded.status,
                broker_ticket=COALESCE(excluded.broker_ticket, orders.broker_ticket),
                fill_price=COALESCE(excluded.fill_price, orders.fill_price),
                message=excluded.message,
                dry_run=excluded.dry_run,
                payload_json=excluded.payload_json
            """,
            (
                utc_now().isoformat(),
                client_order_id,
                candidate_id,
                symbol,
                side,
                volume,
                stop_loss,
                take_profit,
                status,
                broker_ticket,
                fill_price,
                message,
                1 if dry_run else 0,
                account_mode,
                json.dumps(payload or {}),
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def record_order_result(
        self,
        order: OrderResult,
        *,
        symbol: str,
        side: str,
        volume: float,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        dry_run: bool = False,
        account_mode: str = "demo",
        candidate_id: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int:
        oid = self.record_order(
            client_order_id=order.client_order_id,
            symbol=symbol,
            side=side,
            volume=volume,
            status=order.status.value,
            stop_loss=stop_loss,
            take_profit=take_profit,
            broker_ticket=order.broker_ticket,
            fill_price=order.fill_price,
            message=order.message,
            dry_run=dry_run,
            account_mode=account_mode,
            candidate_id=candidate_id,
            payload=payload,
        )
        if (
            not dry_run
            and order.status.value == "FILLED"
            and order.fill_price is not None
            and order.broker_ticket
        ):
            self.record_fill(
                client_order_id=order.client_order_id,
                broker_ticket=order.broker_ticket,
                symbol=symbol,
                side=side,
                volume=volume,
                fill_price=order.fill_price,
            )
            self.upsert_position(
                Position(
                    ticket=order.broker_ticket,
                    symbol=symbol,
                    side=Side(side) if not isinstance(side, Side) else side,
                    volume=volume,
                    entry_price=order.fill_price,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    client_order_id=order.client_order_id,
                )
            )
        return oid

    def record_fill(
        self,
        *,
        client_order_id: str,
        broker_ticket: str | None,
        symbol: str,
        side: str,
        volume: float,
        fill_price: float,
        payload: dict[str, Any] | None = None,
    ) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO fills(
                created_at, client_order_id, broker_ticket, symbol, side,
                volume, fill_price, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now().isoformat(),
                client_order_id,
                broker_ticket,
                symbol,
                side,
                volume,
                fill_price,
                json.dumps(payload or {}),
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def upsert_position(self, position: Position, *, status: str = "open") -> None:
        now = utc_now().isoformat()
        self._conn.execute(
            """
            INSERT INTO positions_journal(
                ticket, created_at, updated_at, client_order_id, symbol, side,
                volume, entry_price, stop_loss, take_profit, profit, status, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticket) DO UPDATE SET
                updated_at=excluded.updated_at,
                volume=excluded.volume,
                stop_loss=excluded.stop_loss,
                take_profit=excluded.take_profit,
                profit=excluded.profit,
                status=excluded.status,
                payload_json=excluded.payload_json
            """,
            (
                position.ticket,
                now,
                now,
                position.client_order_id,
                position.symbol,
                position.side.value if hasattr(position.side, "value") else str(position.side),
                position.volume,
                position.entry_price,
                position.stop_loss,
                position.take_profit,
                position.profit,
                status,
                position.model_dump_json(),
            ),
        )
        self._conn.commit()

    def mark_position_closed(self, ticket: str) -> None:
        self._conn.execute(
            """
            UPDATE positions_journal
            SET status='closed', updated_at=?
            WHERE ticket=?
            """,
            (utc_now().isoformat(), ticket),
        )
        self._conn.commit()

    def open_position_tickets(self) -> set[str]:
        rows = self._conn.execute(
            "SELECT ticket FROM positions_journal WHERE status='open'"
        ).fetchall()
        return {str(r["ticket"]) for r in rows}

    def count_orders(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM orders").fetchone()
        return int(row[0])

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
