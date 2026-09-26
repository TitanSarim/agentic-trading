-- Phase 1 journal schema (SQLite).
-- Postgres may replace this later; keep tables minimal and aligned to plan §4.6.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS system_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS market_bars (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    bar_time TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL DEFAULT 0,
    spread REAL,
    UNIQUE (symbol, timeframe, bar_time)
);

CREATE TABLE IF NOT EXISTS trade_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry REAL NOT NULL,
    stop REAL NOT NULL,
    target REAL NOT NULL,
    risk_reward REAL,
    regime TEXT,
    setup_score REAL,
    payload_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS llm_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    candidate_id INTEGER,
    model TEXT,
    prompt_version TEXT,
    decision TEXT NOT NULL,
    confidence REAL,
    risk_modifier REAL,
    reason_code TEXT,
    raw_response TEXT,
    FOREIGN KEY (candidate_id) REFERENCES trade_candidates(id)
);

CREATE TABLE IF NOT EXISTS risk_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    candidate_id INTEGER,
    approved INTEGER NOT NULL,
    volume REAL,
    reason_code TEXT,
    risk_dollars REAL,
    new_trades_locked INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (candidate_id) REFERENCES trade_candidates(id)
);

CREATE TABLE IF NOT EXISTS account_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    balance REAL NOT NULL,
    equity REAL NOT NULL,
    margin REAL,
    free_margin REAL,
    currency TEXT,
    account_mode TEXT
);
