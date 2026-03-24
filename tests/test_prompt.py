"""Tests for grader prompt templates."""

import pytest
from datetime import datetime, timezone

from shared.models import Candidate, SignalMatch
from grader.models import GradingContext, Greeks, NewsItem, InsiderTrade
from grader.prompt import build_system_prompt, build_user_prompt


def make_sample_context(greeks=None, recent_news=None, insider_trades=None, congressional_trades=None):
    """Factory fixture for GradingContext with configurable optional fields."""
    candidate = Candidate(
        id="cand-p1",
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
        signals=[
            SignalMatch(rule_name="otm", weight=1.0, detail="OTM 28.6%"),
            SignalMatch(rule_name="premium", weight=1.5, detail="Premium $75k"),
        ],
        confluence_score=2.5,
        dark_pool_confirmation=False,
        market_tide_aligned=False,
        raw_alert_id="raw-1",
    )
    return GradingContext(
        candidate=candidate,
        current_spot=150.0,
        daily_volume=1_000_000,
        avg_daily_volume=800_000,
        greeks=greeks,
        recent_news=recent_news or [],
        insider_trades=insider_trades or [],
        congressional_trades=congressional_trades or [],
        sector="Technology",
        market_cap=12_500_000_000.0,
    )


def test_prompt_renders_without_errors():
    ctx = make_sample_context(greeks=Greeks(delta=0.5, iv=0.32))
    system = build_system_prompt()
    user = build_user_prompt(ctx)
    assert "JSON" in system
    assert ctx.candidate.ticker in user
    assert "$" in user  # Premium is formatted


def test_prompt_includes_json_schema():
    system = build_system_prompt()
    assert '"score"' in system
    assert '"rationale"' in system


def test_prompt_handles_missing_greeks():
    ctx = make_sample_context(greeks=None)
    user = build_user_prompt(ctx)
    assert "Not available" in user


def test_prompt_handles_missing_news():
    ctx = make_sample_context(recent_news=[])
    user = build_user_prompt(ctx)
    assert "No recent headlines" in user


def test_prompt_handles_missing_insider():
    ctx = make_sample_context(insider_trades=[], congressional_trades=[])
    user = build_user_prompt(ctx)
    assert "No recent insider activity" in user


def test_prompt_formats_all_context_fields():
    """User prompt correctly formats all fields from GradingContext."""
    greeks = Greeks(delta=0.42, gamma=0.08, theta=-0.03, vega=0.11, iv=0.34)
    news = [
        NewsItem(
            headline="ACME beats earnings",
            source="Reuters",
            published_at=datetime(2026, 3, 24, 13, 15, 0, tzinfo=timezone.utc),
        )
    ]
    insider = [
        InsiderTrade(
            name="Jane Doe",
            title="CEO",
            trade_type="buy",
            shares=2000,
            value=500000.0,
            filed_at=datetime(2026, 3, 20, 10, 0, 0, tzinfo=timezone.utc),
        )
    ]
    ctx = make_sample_context(greeks=greeks, recent_news=news, insider_trades=insider)

    user = build_user_prompt(ctx)

    assert "ACME" in user
    assert "$180" in user or "180" in user
    assert "CALL" in user
    assert "2026-04-03" in user
    assert "$75,000" in user
    assert "Sweep" in user
    assert "$150.00" in user
    assert "1,000,000" in user
    assert "800,000" in user
    assert "Technology" in user
    assert "Delta:" in user
    assert "IV:" in user
    assert "[Reuters]" in user
    assert "ACME beats earnings" in user
    assert "Jane Doe" in user
    assert "otm" in user
    assert "premium" in user
