"""Tests for CandidateQueue."""
import pytest
from datetime import datetime

from scanner.models.candidate import Candidate, SignalMatch
from scanner.output.queue import CandidateQueue


@pytest.fixture
def sample_candidate():
    return Candidate(
        id="cand-q-1",
        source="flow_alert",
        ticker="ACME",
        direction="bullish",
        strike=180.0,
        expiry="2026-04-03",
        premium_usd=75000.0,
        underlying_price=140.0,
        implied_volatility=None,
        execution_type="Sweep",
        dte=14,
        signals=[SignalMatch(rule_name="otm", weight=1.0, detail="OTM 28.6%")],
        confluence_score=1.0,
        dark_pool_confirmation=False,
        market_tide_aligned=False,
        raw_alert_id="raw-1",
    )


@pytest.mark.asyncio
async def test_queue_put_get(sample_candidate):
    """Put candidate and get it back."""
    queue = CandidateQueue(max_size=10)
    await queue.put(sample_candidate)
    got = await queue.get()
    assert got is sample_candidate
    assert got.ticker == "ACME"


@pytest.mark.asyncio
async def test_queue_qsize(sample_candidate):
    """qsize reflects items in queue."""
    queue = CandidateQueue(max_size=10)
    assert queue.qsize() == 0
    await queue.put(sample_candidate)
    await queue.put(sample_candidate)
    assert queue.qsize() == 2
    await queue.get()
    assert queue.qsize() == 1
    await queue.get()
    assert queue.qsize() == 0
