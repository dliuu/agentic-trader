from __future__ import annotations

from pathlib import Path

import aiosqlite

DB_PATH = Path("data/trades.db")


async def get_db() -> aiosqlite.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(DB_PATH)
    await db.execute("PRAGMA journal_mode=WAL")
    await _ensure_tables(db)
    return db


async def _ensure_tables(db: aiosqlite.Connection) -> None:
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS scans (
            id TEXT PRIMARY KEY,
            ticker TEXT NOT NULL,
            data JSON NOT NULL,
            scanned_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS grades (
            id TEXT PRIMARY KEY,
            candidate_id TEXT NOT NULL,
            score INTEGER NOT NULL,
            verdict TEXT NOT NULL,
            rationale TEXT,
            signals_confirmed JSON,
            model TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            latency_ms INTEGER,
            graded_at TEXT NOT NULL,
            FOREIGN KEY (candidate_id) REFERENCES scans(id)
        );
        CREATE TABLE IF NOT EXISTS executions (
            id TEXT PRIMARY KEY,
            grade_id TEXT NOT NULL,
            action TEXT NOT NULL,
            filled_at TEXT,
            FOREIGN KEY (grade_id) REFERENCES grades(id)
        );
        CREATE TABLE IF NOT EXISTS flow_scores (
            candidate_id TEXT PRIMARY KEY,
            score INTEGER NOT NULL,
            rationale TEXT,
            signals JSON,
            skipped INTEGER DEFAULT 0,
            skip_reason TEXT,
            scored_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS signals (
            id TEXT PRIMARY KEY,
            ticker TEXT NOT NULL,
            strike REAL NOT NULL,
            expiry TEXT NOT NULL,
            option_type TEXT NOT NULL,
            direction TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending',
            initial_score INTEGER NOT NULL,
            initial_premium REAL NOT NULL,
            initial_oi INTEGER NOT NULL,
            initial_volume INTEGER NOT NULL,
            initial_contract_adv INTEGER NOT NULL DEFAULT 0,
            grade_id TEXT NOT NULL,
            conviction_score REAL NOT NULL,
            snapshots_taken INTEGER NOT NULL DEFAULT 0,
            confirming_flows INTEGER NOT NULL DEFAULT 0,
            oi_high_water INTEGER NOT NULL DEFAULT 0,
            chain_spread_count INTEGER NOT NULL DEFAULT 0,
            cumulative_premium REAL NOT NULL DEFAULT 0.0,
            days_without_flow INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            last_polled_at TEXT,
            last_flow_at TEXT,
            matured_at TEXT,
            terminal_at TEXT,
            terminal_reason TEXT,
            risk_params_json TEXT,
            anomaly_fingerprint TEXT DEFAULT '',
            FOREIGN KEY (grade_id) REFERENCES grades(id)
        );
        CREATE INDEX IF NOT EXISTS idx_signals_state ON signals(state);
        CREATE INDEX IF NOT EXISTS idx_signals_ticker ON signals(ticker);
        CREATE INDEX IF NOT EXISTS idx_signals_expiry ON signals(expiry);

        CREATE TABLE IF NOT EXISTS signal_snapshots (
            id TEXT PRIMARY KEY,
            signal_id TEXT NOT NULL,
            snapshot_at TEXT NOT NULL,
            contract_oi INTEGER,
            contract_volume INTEGER,
            contract_bid REAL,
            contract_ask REAL,
            contract_spread_pct REAL,
            spot_price REAL,
            neighbor_oi_total INTEGER,
            neighbor_strikes_active INTEGER,
            neighbor_put_call_ratio REAL,
            new_flow_count INTEGER DEFAULT 0,
            new_flow_premium REAL DEFAULT 0.0,
            new_flow_same_contract INTEGER DEFAULT 0,
            new_flow_same_expiry INTEGER DEFAULT 0,
            conviction_delta REAL DEFAULT 0.0,
            conviction_after REAL DEFAULT 0.0,
            signals_fired TEXT,
            notes TEXT,
            FOREIGN KEY (signal_id) REFERENCES signals(id)
        );
        CREATE INDEX IF NOT EXISTS idx_snapshots_signal ON signal_snapshots(signal_id);
        CREATE INDEX IF NOT EXISTS idx_snapshots_at ON signal_snapshots(snapshot_at);
        """
    )
    await db.commit()
