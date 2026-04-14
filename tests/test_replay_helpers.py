"""Unit tests for replay helper functions."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from replay.helpers import (
    build_flow_watch_result,
    find_contract,
    hot_ticker_count_for_date,
    mock_synthesis_score,
    run_gate2_from_backfill,
)
from shared.models import Candidate, SignalMatch, SubScore
from tracker.models import Signal, SignalState


def test_find_contract():
    chain = [
        {"strike": 50.0, "expiry": "2025-08-15", "option_type": "call", "open_interest": 10},
        {"strike": 50.01, "expiry": "2025-08-15", "option_type": "call", "open_interest": 99},
    ]
    c = find_contract(chain, 50.0, "2025-08-15", "call")
    assert c is not None
    assert c["open_interest"] in (10, 99)


def test_build_flow_watch_result():
    from datetime import datetime, timezone

    from scanner.models.flow_alert import FlowAlert

    sig = Signal(
        id="s",
        ticker="ACME",
        strike=180.0,
        expiry="2026-04-03",
        option_type="call",
        direction="bullish",
        state=SignalState.PENDING,
        initial_score=80,
        initial_premium=75000,
        initial_oi=100,
        initial_volume=500,
        grade_id="g",
        conviction_score=80.0,
        created_at=datetime(2026, 3, 20, 10, 0, tzinfo=timezone.utc),
    )
    a = FlowAlert(
        id="a1",
        ticker="ACME",
        type="call",
        strike=180.0,
        expiry="2026-04-03",
        total_premium=1000,
        total_size=10,
        open_interest=100,
        underlying_price=140.0,
        has_sweep=True,
        has_floor=False,
        created_at=datetime(2026, 3, 20, 16, 0, tzinfo=timezone.utc),
    )
    r = build_flow_watch_result(
        sig,
        [a],
        cutoff=sig.created_at,
        checked_at=datetime(2026, 3, 20, 20, 0, tzinfo=timezone.utc),
    )
    assert len(r.events) == 1
    assert r.events[0].is_same_contract is True


def test_mock_synthesis_score():
    f = SubScore(agent="flow", score=80, rationale="", signals=[])
    v = SubScore(agent="vol", score=50, rationale="", signals=[])
    r = SubScore(agent="risk", score=60, rationale="", signals=[])
    assert mock_synthesis_score(f, v, r) == int(0.6 * 80 + 0.2 * 50 + 0.2 * 60)


def test_hot_ticker_count_for_date():
    m = {"ACME": ["2026-03-01", "2026-03-05", "2026-03-10", "2026-03-12", "2026-03-15", "2026-03-18"]}
    assert hot_ticker_count_for_date(m, "ACME", "2026-03-20", lookback_days=14) >= 4


def test_run_gate2_from_backfill_smoke():
    from grader.context.sector_cache import SectorBenchmarkCache

    cache = SectorBenchmarkCache(
        benchmarks={},
        market_iv_rank=50.0,
        market_iv=0.2,
        market_iv_rv_ratio=1.0,
        refreshed_at=datetime.now(timezone.utc),
        ticker_snapshots=[],
    )
    c = Candidate(
        id="c1",
        source="flow_alert",
        ticker="ACME",
        direction="bullish",
        strike=180.0,
        expiry="2026-04-03",
        premium_usd=75000,
        underlying_price=140.0,
        implied_volatility=0.3,
        execution_type="Sweep",
        dte=14,
        volume=500,
        open_interest=100,
        signals=[SignalMatch(rule_name="premium", weight=1.0, detail="")],
        confluence_score=5.0,
        raw_alert_id="x",
    )
    flow = SubScore(agent="flow_analyst", score=70, rationale="", signals=[])
    chain = {
        "data": [
            {
                "expiry": "2026-04-03",
                "strike": 180.0,
                "option_type": "call",
                "bid": 2.0,
                "ask": 2.2,
                "volume": 100,
                "open_interest": 200,
                "delta": 0.3,
                "theta": -0.05,
                "gamma": 0.01,
                "vega": 0.1,
            }
        ]
    }
    vol = {"data": {"realized_volatility_20d": 0.25, "realized_volatility_60d": 0.26}}
    passed, vol_s, risk_s = run_gate2_from_backfill(c, flow, chain, vol, cache)
    assert isinstance(passed, bool)
    assert vol_s.score > 0
    assert risk_s.score > 0


@pytest.mark.asyncio
async def test_build_chain_poll_result_from_saved():
    from datetime import datetime, timezone

    import httpx

    from tracker.chain_poller import ChainPoller
    from tracker.config import TrackerConfig

    raw = {
        "data": [
            {
                "expiry": "2026-04-03",
                "strike": 180.0,
                "option_type": "call",
                "open_interest": 100,
                "volume": 50,
                "bid": 2.0,
                "ask": 2.2,
                "underlying_price": 140.0,
            }
        ]
    }
    sig = Signal(
        id="s",
        ticker="ACME",
        strike=180.0,
        expiry="2026-04-03",
        option_type="call",
        direction="bullish",
        state=SignalState.PENDING,
        initial_score=80,
        initial_premium=75000,
        initial_oi=100,
        initial_volume=500,
        grade_id="g",
        conviction_score=80.0,
        created_at=datetime.now(timezone.utc),
    )
    async with httpx.AsyncClient() as client:
        poller = ChainPoller(client, "t", TrackerConfig())
        r = poller.from_saved_json(raw, sig)
    assert r.contract_found is True
    assert r.contract_oi == 100
