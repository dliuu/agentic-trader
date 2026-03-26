"""Tests for the flow analyst deterministic scoring engine."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from grader.agents.flow_analyst import FlowAnalyst
from shared.filters import FLOW_SCORING, GATE_THRESHOLDS
from shared.models import FillType, FlowCandidate, OptionType


def _make_candidate(**overrides) -> FlowCandidate:
    """Factory for test flow rows with sensible defaults."""
    defaults = dict(
        id="test-001",
        ticker="ACME",
        strike=150.0,
        expiry=datetime.now(timezone.utc) + timedelta(days=10),
        option_type=OptionType.CALL,
        fill_type=FillType.SWEEP,
        premium=200_000.0,
        spot_price=120.0,
        volume=5000,
        open_interest=1000,
        oi_change=4.0,
        confluence_score=4,
        signals=["deep_otm", "outsized_premium", "sweep", "oi_spike"],
        scanned_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    return FlowCandidate(**defaults)


class TestFlowAnalyst:
    def setup_method(self):
        self.analyst = FlowAnalyst()

    def test_spy_excluded(self):
        c = _make_candidate(ticker="SPY")
        result = self.analyst.score(c)
        assert result.skipped is True
        assert result.score == 0
        assert "etf" in (result.skip_reason or "")

    def test_qqq_excluded(self):
        c = _make_candidate(ticker="QQQ")
        result = self.analyst.score(c)
        assert result.skipped is True
        assert "etf" in (result.skip_reason or "")

    def test_iwm_excluded(self):
        c = _make_candidate(ticker="IWM")
        result = self.analyst.score(c)
        assert result.skipped is True
        assert "etf" in (result.skip_reason or "")

    def test_tqqq_excluded_as_leveraged(self):
        c = _make_candidate(ticker="TQQQ")
        result = self.analyst.score(c)
        assert result.skipped is True
        assert "leveraged_etf" in (result.skip_reason or "")

    def test_vxx_excluded_as_vix_product(self):
        c = _make_candidate(ticker="VXX")
        result = self.analyst.score(c)
        assert result.skipped is True
        assert "vix_product" in (result.skip_reason or "")

    def test_normal_ticker_not_excluded(self):
        c = _make_candidate(ticker="AAPL")
        result = self.analyst.score(c)
        assert result.skipped is False
        assert result.score > 0

    def test_nvda_not_excluded(self):
        c = _make_candidate(ticker="NVDA")
        result = self.analyst.score(c)
        assert result.skipped is False
        assert result.score > 0

    def test_tsla_not_excluded(self):
        c = _make_candidate(ticker="TSLA")
        result = self.analyst.score(c)
        assert result.skipped is False
        assert result.score > 0

    def test_perfect_trade_scores_high(self):
        """A trade hitting every positive signal should score 85+."""
        c = _make_candidate(
            premium=600_000,
            fill_type=FillType.SWEEP,
            oi_change=6.0,
            strike=180.0,
            spot_price=120.0,
            expiry=datetime.now(timezone.utc) + timedelta(days=3),
            confluence_score=5,
        )
        result = self.analyst.score(c)
        assert result.score >= 85
        assert "premium_over_500k" in result.signals
        assert "sweep_fill" in result.signals
        assert "deep_otm_25pct" in result.signals
        assert "weekly_expiry" in result.signals

    def test_weak_trade_scores_low(self):
        """A trade with minimal signals should score around 45-55."""
        c = _make_candidate(
            premium=10_000,
            fill_type=FillType.SPLIT,
            oi_change=1.0,
            strike=121.0,
            spot_price=120.0,
            expiry=datetime.now(timezone.utc) + timedelta(days=90),
            confluence_score=2,
        )
        result = self.analyst.score(c)
        assert result.score <= 55

    def test_likely_hedge_scores_below_threshold(self, monkeypatch):
        """ATM split, declining OI, long-dated: hedge-like; should fail Gate 1.

        With default weights, split + ATM + declining OI + LEAPS lands on the Gate 1
        floor (40). A slightly stronger declining-OI penalty keeps the score strictly
        below the threshold without changing shipped defaults.
        """
        monkeypatch.setattr(
            "grader.agents.flow_analyst.FLOW_SCORING",
            replace(FLOW_SCORING, oi_change_declining_points=-8),
        )
        c = _make_candidate(
            premium=10_000,
            fill_type=FillType.SPLIT,
            oi_change=-2.0,
            strike=120.0,
            spot_price=120.0,
            expiry=datetime.now(timezone.utc) + timedelta(days=180),
            confluence_score=2,
        )
        result = self.analyst.score(c)
        assert result.score < GATE_THRESHOLDS.flow_analyst_min

    def test_same_input_same_output(self):
        """Flow analyst must be perfectly deterministic."""
        c = _make_candidate()
        r1 = self.analyst.score(c)
        r2 = self.analyst.score(c)
        assert r1.score == r2.score
        assert r1.signals == r2.signals

    def test_zero_spot_price_no_crash(self):
        c = _make_candidate(spot_price=0.0)
        result = self.analyst.score(c)
        assert isinstance(result.score, int)

    def test_none_oi_change_no_crash(self):
        c = _make_candidate(oi_change=None)
        result = self.analyst.score(c)
        assert isinstance(result.score, int)

    def test_score_clamped_to_100(self):
        """Even with every signal maxed, score cannot exceed 100."""
        c = _make_candidate(
            premium=1_000_000,
            fill_type=FillType.SWEEP,
            oi_change=10.0,
            strike=300.0,
            spot_price=100.0,
            expiry=datetime.now(timezone.utc) + timedelta(days=2),
            confluence_score=6,
        )
        result = self.analyst.score(c)
        assert result.score <= 100

    def test_score_clamped_to_1(self):
        """Score cannot go below 1 (non-skipped)."""
        c = _make_candidate(
            premium=100,
            fill_type=FillType.SPLIT,
            oi_change=-5.0,
            strike=120.0,
            spot_price=120.0,
            expiry=datetime.now(timezone.utc) + timedelta(days=365),
            confluence_score=2,
        )
        result = self.analyst.score(c)
        assert result.score >= 1
