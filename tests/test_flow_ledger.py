"""Tests for flow ledger (append-only, dedup, aggregate)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from scanner.models.flow_alert import FlowAlert
from shared.db import get_db
from tracker.flow_ledger import FlowLedger, ledger_entry_from_flow_alert
from tracker.models import LedgerAggregate, LedgerEntry, Signal, SignalState


@pytest.fixture(autouse=True)
def _ledger_db(tmp_path, monkeypatch):
    monkeypatch.setattr("shared.db.DB_PATH", tmp_path / "ledger_trades.db")


def _sig(**kw) -> Signal:
    d = dict(
        id=str(uuid.uuid4()),
        ticker="ACME",
        strike=50.0,
        expiry="2026-12-20",
        option_type="call",
        direction="bullish",
        state=SignalState.PENDING,
        initial_score=80,
        initial_premium=50_000.0,
        initial_oi=100,
        initial_volume=200,
        grade_id="g-1",
        conviction_score=80.0,
        created_at=datetime.now(timezone.utc),
    )
    d.update(kw)
    return Signal(**d)


def _entry(
    signal_id: str,
    alert_id: str,
    *,
    strike: float = 50.0,
    expiry: str = "2026-12-20",
    premium: float = 10_000.0,
    created: datetime | None = None,
    is_same_contract: bool = True,
    is_same_expiry: bool = False,
    execution_type: str | None = "Sweep",
) -> LedgerEntry:
    now = datetime.now(timezone.utc)
    c = created or now
    if c.tzinfo is None:
        c = c.replace(tzinfo=timezone.utc)
    return LedgerEntry(
        id=str(uuid.uuid4()),
        signal_id=signal_id,
        alert_id=alert_id,
        ticker="ACME",
        strike=strike,
        expiry=expiry,
        option_type="call",
        direction="bullish",
        premium=premium,
        volume=100,
        execution_type=execution_type,
        is_same_contract=is_same_contract,
        is_same_expiry=is_same_expiry,
        source="scanner",
        created_at=c,
        recorded_at=now,
    )


@pytest.mark.asyncio
class TestFlowLedger:
    async def test_record_batch_and_aggregate(self):
        sig = _sig()
        db = await get_db()
        await db.execute(
            """INSERT INTO signals
            (id, ticker, strike, expiry, option_type, direction, state,
             initial_score, initial_premium, initial_oi, initial_volume,
             initial_contract_adv, grade_id, conviction_score,
             snapshots_taken, confirming_flows, oi_high_water,
             chain_spread_count, cumulative_premium, days_without_flow,
             created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                sig.id,
                sig.ticker,
                sig.strike,
                sig.expiry,
                sig.option_type,
                sig.direction,
                sig.state.value,
                sig.initial_score,
                sig.initial_premium,
                sig.initial_oi,
                sig.initial_volume,
                sig.initial_contract_adv,
                sig.grade_id,
                sig.conviction_score,
                0,
                0,
                sig.initial_oi,
                0,
                sig.cumulative_premium,
                0,
                sig.created_at.isoformat(),
            ),
        )
        await db.commit()
        await db.close()

        ledger = FlowLedger()
        d1 = datetime(2026, 1, 2, 12, 0, 0, tzinfo=timezone.utc)
        d2 = datetime(2026, 1, 5, 12, 0, 0, tzinfo=timezone.utc)
        await ledger.record_batch(
            [
                _entry(sig.id, "a1", created=d1, strike=50.0, is_same_contract=True),
                _entry(sig.id, "a2", created=d2, strike=55.0, is_same_contract=False, is_same_expiry=True),
                _entry(sig.id, "a3", created=d2, strike=50.0, expiry="2027-01-15", is_same_contract=False),
            ]
        )
        agg = await ledger.aggregate(sig.id)
        assert isinstance(agg, LedgerAggregate)
        assert agg.signal_id == sig.id
        assert agg.total_entries == 3
        assert agg.total_premium == pytest.approx(30_000.0)
        assert agg.distinct_days >= 2
        assert agg.same_contract_count == 1
        assert agg.same_expiry_count == 1
        assert agg.sweep_count >= 1

    async def test_insert_or_ignore_duplicate_alert_id(self):
        sig = _sig(id="sig-dedup")
        db = await get_db()
        await db.execute(
            """INSERT INTO signals
            (id, ticker, strike, expiry, option_type, direction, state,
             initial_score, initial_premium, initial_oi, initial_volume,
             initial_contract_adv, grade_id, conviction_score,
             snapshots_taken, confirming_flows, oi_high_water,
             chain_spread_count, cumulative_premium, days_without_flow,
             created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                sig.id,
                sig.ticker,
                sig.strike,
                sig.expiry,
                sig.option_type,
                sig.direction,
                sig.state.value,
                sig.initial_score,
                sig.initial_premium,
                sig.initial_oi,
                sig.initial_volume,
                sig.initial_contract_adv,
                sig.grade_id,
                sig.conviction_score,
                0,
                0,
                sig.initial_oi,
                0,
                sig.cumulative_premium,
                0,
                sig.created_at.isoformat(),
            ),
        )
        await db.commit()
        await db.close()

        ledger = FlowLedger()
        e = _entry(sig.id, "same-alert", premium=1000.0)
        await ledger.record(e)
        await ledger.record(e)
        agg = await ledger.aggregate(sig.id)
        assert agg.total_entries == 1

    async def test_has_alert(self):
        sig = _sig(id="sig-has")
        db = await get_db()
        await db.execute(
            """INSERT INTO signals
            (id, ticker, strike, expiry, option_type, direction, state,
             initial_score, initial_premium, initial_oi, initial_volume,
             initial_contract_adv, grade_id, conviction_score,
             snapshots_taken, confirming_flows, oi_high_water,
             chain_spread_count, cumulative_premium, days_without_flow,
             created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                sig.id,
                sig.ticker,
                sig.strike,
                sig.expiry,
                sig.option_type,
                sig.direction,
                sig.state.value,
                sig.initial_score,
                sig.initial_premium,
                sig.initial_oi,
                sig.initial_volume,
                sig.initial_contract_adv,
                sig.grade_id,
                sig.conviction_score,
                0,
                0,
                sig.initial_oi,
                0,
                sig.cumulative_premium,
                0,
                sig.created_at.isoformat(),
            ),
        )
        await db.commit()
        await db.close()

        ledger = FlowLedger()
        assert await ledger.has_alert("x") is False
        await ledger.record(_entry(sig.id, "aid-1", premium=1.0))
        assert await ledger.has_alert("aid-1") is True

    async def test_purge_terminal(self):
        sig = _sig(id="sig-purge")
        db = await get_db()
        await db.execute(
            """INSERT INTO signals
            (id, ticker, strike, expiry, option_type, direction, state,
             initial_score, initial_premium, initial_oi, initial_volume,
             initial_contract_adv, grade_id, conviction_score,
             snapshots_taken, confirming_flows, oi_high_water,
             chain_spread_count, cumulative_premium, days_without_flow,
             created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                sig.id,
                sig.ticker,
                sig.strike,
                sig.expiry,
                sig.option_type,
                sig.direction,
                sig.state.value,
                sig.initial_score,
                sig.initial_premium,
                sig.initial_oi,
                sig.initial_volume,
                sig.initial_contract_adv,
                sig.grade_id,
                sig.conviction_score,
                0,
                0,
                sig.initial_oi,
                0,
                sig.cumulative_premium,
                0,
                sig.created_at.isoformat(),
            ),
        )
        await db.commit()
        await db.close()

        ledger = FlowLedger()
        await ledger.record(_entry(sig.id, "z1", premium=1.0))
        n = await ledger.purge_terminal(sig.id)
        assert n >= 1
        agg = await ledger.aggregate(sig.id)
        assert agg.total_entries == 0


def test_ledger_entry_from_flow_alert_flags():
    sig = _sig()
    raw = {
        "id": "uw-99",
        "ticker": "ACME",
        "type": "call",
        "strike": 55.0,
        "expiry": "2026-12-20",
        "total_premium": 12000.0,
        "total_size": 50,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    alert = FlowAlert.model_validate(raw)
    le = ledger_entry_from_flow_alert(alert, signal_id=sig.id, signal=sig, source="scanner")
    assert le.alert_id == "uw-99"
    assert le.is_same_contract is False
    assert le.is_same_expiry is True
