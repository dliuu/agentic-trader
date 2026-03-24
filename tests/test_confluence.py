import pytest
from datetime import datetime, timedelta

from shared.models import Candidate, SignalMatch
from scanner.models.dark_pool import DarkPoolPrint
from scanner.models.market_tide import MarketTide
from scanner.rules.confluence import ConfluenceEnricher


@pytest.fixture
def sample_candidate():
    return Candidate(
        id="cand-1",
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


def test_confluence_adds_dark_pool_signal(sample_config, sample_candidate):
    enricher = ConfluenceEnricher(sample_config)
    now = datetime.utcnow()
    dark_pool = [
        DarkPoolPrint(ticker="ACME", notional=600000, executed_at=now - timedelta(minutes=5))
    ]
    tide = MarketTide(direction="neutral")
    result = enricher.enrich(sample_candidate, dark_pool, tide)
    assert result.dark_pool_confirmation is True
    assert any(s.rule_name == "dark_pool" for s in result.signals)


def test_confluence_adds_tide_aligned(sample_config, sample_candidate):
    enricher = ConfluenceEnricher(sample_config)
    tide = MarketTide(direction="bullish")
    result = enricher.enrich(sample_candidate, [], tide)
    assert result.market_tide_aligned is True
