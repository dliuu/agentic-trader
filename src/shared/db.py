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
        """
    )
    await db.commit()
