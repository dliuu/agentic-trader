"""Tests for ScannerDB persistence layer."""
import pytest
from datetime import datetime

from scanner.models.candidate import Candidate, SignalMatch
from scanner.state.db import ScannerDB


@pytest.fixture
def sample_candidate():
    return Candidate(
        id="cand-db-1",
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


@pytest.fixture
async def db(tmp_path):
    """Fresh SQLite DB in a temp directory."""
    db_path = str(tmp_path / "test.db")
    scanner_db = ScannerDB(db_path)
    await scanner_db.connect()
    yield scanner_db
    await scanner_db.close()


@pytest.mark.asyncio
async def test_db_save_and_read_candidate(db, sample_candidate):
    """Save candidate and read it back."""
    await db.save_candidate(sample_candidate)
    row = await db.get_candidate(sample_candidate.id)
    assert row is not None
    assert row["ticker"] == sample_candidate.ticker
    assert row["direction"] == sample_candidate.direction
    assert row["strike"] == sample_candidate.strike
    assert row["expiry"] == sample_candidate.expiry
    assert row["premium_usd"] == sample_candidate.premium_usd
    assert row["confluence_score"] == sample_candidate.confluence_score
    assert row["raw_alert_id"] == sample_candidate.raw_alert_id


@pytest.mark.asyncio
async def test_db_save_and_read_raw_alert(db):
    """Save raw_alert and read it back."""
    await db.save_raw_alert("alert-1", "flow_alert", {"ticker": "AAPL", "strike": 150})
    row = await db.get_raw_alert("alert-1")
    assert row is not None
    assert row["id"] == "alert-1"
    assert row["source"] == "flow_alert"
    import json
    payload = json.loads(row["payload_json"])
    assert payload["ticker"] == "AAPL"
    assert payload["strike"] == 150


@pytest.mark.asyncio
async def test_db_log_and_read_cycle(db):
    """Log cycle and read it back."""
    started = datetime(2026, 3, 21, 9, 30, 0)
    finished = datetime(2026, 3, 21, 9, 30, 5)
    await db.log_cycle(
        started_at=started,
        finished_at=finished,
        alerts=10,
        candidates=2,
        errors=0,
    )
    row = await db.get_last_cycle()
    assert row is not None
    assert row["started_at"] == started.isoformat()
    assert row["finished_at"] == finished.isoformat()
    assert row["alerts_received"] == 10
    assert row["candidates_flagged"] == 2
    assert row["errors"] == 0
