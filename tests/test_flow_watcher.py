"""Tests for FlowWatcher."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import respx

from tracker.flow_watcher import FlowWatcher, UW_BASE
from tracker.models import Signal, SignalState


def _make_signal(**overrides) -> Signal:
    defaults = dict(
        id="sig-001", ticker="ACME", strike=50.0, expiry="2025-08-15",
        option_type="call", direction="bullish", state=SignalState.PENDING,
        initial_score=82, initial_premium=50000, initial_oi=100,
        initial_volume=500, grade_id="g1", conviction_score=82.0,
        created_at=datetime.now(timezone.utc) - timedelta(hours=2),
        last_polled_at=datetime.now(timezone.utc) - timedelta(minutes=10),
    )
    defaults.update(overrides)
    return Signal(**defaults)


def _flow_response(events: list[dict] | None = None) -> dict:
    if events is None:
        events = [
            {
                "id": "flow-new-001",
                "ticker": "ACME",
                "type": "call",
                "strike": 50.0,
                "expiry": "2025-08-15",
                "total_premium": 30000,
                "total_size": 200,
                "has_sweep": True,
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
            {
                "id": "flow-old-001",
                "ticker": "ACME",
                "type": "call",
                "strike": 55.0,
                "expiry": "2025-08-15",
                "total_premium": 15000,
                "total_size": 100,
                "has_sweep": False,
                "created_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
            },
        ]
    return {"data": events}


class TestFlowWatcher:
    @respx.mock
    async def test_finds_new_flow(self):
        respx.get(f"{UW_BASE}/api/option-trades/flow-alerts").mock(
            return_value=httpx.Response(200, json=_flow_response())
        )
        async with httpx.AsyncClient() as client:
            watcher = FlowWatcher(client, "fake-token")
            result = await watcher.check(_make_signal())

        assert len(result.events) >= 1
        assert result.events[0].alert_id == "flow-new-001"

    @respx.mock
    async def test_same_contract_detection(self):
        respx.get(f"{UW_BASE}/api/option-trades/flow-alerts").mock(
            return_value=httpx.Response(200, json=_flow_response())
        )
        async with httpx.AsyncClient() as client:
            watcher = FlowWatcher(client, "fake-token")
            result = await watcher.check(_make_signal())

        same_contract = [e for e in result.events if e.is_same_contract]
        # flow-new-001 is same contract (strike=50, expiry=2025-08-15, call)
        assert len(same_contract) >= 1

    @respx.mock
    async def test_no_flow_returns_empty(self):
        respx.get(f"{UW_BASE}/api/option-trades/flow-alerts").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        async with httpx.AsyncClient() as client:
            watcher = FlowWatcher(client, "fake-token")
            result = await watcher.check(_make_signal())

        assert len(result.events) == 0

    @respx.mock
    async def test_api_error_returns_empty(self):
        respx.get(f"{UW_BASE}/api/option-trades/flow-alerts").mock(
            return_value=httpx.Response(500)
        )
        async with httpx.AsyncClient() as client:
            watcher = FlowWatcher(client, "fake-token")
            result = await watcher.check(_make_signal())

        assert len(result.events) == 0
