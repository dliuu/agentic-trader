"""Tests for grader data models."""

import pytest
from datetime import datetime, timezone
from pydantic import ValidationError

from shared.models import Candidate, SignalMatch
from grader.models import GradingContext, GradeResponse, Greeks, NewsItem, InsiderTrade, ScoredTrade


@pytest.fixture
def sample_candidate():
    return Candidate(
        id="cand-g1",
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


def test_grading_context_instantiation(sample_candidate):
    """GradingContext can be instantiated with sample data."""
    ctx = GradingContext(
        candidate=sample_candidate,
        current_spot=150.0,
        daily_volume=1_000_000,
    )
    assert ctx.candidate.ticker == "ACME"
    assert ctx.current_spot == 150.0
    assert ctx.daily_volume == 1_000_000
    assert ctx.avg_daily_volume is None
    assert ctx.greeks is None
    assert ctx.recent_news == []
    assert ctx.insider_trades == []


def test_grading_context_with_optionals(sample_candidate):
    """GradingContext accepts all optional fields."""
    greeks = Greeks(delta=0.5, iv=0.32)
    news = [NewsItem(headline="ACME beats", source="Reuters", published_at=datetime.now(timezone.utc))]
    insider = [
        InsiderTrade(
            name="Jane Doe",
            title="CEO",
            trade_type="buy",
            shares=10_000,
            value=500_000.0,
            filed_at=datetime.now(timezone.utc),
        )
    ]
    ctx = GradingContext(
        candidate=sample_candidate,
        current_spot=145.0,
        daily_volume=2_000_000,
        avg_daily_volume=1_500_000,
        greeks=greeks,
        recent_news=news,
        insider_trades=insider,
        congressional_trades=[],
        sector="Technology",
        market_cap=50_000_000_000.0,
    )
    assert ctx.greeks is not None
    assert ctx.greeks.delta == 0.5
    assert len(ctx.recent_news) == 1
    assert len(ctx.insider_trades) == 1
    assert ctx.sector == "Technology"


def test_grade_response_instantiation():
    """GradeResponse can be instantiated with valid data."""
    resp = GradeResponse(
        score=82,
        verdict="pass",
        rationale="Strong signal. OTM premium supports directional bet.",
        signals_confirmed=["otm", "premium"],
        risk_factors=["elevated IV"],
        likely_directional=True,
    )
    assert resp.score == 82
    assert resp.verdict == "pass"
    assert resp.likely_directional is True


def test_grade_response_score_validation_rejects_out_of_range():
    """GradeResponse(score=105, ...) raises ValidationError."""
    with pytest.raises(ValidationError) as exc_info:
        GradeResponse(
            score=105,
            verdict="pass",
            rationale="Test",
            signals_confirmed=[],
            likely_directional=True,
        )
    assert "Score must be 1–100" in str(exc_info.value)


def test_grade_response_score_validation_rejects_zero():
    """GradeResponse(score=0, ...) raises ValidationError."""
    with pytest.raises(ValidationError):
        GradeResponse(
            score=0,
            verdict="fail",
            rationale="Test",
            signals_confirmed=[],
            likely_directional=False,
        )


def test_grade_response_verdict_validation_rejects_invalid():
    """Free-form verdicts are normalized to pass/fail instead of raising."""
    resp = GradeResponse(
        score=50,
        verdict="maybe",
        rationale="Test",
        signals_confirmed=[],
        likely_directional=True,
    )
    assert resp.verdict == "fail"


def test_grade_response_round_trips_through_json():
    """GradeResponse round-trips: model_validate_json(obj.model_dump_json())."""
    obj = GradeResponse(
        score=75,
        verdict="pass",
        rationale="Moderate conviction. Some supporting signals.",
        signals_confirmed=["otm"],
        risk_factors=[],
        likely_directional=True,
    )
    json_str = obj.model_dump_json()
    restored = GradeResponse.model_validate_json(json_str)
    assert restored.score == obj.score
    assert restored.verdict == obj.verdict
    assert restored.rationale == obj.rationale
    assert restored.signals_confirmed == obj.signals_confirmed


def test_scored_trade_instantiation(sample_candidate):
    """ScoredTrade can be instantiated with sample data."""
    grade = GradeResponse(
        score=85,
        verdict="pass",
        rationale="High conviction.",
        signals_confirmed=["otm", "premium"],
        likely_directional=True,
    )
    scored = ScoredTrade(
        candidate=sample_candidate,
        grade=grade,
        graded_at=datetime.now(timezone.utc),
        model_used="claude-sonnet-4-20250514",
        latency_ms=1200,
        input_tokens=1500,
        output_tokens=180,
    )
    assert scored.candidate.ticker == "ACME"
    assert scored.grade.score == 85
    assert scored.latency_ms == 1200
