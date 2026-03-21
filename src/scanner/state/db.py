"""SQLite persistence layer.

Stores every candidate the scanner flags, every alert
it sees (for replay/backtesting), and scanner health metrics.
"""
from __future__ import annotations
import json
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
    id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    direction TEXT NOT NULL,
    strike REAL NOT NULL,
    expiry TEXT NOT NULL,
    premium_usd REAL NOT NULL,
    confluence_score REAL NOT NULL,
    dark_pool_confirmation INTEGER NOT NULL DEFAULT 0,
    market_tide_aligned INTEGER NOT NULL DEFAULT 0,
    signals_json TEXT NOT NULL,
    raw_alert_id TEXT NOT NULL,
    scanned_at TEXT NOT NULL,
    graded_at TEXT,
    grade_score REAL,
    executed_at TEXT,
    outcome TEXT
);

CREATE TABLE IF NOT EXISTS raw_alerts (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scan_cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    alerts_received INTEGER NOT NULL,
    candidates_flagged INTEGER NOT NULL,
    errors INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_candidates_ticker ON candidates(ticker);
CREATE INDEX IF NOT EXISTS idx_candidates_scanned ON candidates(scanned_at);
CREATE INDEX IF NOT EXISTS idx_raw_alerts_source ON raw_alerts(source);
"""


class ScannerDB:
    def __init__(self, db_path: str):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db: aiosqlite.Connection | None = None

    async def connect(self):
        self._db = await aiosqlite.connect(str(self._path))
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def save_candidate(self, candidate) -> None:
        await self._db.execute(
            """INSERT OR REPLACE INTO candidates
               (id, ticker, direction, strike, expiry, premium_usd,
                confluence_score, dark_pool_confirmation, market_tide_aligned,
                signals_json, raw_alert_id, scanned_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                candidate.id,
                candidate.ticker,
                candidate.direction,
                candidate.strike,
                candidate.expiry,
                candidate.premium_usd,
                candidate.confluence_score,
                int(candidate.dark_pool_confirmation),
                int(candidate.market_tide_aligned),
                json.dumps([s.model_dump() for s in candidate.signals]),
                candidate.raw_alert_id,
                candidate.scanned_at.isoformat(),
            ),
        )
        await self._db.commit()

    async def save_raw_alert(self, alert_id: str, source: str, payload: dict):
        await self._db.execute(
            "INSERT OR IGNORE INTO raw_alerts (id, source, payload_json, received_at) "
            "VALUES (?, ?, ?, datetime('now'))",
            (alert_id, source, json.dumps(payload)),
        )
        await self._db.commit()

    async def log_cycle(
        self,
        started_at,
        finished_at,
        alerts: int,
        candidates: int,
        errors: int = 0,
    ):
        await self._db.execute(
            "INSERT INTO scan_cycles (started_at, finished_at, alerts_received, "
            "candidates_flagged, errors) VALUES (?, ?, ?, ?, ?)",
            (
                started_at.isoformat(),
                finished_at.isoformat(),
                alerts,
                candidates,
                errors,
            ),
        )
        await self._db.commit()

    async def close(self):
        if self._db:
            await self._db.close()
