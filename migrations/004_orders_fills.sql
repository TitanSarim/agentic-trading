-- Phase 6: orders / fills / open positions journal.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    client_order_id TEXT NOT NULL UNIQUE,
    candidate_id INTEGER,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    volume REAL NOT NULL,
    stop_loss REAL,
    take_profit REAL,
    status TEXT NOT NULL,
    broker_ticket TEXT,
    fill_price REAL,
    message TEXT,
    dry_run INTEGER NOT NULL DEFAULT 0,
    account_mode TEXT NOT NULL DEFAULT 'demo',
    payload_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (candidate_id) REFERENCES trade_candidates(id)
);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    client_order_id TEXT NOT NULL,
    broker_ticket TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    volume REAL NOT NULL,
    fill_price REAL NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS positions_journal (
    ticket TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    client_order_id TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    volume REAL NOT NULL,
    entry_price REAL NOT NULL,
    stop_loss REAL,
    take_profit REAL,
    profit REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'open',
    payload_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_fills_client_order_id ON fills(client_order_id);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions_journal(status);
