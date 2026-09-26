-- Phase 4: persist scanner opportunity score components (plan §4.6).

CREATE TABLE IF NOT EXISTS opportunity_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    eligible INTEGER NOT NULL,
    reason_code TEXT NOT NULL,
    opportunity_score REAL NOT NULL,
    rank INTEGER,
    selected_for_strategy INTEGER NOT NULL DEFAULT 0,
    trend_score REAL,
    volatility_score REAL,
    momentum_score REAL,
    setup_quality REAL,
    liquidity_score REAL,
    spread_penalty REAL,
    abnormal_volatility_penalty REAL,
    correlation_penalty REAL,
    regime TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_opportunity_scores_symbol_tf
    ON opportunity_scores(symbol, timeframe, created_at);
