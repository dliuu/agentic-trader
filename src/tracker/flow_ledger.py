"""Append-only flow ledger for watched tickers (trades.db)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from shared.db import get_db
from tracker.models import LedgerAggregate, LedgerEntry


def _dt(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except (ValueError, TypeError):
        return None


class FlowLedger:
    """Append-only flow record for watched tickers."""

    async def record(self, entry: LedgerEntry) -> None:
        """Write a single flow event. Idempotent on alert_id (INSERT OR IGNORE)."""
        await self.record_batch([entry])

    async def record_batch(self, entries: list[LedgerEntry]) -> None:
        """Write multiple events in one transaction."""
        if not entries:
            return
        db = await get_db()
        try:
            await db.executemany(
                """INSERT OR IGNORE INTO flow_ledger
                   (id, signal_id, alert_id, ticker, strike, expiry, option_type, direction,
                    premium, volume, open_interest, execution_type, underlying_price,
                    implied_volatility, is_same_contract, is_same_expiry, source,
                    created_at, recorded_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        e.id,
                        e.signal_id,
                        e.alert_id,
                        e.ticker,
                        e.strike,
                        e.expiry,
                        e.option_type,
                        e.direction,
                        e.premium,
                        e.volume,
                        e.open_interest,
                        e.execution_type,
                        e.underlying_price,
                        e.implied_volatility,
                        1 if e.is_same_contract else 0,
                        1 if e.is_same_expiry else 0,
                        e.source,
                        e.created_at.isoformat(),
                        e.recorded_at.isoformat(),
                    )
                    for e in entries
                ],
            )
            await db.commit()
        finally:
            await db.close()

    async def get_entries(
        self, signal_id: str, since: datetime | None = None
    ) -> list[LedgerEntry]:
        """All ledger entries for a signal, optionally filtered by created_at > since."""
        db = await get_db()
        try:
            if since is not None:
                cursor = await db.execute(
                    "SELECT * FROM flow_ledger WHERE signal_id = ? "
                    "AND created_at > ? ORDER BY created_at ASC",
                    (signal_id, since.isoformat()),
                )
            else:
                cursor = await db.execute(
                    "SELECT * FROM flow_ledger WHERE signal_id = ? ORDER BY created_at ASC",
                    (signal_id,),
                )
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            out: list[LedgerEntry] = []
            for row in rows:
                d = dict(zip(cols, row))
                out.append(self._row_to_entry(d))
            return out
        finally:
            await db.close()

    async def aggregate(self, signal_id: str) -> LedgerAggregate:
        """Compute summary stats for conviction engine and re-grader."""
        db = await get_db()
        try:
            cursor = await db.execute(
                """
                SELECT
                    COUNT(*) as total_entries,
                    COALESCE(SUM(premium), 0) as total_premium,
                    COUNT(DISTINCT DATE(created_at)) as distinct_days,
                    SUM(CASE WHEN is_same_contract = 1 THEN 1 ELSE 0 END) as same_contract_count,
                    SUM(CASE WHEN is_same_expiry = 1 THEN 1 ELSE 0 END) as same_expiry_count,
                    SUM(CASE WHEN is_same_contract = 0 AND is_same_expiry = 0 THEN 1 ELSE 0 END)
                        as different_expiry_count,
                    COUNT(DISTINCT strike) as distinct_strikes,
                    COUNT(DISTINCT expiry) as distinct_expiries,
                    SUM(CASE WHEN lower(COALESCE(execution_type,'')) = 'sweep' THEN 1 ELSE 0 END)
                        as sweep_count,
                    SUM(CASE WHEN lower(COALESCE(execution_type,'')) = 'block' THEN 1 ELSE 0 END)
                        as block_count,
                    MAX(created_at) as latest_entry_at,
                    MIN(created_at) as earliest_entry_at
                FROM flow_ledger
                WHERE signal_id = ?
                """,
                (signal_id,),
            )
            row = await cursor.fetchone()
            if not row:
                return LedgerAggregate(signal_id=signal_id)
            cols = [d[0] for d in cursor.description]
            d = dict(zip(cols, row))
            return LedgerAggregate(
                signal_id=signal_id,
                total_entries=int(d["total_entries"] or 0),
                total_premium=float(d["total_premium"] or 0),
                distinct_days=int(d["distinct_days"] or 0),
                same_contract_count=int(d["same_contract_count"] or 0),
                same_expiry_count=int(d["same_expiry_count"] or 0),
                different_expiry_count=int(d["different_expiry_count"] or 0),
                distinct_strikes=int(d["distinct_strikes"] or 0),
                distinct_expiries=int(d["distinct_expiries"] or 0),
                sweep_count=int(d["sweep_count"] or 0),
                block_count=int(d["block_count"] or 0),
                latest_entry_at=_dt(d.get("latest_entry_at")),
                earliest_entry_at=_dt(d.get("earliest_entry_at")),
            )
        finally:
            await db.close()

    async def has_alert(self, alert_id: str) -> bool:
        """Check if an alert_id is already recorded (dedup)."""
        db = await get_db()
        try:
            cur = await db.execute(
                "SELECT 1 FROM flow_ledger WHERE alert_id = ? LIMIT 1", (alert_id,)
            )
            row = await cur.fetchone()
            return row is not None
        finally:
            await db.close()

    async def purge_terminal(self, signal_id: str) -> int:
        """Delete entries for a terminal signal. Returns rows deleted."""
        db = await get_db()
        try:
            cur = await db.execute("DELETE FROM flow_ledger WHERE signal_id = ?", (signal_id,))
            await db.commit()
            return cur.rowcount if cur.rowcount is not None else 0
        finally:
            await db.close()

    async def purge_entries_older_than(self, retention_days: int) -> int:
        """Delete ledger rows whose created_at is older than retention_days (all signals)."""
        if retention_days <= 0:
            return 0
        cutoff = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        boundary = (cutoff - timedelta(days=retention_days)).isoformat()
        db = await get_db()
        try:
            cur = await db.execute(
                "DELETE FROM flow_ledger WHERE created_at < ?", (boundary,)
            )
            await db.commit()
            return cur.rowcount if cur.rowcount is not None else 0
        finally:
            await db.close()

    @staticmethod
    def _row_to_entry(row: dict) -> LedgerEntry:
        return LedgerEntry(
            id=row["id"],
            signal_id=row["signal_id"],
            alert_id=row["alert_id"],
            ticker=row["ticker"],
            strike=float(row["strike"]),
            expiry=row["expiry"],
            option_type=row["option_type"],
            direction=row["direction"],
            premium=float(row["premium"]),
            volume=int(row["volume"] or 0),
            open_interest=row.get("open_interest"),
            execution_type=row.get("execution_type"),
            underlying_price=row.get("underlying_price"),
            implied_volatility=row.get("implied_volatility"),
            is_same_contract=bool(row.get("is_same_contract", 0)),
            is_same_expiry=bool(row.get("is_same_expiry", 0)),
            source=row.get("source") or "scanner",
            created_at=_dt(row["created_at"]) or datetime.now(timezone.utc),
            recorded_at=_dt(row["recorded_at"]) or datetime.now(timezone.utc),
        )


def ledger_entry_from_flow_alert(
    alert: Any,
    *,
    signal_id: str,
    signal: Any,
    source: str,
    recorded_at: datetime | None = None,
) -> LedgerEntry:
    """Build LedgerEntry from scanner FlowAlert + target Signal (for flags)."""
    now = recorded_at or datetime.now(timezone.utc)
    option_type = "call" if alert.direction == "bullish" else "put"
    is_same_contract = (
        alert.strike == signal.strike
        and alert.expiry == signal.expiry
        and option_type == signal.option_type
    )
    is_same_expiry = alert.expiry == signal.expiry and not is_same_contract
    created = alert.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return LedgerEntry(
        id=str(uuid.uuid4()),
        signal_id=signal_id,
        alert_id=str(alert.id),
        ticker=alert.ticker.upper(),
        strike=alert.strike,
        expiry=alert.expiry,
        option_type=option_type,
        direction=alert.direction,
        premium=float(alert.total_premium),
        volume=int(alert.total_size),
        open_interest=alert.open_interest,
        execution_type=alert.execution_type,
        underlying_price=alert.underlying_price,
        implied_volatility=alert.implied_volatility,
        is_same_contract=is_same_contract,
        is_same_expiry=is_same_expiry,
        source=source,
        created_at=created,
        recorded_at=now,
    )


def ledger_entry_from_flow_event(
    event: Any,
    *,
    signal_id: str,
    signal: Any,
    source: str,
    recorded_at: datetime | None = None,
) -> LedgerEntry:
    """Persist a FlowWatcher FlowEvent into the ledger (idempotent on alert_id)."""
    now = recorded_at or datetime.now(timezone.utc)
    created = event.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    exec_raw = getattr(event, "fill_type", None) or None
    execution_type = None
    if exec_raw:
        er = str(exec_raw).strip()
        execution_type = er[:1].upper() + er[1:].lower() if len(er) > 1 else er.upper()
    return LedgerEntry(
        id=str(uuid.uuid4()),
        signal_id=signal_id,
        alert_id=str(event.alert_id),
        ticker=str(signal.ticker).upper(),
        strike=float(event.strike),
        expiry=str(event.expiry),
        option_type=str(event.option_type),
        direction=str(signal.direction),
        premium=float(event.premium),
        volume=int(event.volume or 0),
        open_interest=None,
        execution_type=execution_type,
        underlying_price=None,
        implied_volatility=None,
        is_same_contract=bool(event.is_same_contract),
        is_same_expiry=bool(event.is_same_expiry),
        source=source,
        created_at=created,
        recorded_at=now,
    )
