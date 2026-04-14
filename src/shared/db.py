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
            regrade_count INTEGER NOT NULL DEFAULT 0,
            last_regraded_at TEXT,
            milestones_fired TEXT DEFAULT '[]',
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

        CREATE TABLE IF NOT EXISTS flow_ledger (
            id TEXT PRIMARY KEY,
            signal_id TEXT NOT NULL,
            alert_id TEXT NOT NULL,
            ticker TEXT NOT NULL,
            strike REAL NOT NULL,
            expiry TEXT NOT NULL,
            option_type TEXT NOT NULL,
            direction TEXT NOT NULL,
            premium REAL NOT NULL,
            volume INTEGER NOT NULL DEFAULT 0,
            open_interest INTEGER,
            execution_type TEXT,
            underlying_price REAL,
            implied_volatility REAL,
            is_same_contract INTEGER NOT NULL DEFAULT 0,
            is_same_expiry INTEGER NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT 'scanner',
            created_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            FOREIGN KEY (signal_id) REFERENCES signals(id)
        );
        CREATE INDEX IF NOT EXISTS idx_ledger_signal ON flow_ledger(signal_id);
        CREATE INDEX IF NOT EXISTS idx_ledger_ticker ON flow_ledger(ticker);
        CREATE INDEX IF NOT EXISTS idx_ledger_created ON flow_ledger(created_at);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_alert_unique ON flow_ledger(alert_id);

        CREATE TABLE IF NOT EXISTS news_events (
            id TEXT PRIMARY KEY,
            signal_id TEXT NOT NULL,
            ticker TEXT NOT NULL,
            event_type TEXT NOT NULL,
            title TEXT NOT NULL,
            source TEXT NOT NULL,
            url TEXT,
            published_at TEXT NOT NULL,
            detected_at TEXT NOT NULL,
            catalyst_matched INTEGER NOT NULL DEFAULT 0,
            catalyst_keywords TEXT,
            filing_type TEXT,
            source_id TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (signal_id) REFERENCES signals(id)
        );
        CREATE INDEX IF NOT EXISTS idx_news_signal ON news_events(signal_id);
        CREATE INDEX IF NOT EXISTS idx_news_ticker ON news_events(ticker);
        CREATE INDEX IF NOT EXISTS idx_news_source_id ON news_events(source_id);
        CREATE INDEX IF NOT EXISTS idx_news_detected ON news_events(detected_at);

        CREATE TABLE IF NOT EXISTS regrades (
            id TEXT PRIMARY KEY,
            signal_id TEXT NOT NULL,
            trigger_reason TEXT NOT NULL,
            sentiment_score INTEGER,
            insider_score INTEGER,
            sector_score INTEGER,
            synthesis_score INTEGER,
            synthesis_rationale TEXT,
            deterministic_conviction REAL,
            blended_conviction REAL,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            latency_ms INTEGER NOT NULL DEFAULT 0,
            regraded_at TEXT NOT NULL,
            FOREIGN KEY (signal_id) REFERENCES signals(id)
        );
        CREATE INDEX IF NOT EXISTS idx_regrades_signal ON regrades(signal_id);
        """
    )
    await db.commit()
    await _migrate_signals_regrader_columns(db)


async def _migrate_signals_regrader_columns(db: aiosqlite.Connection) -> None:
    """Add re-grader columns to existing signals DBs (CREATE IF NOT EXISTS skips new cols)."""
    cur = await db.execute("PRAGMA table_info(signals)")
    rows = await cur.fetchall()
    colnames = {str(r[1]) for r in rows}
    migrations: list[str] = []
    if "regrade_count" not in colnames:
        migrations.append(
            "ALTER TABLE signals ADD COLUMN regrade_count INTEGER NOT NULL DEFAULT 0"
        )
    if "last_regraded_at" not in colnames:
        migrations.append("ALTER TABLE signals ADD COLUMN last_regraded_at TEXT")
    if "milestones_fired" not in colnames:
        migrations.append(
            "ALTER TABLE signals ADD COLUMN milestones_fired TEXT DEFAULT '[]'"
        )
    for stmt in migrations:
        try:
            await db.execute(stmt)
        except Exception:
            pass
    if migrations:
        await db.commit()
