"""Tests for ChainPoller."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import respx

from tracker.chain_poller import ChainPoller, UW_BASE
from tracker.config import TrackerConfig
from tracker.models import Signal, SignalState


def _make_signal(**overrides) -> Signal:
    defaults = dict(
        id="sig-001", ticker="ACME", strike=50.0, expiry="2025-08-15",
        option_type="call", direction="bullish", state=SignalState.PENDING,
        initial_score=82, initial_premium=50000, initial_oi=100,
        initial_volume=500, grade_id="g1", conviction_score=82.0,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    return Signal(**defaults)


def _chain_response() -> dict:
    """Mock /api/stock/ACME/option-chains response with contracts around strike 50."""
    contracts = []
    for strike in [40.0, 45.0, 50.0, 55.0, 60.0]:
        for opt in ["call", "put"]:
            contracts.append({
                "expiry": "2025-08-15",
                "strike": strike,
                "option_type": opt,
                "open_interest": 100 if strike == 50.0 and opt == "call" else 20,
                "volume": 50 if strike == 50.0 else 5,
                "bid": 2.10,
                "ask": 2.30,
                "underlying_price": 48.50,
                "implied_volatility": 0.45,
            })
    # Adjacent expiry
    contracts.append({
        "expiry": "2025-08-22",
        "strike": 50.0,
        "option_type": "call",
        "open_interest": 30,
        "volume": 10,
        "bid": 3.0,
        "ask": 3.40,
        "underlying_price": 48.50,
    })
    return {"data": contracts}


class TestChainPoller:
    @respx.mock
    async def test_basic_poll(self):
        respx.get(f"{UW_BASE}/api/stock/ACME/option-chains").mock(
            return_value=httpx.Response(200, json=_chain_response())
        )
        async with httpx.AsyncClient() as client:
            poller = ChainPoller(client, "fake-token")
            result = await poller.poll(_make_signal())

        assert result.contract_found is True
        assert result.contract_oi == 100
        assert result.contract_volume == 50
        assert result.spot_price == 48.50
        assert len(result.neighbor_strikes) > 0
        assert len(result.adjacent_expiry_oi) > 0

    @respx.mock
    async def test_contract_not_found(self):
        respx.get(f"{UW_BASE}/api/stock/ACME/option-chains").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        async with httpx.AsyncClient() as client:
            poller = ChainPoller(client, "fake-token")
            result = await poller.poll(_make_signal())

        assert result.contract_found is False

    @respx.mock
    async def test_api_error(self):
        respx.get(f"{UW_BASE}/api/stock/ACME/option-chains").mock(
            return_value=httpx.Response(500)
        )
        async with httpx.AsyncClient() as client:
            poller = ChainPoller(client, "fake-token")
            result = await poller.poll(_make_signal())

        assert result.contract_found is False

    @respx.mock
    async def test_neighbor_radius(self):
        """Neighbors should only include strikes within configured radius."""
        cfg = TrackerConfig(neighbor_strike_radius=1)
        respx.get(f"{UW_BASE}/api/stock/ACME/option-chains").mock(
            return_value=httpx.Response(200, json=_chain_response())
        )
        async with httpx.AsyncClient() as client:
            poller = ChainPoller(client, "fake-token", config=cfg)
            result = await poller.poll(_make_signal())

        neighbor_strikes = {n.strike for n in result.neighbor_strikes}
        assert 45.0 in neighbor_strikes
        assert 55.0 in neighbor_strikes
        assert 40.0 not in neighbor_strikes  # outside radius of 1
