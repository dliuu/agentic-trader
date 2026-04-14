"""SQLite persistence for signals and snapshots."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import aiosqlite
import structlog

from shared.db import _ensure_tables, get_db
from tracker.models import (
    ACTIVE_STATES,
    MONITORING_STATES,
    Signal,
    SignalSnapshot,
    SignalState,
)

log = structlog.get_logger()


def _parse_milestones_fired(raw: object) -> list[str]:
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
            return [str(x) for x in data] if isinstance(data, list) else []
        except (json.JSONDecodeError, TypeError):
            return []
    return []


class SignalStore:
    """CRUD operations for the signals and signal_snapshots tables."""

    def __init__(self, db_path: str | None = None):
        """If ``db_path`` is set, all operations use that SQLite file (e.g. replay)."""
        self._db_path = db_path

    async def _connect(self) -> aiosqlite.Connection:
        if self._db_path:
            p = Path(self._db_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            db = await aiosqlite.connect(str(p))
            await db.execute("PRAGMA journal_mode=WAL")
            await _ensure_tables(db)
            return db
        return await get_db()

    async def create_signal(self, signal: Signal) -> None:
        """Insert a new signal. Called by signal_intake when a ScoredTrade arrives."""
        db = await self._connect()
        try:
            await db.execute(
                """INSERT INTO signals
                   (id, ticker, strike, expiry, option_type, direction, state,
                    initial_score, initial_premium, initial_oi, initial_volume,
                    initial_contract_adv, grade_id, conviction_score,
                    snapshots_taken, confirming_flows, oi_high_water,
                    chain_spread_count, cumulative_premium, days_without_flow,
                    created_at, last_polled_at, last_flow_at, matured_at,
                    terminal_at, terminal_reason, risk_params_json,
                    anomaly_fingerprint, regrade_count, last_regraded_at, milestones_fired)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    signal.id,
                    signal.ticker,
                    signal.strike,
                    signal.expiry,
                    signal.option_type,
                    signal.direction,
                    signal.state.value,
                    signal.initial_score,
                    signal.initial_premium,
                    signal.initial_oi,
                    signal.initial_volume,
                    signal.initial_contract_adv,
                    signal.grade_id,
                    signal.conviction_score,
                    signal.snapshots_taken,
                    signal.confirming_flows,
                    signal.oi_high_water,
                    signal.chain_spread_count,
                    signal.cumulative_premium,
                    signal.days_without_flow,
                    signal.created_at.isoformat(),
                    signal.last_polled_at.isoformat() if signal.last_polled_at else None,
                    signal.last_flow_at.isoformat() if signal.last_flow_at else None,
                    signal.matured_at.isoformat() if signal.matured_at else None,
                    signal.terminal_at.isoformat() if signal.terminal_at else None,
                    signal.terminal_reason,
                    signal.risk_params_json,
                    signal.anomaly_fingerprint,
                    signal.regrade_count,
                    signal.last_regraded_at.isoformat() if signal.last_regraded_at else None,
                    json.dumps(signal.milestones_fired),
                ),
            )
            await db.commit()
            log.info("signal.created", signal_id=signal.id, ticker=signal.ticker)
        finally:
            await db.close()

    async def get_active_signals(self) -> list[Signal]:
        """Return all signals in pending or accumulating state."""
        db = await self._connect()
        try:
            placeholders = ",".join(f"'{s.value}'" for s in ACTIVE_STATES)
            cursor = await db.execute(
                f"SELECT * FROM signals WHERE state IN ({placeholders}) "
                "ORDER BY created_at ASC"
            )
            rows = await cursor.fetchall()
            columns = [d[0] for d in cursor.description]
            return [self._row_to_signal(dict(zip(columns, row))) for row in rows]
        finally:
            await db.close()

    async def get_signal(self, signal_id: str) -> Signal | None:
        """Fetch a single signal by ID."""
        db = await self._connect()
        try:
            cursor = await db.execute(
                "SELECT * FROM signals WHERE id = ?", (signal_id,)
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            columns = [d[0] for d in cursor.description]
            return self._row_to_signal(dict(zip(columns, row)))
        finally:
            await db.close()

    async def count_active(self) -> int:
        """Count signals in active states."""
        db = await self._connect()
        try:
            placeholders = ",".join(f"'{s.value}'" for s in ACTIVE_STATES)
            cursor = await db.execute(
                f"SELECT COUNT(*) FROM signals WHERE state IN ({placeholders})"
            )
            row = await cursor.fetchone()
            return row[0] if row else 0
        finally:
            await db.close()

    async def update_signal(
        self,
        signal_id: str,
        **fields,
    ) -> None:
        """Update specific fields on a signal.

        Accepts any column name as a keyword argument. Datetime values
        are auto-converted to ISO strings. SignalState values are
        converted to their string value.
        """
        if not fields:
            return

        set_clauses = []
        values = []
        for key, val in fields.items():
            set_clauses.append(f"{key} = ?")
            if isinstance(val, datetime):
                values.append(val.isoformat())
            elif isinstance(val, SignalState):
                values.append(val.value)
            elif isinstance(val, list):
                values.append(json.dumps(val))
            else:
                values.append(val)
        values.append(signal_id)

        db = await self._connect()
        try:
            await db.execute(
                f"UPDATE signals SET {', '.join(set_clauses)} WHERE id = ?",
                tuple(values),
            )
            await db.commit()
        finally:
            await db.close()

    async def add_snapshot(self, snapshot: SignalSnapshot) -> None:
        """Insert a signal snapshot."""
        db = await self._connect()
        try:
            await db.execute(
                """INSERT INTO signal_snapshots
                   (id, signal_id, snapshot_at, contract_oi, contract_volume,
                    contract_bid, contract_ask, contract_spread_pct, spot_price,
                    neighbor_oi_total, neighbor_strikes_active, neighbor_put_call_ratio,
                    new_flow_count, new_flow_premium, new_flow_same_contract,
                    new_flow_same_expiry, conviction_delta, conviction_after,
                    signals_fired, notes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    snapshot.id,
                    snapshot.signal_id,
                    snapshot.snapshot_at.isoformat(),
                    snapshot.contract_oi,
                    snapshot.contract_volume,
                    snapshot.contract_bid,
                    snapshot.contract_ask,
                    snapshot.contract_spread_pct,
                    snapshot.spot_price,
                    snapshot.neighbor_oi_total,
                    snapshot.neighbor_strikes_active,
                    snapshot.neighbor_put_call_ratio,
                    snapshot.new_flow_count,
                    snapshot.new_flow_premium,
                    snapshot.new_flow_same_contract,
                    snapshot.new_flow_same_expiry,
                    snapshot.conviction_delta,
                    snapshot.conviction_after,
                    json.dumps(snapshot.signals_fired),
                    snapshot.notes,
                ),
            )
            await db.commit()
        finally:
            await db.close()

    async def get_snapshots(
        self, signal_id: str, limit: int = 100
    ) -> list[SignalSnapshot]:
        """Fetch snapshots for a signal, most recent first."""
        db = await self._connect()
        try:
            cursor = await db.execute(
                "SELECT * FROM signal_snapshots WHERE signal_id = ? "
                "ORDER BY snapshot_at DESC LIMIT ?",
                (signal_id, limit),
            )
            rows = await cursor.fetchall()
            columns = [d[0] for d in cursor.description]
            return [self._row_to_snapshot(dict(zip(columns, row))) for row in rows]
        finally:
            await db.close()

    async def get_latest_snapshot(self, signal_id: str) -> SignalSnapshot | None:
        """Fetch the most recent snapshot for a signal."""
        snapshots = await self.get_snapshots(signal_id, limit=1)
        return snapshots[0] if snapshots else None

    async def get_regrade_history(self, signal_id: str, limit: int = 50) -> list[dict]:
        """Rows from `regrades` for audit / debugging."""
        db = await self._connect()
        try:
            cursor = await db.execute(
                "SELECT * FROM regrades WHERE signal_id = ? ORDER BY regraded_at DESC LIMIT ?",
                (signal_id, limit),
            )
            rows = await cursor.fetchall()
            columns = [d[0] for d in cursor.description]
            return [dict(zip(columns, row)) for row in rows]
        finally:
            await db.close()

    async def check_duplicate_signal(self, ticker: str, strike: float, expiry: str) -> bool:
        """Check if an active signal already exists for this contract."""
        db = await self._connect()
        try:
            placeholders = ",".join(f"'{s.value}'" for s in ACTIVE_STATES)
            cursor = await db.execute(
                f"SELECT COUNT(*) FROM signals "
                f"WHERE ticker = ? AND strike = ? AND expiry = ? "
                f"AND state IN ({placeholders})",
                (ticker, strike, expiry),
            )
            row = await cursor.fetchone()
            return (row[0] if row else 0) > 0
        finally:
            await db.close()

    def _monitoring_state_placeholders(self) -> tuple[str, tuple[str, ...]]:
        states = tuple(sorted(s.value for s in MONITORING_STATES))
        ph = ",".join("?" * len(states))
        return ph, states

    async def get_watched_tickers(self) -> set[str]:
        """Tickers with a non-terminal monitored signal (pending, accumulating, or actionable)."""
        db = await self._connect()
        try:
            ph, states = self._monitoring_state_placeholders()
            cursor = await db.execute(
                f"SELECT DISTINCT ticker FROM signals WHERE state IN ({ph})",
                states,
            )
            rows = await cursor.fetchall()
            return {str(row[0]).upper() for row in rows if row[0]}
        finally:
            await db.close()

    async def get_ticker_signal_map(self) -> dict[str, str]:
        """{ticker_upper: signal_id} for monitored signals; most recent signal wins per ticker."""
        db = await self._connect()
        try:
            ph, states = self._monitoring_state_placeholders()
            cursor = await db.execute(
                f"SELECT ticker, id FROM signals WHERE state IN ({ph}) ORDER BY created_at DESC",
                states,
            )
            rows = await cursor.fetchall()
            out: dict[str, str] = {}
            for ticker, sid in rows:
                u = str(ticker).upper() if ticker else ""
                if u and u not in out:
                    out[u] = sid
            return out
        finally:
            await db.close()

    async def has_active_signal_for_ticker(self, ticker: str) -> bool:
        """True if any monitored signal already exists for this ticker (one signal per ticker pilot)."""
        db = await self._connect()
        try:
            ph, states = self._monitoring_state_placeholders()
            cursor = await db.execute(
                f"SELECT COUNT(*) FROM signals WHERE ticker = ? AND state IN ({ph})",
                (ticker.upper(),) + states,
            )
            row = await cursor.fetchone()
            return (row[0] if row else 0) > 0
        finally:
            await db.close()

    # --- Row mappers ---

    @staticmethod
    def _row_to_signal(row: dict) -> Signal:
        """Convert a SQLite row dict to a Signal model."""
        return Signal(
            id=row["id"],
            ticker=row["ticker"],
            strike=row["strike"],
            expiry=row["expiry"],
            option_type=row["option_type"],
            direction=row["direction"],
            state=SignalState(row["state"]),
            initial_score=row["initial_score"],
            initial_premium=row["initial_premium"],
            initial_oi=row["initial_oi"],
            initial_volume=row["initial_volume"],
            initial_contract_adv=row.get("initial_contract_adv", 0),
            grade_id=row["grade_id"],
            conviction_score=row["conviction_score"],
            snapshots_taken=row.get("snapshots_taken", 0),
            confirming_flows=row.get("confirming_flows", 0),
            oi_high_water=row.get("oi_high_water", 0),
            chain_spread_count=row.get("chain_spread_count", 0),
            cumulative_premium=row.get("cumulative_premium", 0.0),
            days_without_flow=row.get("days_without_flow", 0),
            created_at=datetime.fromisoformat(row["created_at"]),
            last_polled_at=(
                datetime.fromisoformat(row["last_polled_at"])
                if row.get("last_polled_at") else None
            ),
            last_flow_at=(
                datetime.fromisoformat(row["last_flow_at"])
                if row.get("last_flow_at") else None
            ),
            matured_at=(
                datetime.fromisoformat(row["matured_at"])
                if row.get("matured_at") else None
            ),
            terminal_at=(
                datetime.fromisoformat(row["terminal_at"])
                if row.get("terminal_at") else None
            ),
            terminal_reason=row.get("terminal_reason"),
            risk_params_json=row.get("risk_params_json"),
            anomaly_fingerprint=row.get("anomaly_fingerprint", ""),
            regrade_count=int(row.get("regrade_count", 0)),
            last_regraded_at=(
                datetime.fromisoformat(row["last_regraded_at"])
                if row.get("last_regraded_at")
                else None
            ),
            milestones_fired=_parse_milestones_fired(row.get("milestones_fired")),
        )

    @staticmethod
    def _row_to_snapshot(row: dict) -> SignalSnapshot:
        fired = row.get("signals_fired")
        if isinstance(fired, str):
            try:
                fired = json.loads(fired)
            except (json.JSONDecodeError, TypeError):
                fired = []
        return SignalSnapshot(
            id=row["id"],
            signal_id=row["signal_id"],
            snapshot_at=datetime.fromisoformat(row["snapshot_at"]),
            contract_oi=row.get("contract_oi"),
            contract_volume=row.get("contract_volume"),
            contract_bid=row.get("contract_bid"),
            contract_ask=row.get("contract_ask"),
            contract_spread_pct=row.get("contract_spread_pct"),
            spot_price=row.get("spot_price"),
            neighbor_oi_total=row.get("neighbor_oi_total"),
            neighbor_strikes_active=row.get("neighbor_strikes_active"),
            neighbor_put_call_ratio=row.get("neighbor_put_call_ratio"),
            new_flow_count=row.get("new_flow_count", 0),
            new_flow_premium=row.get("new_flow_premium", 0.0),
            new_flow_same_contract=row.get("new_flow_same_contract", 0),
            new_flow_same_expiry=row.get("new_flow_same_expiry", 0),
            conviction_delta=row.get("conviction_delta", 0.0),
            conviction_after=row.get("conviction_after", 0.0),
            signals_fired=fired or [],
            notes=row.get("notes"),
        )
