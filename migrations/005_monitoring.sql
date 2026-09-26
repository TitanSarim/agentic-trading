-- Phase 7 monitoring: persistent kill/halt control + heartbeats.

PRAGMA foreign_keys = ON;

-- Single-row operator control state (id must be 1).
CREATE TABLE IF NOT EXISTS control_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    kill_switch INTEGER NOT NULL DEFAULT 0,
    kill_reason TEXT NOT NULL DEFAULT '',
    halted INTEGER NOT NULL DEFAULT 0,
    halt_reason TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL DEFAULT 'system'
);

INSERT OR IGNORE INTO control_state(
    id, kill_switch, kill_reason, halted, halt_reason, updated_at, updated_by
) VALUES (1, 0, '', 0, '', datetime('now'), 'migrate');

CREATE TABLE IF NOT EXISTS heartbeats (
    component TEXT PRIMARY KEY,
    beat_at TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}'
);
