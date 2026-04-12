"""Tests for SignalStore SQLite persistence."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from tracker.models import Signal, SignalSnapshot, SignalState
from tracker.signal_store import SignalStore


def _make_signal(**overrides) -> Signal:
    defaults = dict(
        id=str(uuid.uuid4()),
        ticker="ACME",
        strike=50.0,
        expiry="2025-08-15",
        option_type="call",
        direction="bullish",
        state=SignalState.PENDING,
        initial_score=82,
        initial_premium=75000.0,
        initial_oi=100,
        initial_volume=500,
        initial_contract_adv=8,
        grade_id="grade-001",
        conviction_score=82.0,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    return Signal(**defaults)


@pytest.fixture(autouse=True)
def _isolated_trades_db(tmp_path, monkeypatch):
    """Part 1 tests expect a fresh DB; shared.data/trades.db accumulates rows across runs."""
    monkeypatch.setattr("shared.db.DB_PATH", tmp_path / "trades_test.db")


class TestSignalStore:
    async def test_create_and_get(self):
        store = SignalStore()
        sig = _make_signal()
        await store.create_signal(sig)
        fetched = await store.get_signal(sig.id)
        assert fetched is not None
        assert fetched.ticker == "ACME"
        assert fetched.state == SignalState.PENDING
        assert fetched.conviction_score == 82.0

    async def test_get_active_signals(self):
        store = SignalStore()
        s1 = _make_signal(state=SignalState.PENDING)
        s2 = _make_signal(state=SignalState.ACCUMULATING)
        s3 = _make_signal(state=SignalState.EXPIRED)
        await store.create_signal(s1)
        await store.create_signal(s2)
        await store.create_signal(s3)
        active = await store.get_active_signals()
        ids = {s.id for s in active}
        assert s1.id in ids
        assert s2.id in ids
        assert s3.id not in ids

    async def test_update_signal(self):
        store = SignalStore()
        sig = _make_signal()
        await store.create_signal(sig)
        await store.update_signal(
            sig.id,
            conviction_score=91.0,
            state=SignalState.ACTIONABLE,
            confirming_flows=3,
        )
        fetched = await store.get_signal(sig.id)
        assert fetched.conviction_score == 91.0
        assert fetched.state == SignalState.ACTIONABLE
        assert fetched.confirming_flows == 3

    async def test_count_active(self):
        store = SignalStore()
        await store.create_signal(_make_signal(state=SignalState.PENDING))
        await store.create_signal(_make_signal(state=SignalState.ACCUMULATING))
        await store.create_signal(_make_signal(state=SignalState.DECAYED))
        assert await store.count_active() == 2

    async def test_duplicate_check(self):
        store = SignalStore()
        await store.create_signal(_make_signal(ticker="ACME", strike=50.0, expiry="2025-08-15"))
        assert await store.check_duplicate_signal("ACME", 50.0, "2025-08-15") is True
        assert await store.check_duplicate_signal("ACME", 55.0, "2025-08-15") is False
        assert await store.check_duplicate_signal("OTHER", 50.0, "2025-08-15") is False

    async def test_add_and_get_snapshot(self):
        store = SignalStore()
        sig = _make_signal()
        await store.create_signal(sig)
        snap = SignalSnapshot(
            id=str(uuid.uuid4()),
            signal_id=sig.id,
            snapshot_at=datetime.now(timezone.utc),
            contract_oi=150,
            contract_volume=200,
            spot_price=42.0,
            conviction_delta=5.0,
            conviction_after=87.0,
            signals_fired=["oi_increase_50pct"],
        )
        await store.add_snapshot(snap)
        latest = await store.get_latest_snapshot(sig.id)
        assert latest is not None
        assert latest.contract_oi == 150
        assert latest.conviction_after == 87.0
        assert "oi_increase_50pct" in latest.signals_fired
