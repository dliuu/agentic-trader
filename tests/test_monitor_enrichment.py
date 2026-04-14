"""Integration tests for monitor enrichment wiring (ledger/news/regrader are optional)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from tracker.chain_poller import ChainPoller
from tracker.conviction import ConvictionEngine
from tracker.models import (
    ChainPollResult,
    FlowEvent,
    FlowWatchResult,
    NewsEventType,
    NewsWatchResult,
    Signal,
    SignalSnapshot,
    SignalState,
)
from tracker.monitor import _process_signal


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DummyStore:
    def __init__(self) -> None:
        self.snapshots: list[SignalSnapshot] = []
        self.updated: dict[str, Any] = {}

    async def get_latest_snapshot(self, signal_id: str) -> SignalSnapshot | None:
        return None

    async def add_snapshot(self, snapshot: SignalSnapshot) -> None:
        self.snapshots.append(snapshot)

    async def update_signal(self, signal_id: str, **fields) -> None:
        self.updated = dict(fields)

    async def get_signal(self, signal_id: str) -> Signal | None:
        return None

    async def get_snapshots(self, signal_id: str, limit: int = 100) -> list[SignalSnapshot]:
        return []


class DummyPoller:
    async def poll(self, signal: Signal, prev: SignalSnapshot | None) -> ChainPollResult:
        return ChainPollResult(
            ticker=signal.ticker,
            polled_at=_now(),
            contract_oi=300,
            contract_found=True,
            contract_bid=2.0,
            contract_ask=2.2,
            spot_price=signal.strike - 1,
            neighbor_strikes=[],
            adjacent_expiry_oi=[],
        )


class DummyWatcher:
    async def check(self, signal: Signal) -> FlowWatchResult:
        return FlowWatchResult(
            ticker=signal.ticker,
            checked_at=_now(),
            events=[
                FlowEvent(
                    alert_id="a1",
                    strike=signal.strike,
                    expiry=signal.expiry,
                    option_type=signal.option_type,
                    premium=60_000.0,
                    volume=10,
                    fill_type="sweep",
                    is_same_contract=True,
                    is_same_expiry=False,
                    created_at=_now(),
                )
            ],
        )


class DummyLedger:
    def __init__(self) -> None:
        self.recorded = 0

    async def record(self, entry) -> None:
        self.recorded += 1

    async def aggregate(self, signal_id: str):
        from tracker.models import LedgerAggregate

        return LedgerAggregate(
            signal_id=signal_id,
            total_entries=3,
            total_premium=200_000.0,
            distinct_days=2,
            same_contract_count=2,
            same_expiry_count=1,
            different_expiry_count=0,
            distinct_strikes=3,
            distinct_expiries=1,
            sweep_count=2,
            block_count=0,
            latest_entry_at=_now() - timedelta(hours=1),
            earliest_entry_at=_now() - timedelta(days=2),
        )

    async def purge_terminal(self, signal_id: str) -> int:
        return 0


class DummyNewsWatcher:
    async def check(self, signal: Signal) -> NewsWatchResult:
        return NewsWatchResult(
            signal_id=signal.id,
            ticker=signal.ticker,
            checked_at=_now(),
            events=[],
            has_catalyst=False,
            catalyst_types=[],
            filing_detected=False,
            regrade_recommended=False,
        )

    async def persist_events(self, events) -> None:
        return None


class DummyRegrader:
    async def maybe_regrade(
        self,
        signal,
        chain,
        flow,
        news,
        ledger_agg,
        deterministic_conviction,
        *,
        signal_for_milestones=None,
    ):
        from tracker.models import RegradeResult

        return RegradeResult(
            signal_id=signal.id,
            triggered=True,
            trigger_reason="premium_2x",
            synthesis_score=90,
            deterministic_conviction=deterministic_conviction,
            blended_conviction=92.0,
            regraded_at=_now(),
        )


@pytest.mark.asyncio
async def test_process_signal_blend_re_evaluates_state(monkeypatch):
    store = DummyStore()
    poller = DummyPoller()
    watcher = DummyWatcher()
    engine = ConvictionEngine()

    signal = Signal(
        id="sig1",
        ticker="ACME",
        strike=50.0,
        expiry=(_now().date() + timedelta(days=30)).isoformat(),
        option_type="call",
        direction="bullish",
        state=SignalState.ACCUMULATING,
        initial_score=80,
        initial_premium=50_000.0,
        initial_oi=100,
        initial_volume=100,
        grade_id="g1",
        conviction_score=88.0,
        confirming_flows=1,
        created_at=_now() - timedelta(days=2),
    )

    q: asyncio.Queue[Signal] = asyncio.Queue()

    await _process_signal(
        signal,
        store,  # type: ignore[arg-type]
        poller,  # type: ignore[arg-type]
        watcher,  # type: ignore[arg-type]
        DummyNewsWatcher(),  # type: ignore[arg-type]
        DummyRegrader(),  # type: ignore[arg-type]
        engine,
        q,
        engine._cfg,  # TrackerConfig default
        DummyLedger(),  # type: ignore[arg-type]
    )

    assert store.snapshots, "snapshot should be persisted"
    assert store.updated.get("conviction_score") == 92.0
    assert store.updated.get("state") == SignalState.ACTIONABLE
    assert store.updated.get("regrade_count") == 1
    assert store.updated.get("milestones_fired") == ["premium_2x"]


@pytest.mark.asyncio
async def test_enrichment_failures_degrade(monkeypatch):
    class BadLedger(DummyLedger):
        async def record(self, entry) -> None:
            raise RuntimeError("ledger down")

        async def aggregate(self, signal_id: str):
            raise RuntimeError("agg down")

    class BadNews(DummyNewsWatcher):
        async def check(self, signal: Signal) -> NewsWatchResult:
            raise RuntimeError("news down")

    class BadRegrader(DummyRegrader):
        async def maybe_regrade(self, *args, **kwargs):
            raise RuntimeError("llm down")

    store = DummyStore()
    poller = DummyPoller()
    watcher = DummyWatcher()
    engine = ConvictionEngine()
    signal = Signal(
        id="sig2",
        ticker="ACME",
        strike=50.0,
        expiry=(_now().date() + timedelta(days=30)).isoformat(),
        option_type="call",
        direction="bullish",
        state=SignalState.ACCUMULATING,
        initial_score=80,
        initial_premium=50_000.0,
        initial_oi=100,
        initial_volume=100,
        grade_id="g1",
        conviction_score=80.0,
        created_at=_now() - timedelta(days=2),
    )
    q: asyncio.Queue[Signal] = asyncio.Queue()

    await _process_signal(
        signal,
        store,  # type: ignore[arg-type]
        poller,  # type: ignore[arg-type]
        watcher,  # type: ignore[arg-type]
        BadNews(),  # type: ignore[arg-type]
        BadRegrader(),  # type: ignore[arg-type]
        engine,
        q,
        engine._cfg,
        BadLedger(),  # type: ignore[arg-type]
    )

    assert store.snapshots
    assert "conviction_score" in store.updated
