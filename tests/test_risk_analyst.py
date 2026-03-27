"""
Tests for the Risk Analyst (conviction scorer).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from grader.agents.risk_analyst import (
    _score_earnings_modifier,
    _score_liquidity_cost,
    _score_move_ratio,
    _score_premium_commitment,
    _score_spread_cost,
    _score_strike_distance,
    _score_time_pressure,
    _tier_lookup,
    extract_days_to_earnings,
    extract_option_chain_data,
    extract_realized_vol,
    score_risk_conviction,
)
from shared.filters import RiskConvictionConfig
from shared.models import FillType, FlowCandidate, OptionType


@pytest.fixture
def cfg() -> RiskConvictionConfig:
    return RiskConvictionConfig()


@pytest.fixture
def base_candidate() -> FlowCandidate:
    return FlowCandidate(
        id="test-001",
        ticker="AAPL",
        strike=200.0,
        expiry=datetime.now(timezone.utc) + timedelta(days=14),
        option_type=OptionType.CALL,
        fill_type=FillType.SWEEP,
        premium=100_000.0,
        spot_price=190.0,
        volume=500,
        open_interest=5000,
        oi_change=1.5,
        confluence_score=3,
        signals=["unusual_volume", "sweep"],
        scanned_at=datetime.now(timezone.utc),
        raw_data={},
    )


@pytest.fixture
def base_chain_data() -> dict:
    return {
        "bid": 4.50,
        "ask": 5.00,
        "mid": 4.75,
        "spread_pct": 10.53,
        "contract_volume": 300,
        "open_interest": 5000,
        "delta": 0.35,
        "theta": -0.08,
        "gamma": 0.02,
        "vega": 0.15,
        "iv": 0.32,
    }


class TestTierLookup:
    def test_below_first_tier(self):
        assert _tier_lookup(10, (25, 50, 75), (-5, 0, 5, 10)) == -5

    def test_at_tier_boundary(self):
        assert _tier_lookup(25, (25, 50, 75), (-5, 0, 5, 10)) == -5

    def test_between_tiers(self):
        assert _tier_lookup(40, (25, 50, 75), (-5, 0, 5, 10)) == 0

    def test_above_last_tier(self):
        assert _tier_lookup(100, (25, 50, 75), (-5, 0, 5, 10)) == 10


class TestFactorScorers:
    def test_premium_extreme(self, cfg):
        pts, signal = _score_premium_commitment(1_000_000, cfg)
        assert pts == 18
        assert signal is not None

    def test_time_pressure_short(self, cfg):
        pts, signal = _score_time_pressure(2, cfg)
        assert pts == 15
        assert signal is not None

    def test_spread_wide(self, cfg):
        pts, signal = _score_spread_cost(25.0, cfg)
        assert pts == 12
        assert signal is not None

    def test_strike_distance(self, cfg):
        pts, otm, _ = _score_strike_distance(240, 200, "call", cfg)
        assert otm == pytest.approx(20.0, abs=0.1)
        assert pts == 15

    def test_move_ratio_hard(self, cfg):
        pts, ratio, signal = _score_move_ratio(230, 200, "call", 5, 25.0, cfg)
        assert ratio is not None and ratio > 1.8
        assert pts == 15
        assert signal is not None

    def test_liquidity_thin(self, cfg):
        pts, signal = _score_liquidity_cost(20, cfg)
        assert pts == 10
        assert signal is not None

    def test_earnings_window(self, cfg):
        pts, signal = _score_earnings_modifier(10, 5, cfg)
        assert pts == cfg.earnings_window_modifier
        assert signal is not None


class TestScoreRiskConviction:
    def test_high_conviction_candidate(self, cfg):
        candidate = FlowCandidate(
            id="high-conv-001",
            ticker="TSLA",
            strike=300.0,
            expiry=datetime.now(timezone.utc) + timedelta(days=3),
            option_type=OptionType.CALL,
            fill_type=FillType.SWEEP,
            premium=600_000.0,
            spot_price=250.0,
            volume=80,
            open_interest=2000,
            oi_change=3.0,
            confluence_score=5,
            signals=["sweep", "unusual_volume"],
            scanned_at=datetime.now(timezone.utc),
            raw_data={},
        )
        chain_data = {
            "bid": 1.50,
            "ask": 2.10,
            "mid": 1.80,
            "spread_pct": 33.3,
            "contract_volume": 40,
            "open_interest": 2000,
            "delta": 0.10,
            "theta": -0.25,
            "gamma": 0.03,
            "vega": 0.08,
            "iv": 0.65,
        }
        result = score_risk_conviction(candidate, chain_data, 50.0, 2, cfg)
        assert result.score >= 75
        assert len(result.conviction_signals) >= 3

    def test_low_conviction_candidate(self, cfg):
        candidate = FlowCandidate(
            id="low-conv-001",
            ticker="MSFT",
            strike=420.0,
            expiry=datetime.now(timezone.utc) + timedelta(days=90),
            option_type=OptionType.CALL,
            fill_type=FillType.SPLIT,
            premium=15_000.0,
            spot_price=418.0,
            volume=5000,
            open_interest=20000,
            oi_change=1.1,
            confluence_score=1,
            signals=["unusual_volume"],
            scanned_at=datetime.now(timezone.utc),
            raw_data={},
        )
        chain_data = {
            "bid": 12.00,
            "ask": 12.30,
            "mid": 12.15,
            "spread_pct": 2.47,
            "contract_volume": 3000,
            "open_interest": 20000,
            "delta": 0.52,
            "theta": -0.03,
            "gamma": 0.01,
            "vega": 0.25,
            "iv": 0.22,
        }
        result = score_risk_conviction(candidate, chain_data, 20.0, None, cfg)
        assert result.score <= 40

    def test_missing_all_data_untradeable(self, cfg):
        candidate = FlowCandidate(
            id="missing-001",
            ticker="XYZ",
            strike=50.0,
            expiry=datetime.now(timezone.utc) + timedelta(days=14),
            option_type=OptionType.PUT,
            fill_type=FillType.BLOCK,
            premium=50_000.0,
            spot_price=55.0,
            volume=0,
            open_interest=100,
            oi_change=None,
            confluence_score=1,
            signals=[],
            scanned_at=datetime.now(timezone.utc),
            raw_data={},
        )
        empty_chain = {
            "bid": None,
            "ask": None,
            "mid": None,
            "spread_pct": None,
            "contract_volume": None,
            "open_interest": None,
            "delta": None,
            "theta": None,
            "gamma": None,
            "vega": None,
            "iv": None,
        }
        result = score_risk_conviction(candidate, empty_chain, None, None, cfg)
        assert result.untradeable is True
        assert len(result.data_gaps) >= 3

    def test_score_clamped(self, cfg):
        candidate = FlowCandidate(
            id="clamp-001",
            ticker="SAFE",
            strike=100.0,
            expiry=datetime.now(timezone.utc) + timedelta(days=180),
            option_type=OptionType.CALL,
            fill_type=FillType.SPLIT,
            premium=5_000.0,
            spot_price=99.5,
            volume=10000,
            open_interest=50000,
            oi_change=1.0,
            confluence_score=1,
            signals=[],
            scanned_at=datetime.now(timezone.utc),
            raw_data={},
        )
        chain_data = {
            "bid": 8.00,
            "ask": 8.10,
            "mid": 8.05,
            "spread_pct": 1.24,
            "contract_volume": 5000,
            "open_interest": 50000,
            "delta": 0.55,
            "theta": -0.01,
            "gamma": 0.005,
            "vega": 0.30,
            "iv": 0.18,
        }
        result = score_risk_conviction(candidate, chain_data, 15.0, None, cfg)
        assert 1 <= result.score <= 100

    def test_execution_params_scale_monotonically(self, cfg):
        candidate_high = FlowCandidate(
            id="scale-001",
            ticker="NVDA",
            strike=180.0,
            expiry=datetime.now(timezone.utc) + timedelta(days=4),
            option_type=OptionType.CALL,
            fill_type=FillType.SWEEP,
            premium=800_000.0,
            spot_price=150.0,
            volume=30,
            open_interest=500,
            oi_change=5.0,
            confluence_score=5,
            signals=["sweep", "size_gt_oi"],
            scanned_at=datetime.now(timezone.utc),
            raw_data={},
        )
        chain_high = {
            "bid": 0.80,
            "ask": 1.20,
            "mid": 1.00,
            "spread_pct": 40.0,
            "contract_volume": 25,
            "open_interest": 500,
            "delta": 0.08,
            "theta": -0.15,
            "gamma": 0.04,
            "vega": 0.05,
            "iv": 0.80,
        }
        result_high = score_risk_conviction(candidate_high, chain_high, 55.0, 3, cfg)

        candidate_low = FlowCandidate(
            id="scale-002",
            ticker="JNJ",
            strike=162.0,
            expiry=datetime.now(timezone.utc) + timedelta(days=60),
            option_type=OptionType.CALL,
            fill_type=FillType.SPLIT,
            premium=20_000.0,
            spot_price=160.0,
            volume=2000,
            open_interest=15000,
            oi_change=1.0,
            confluence_score=1,
            signals=[],
            scanned_at=datetime.now(timezone.utc),
            raw_data={},
        )
        chain_low = {
            "bid": 5.00,
            "ask": 5.15,
            "mid": 5.075,
            "spread_pct": 2.96,
            "contract_volume": 1500,
            "open_interest": 15000,
            "delta": 0.48,
            "theta": -0.02,
            "gamma": 0.01,
            "vega": 0.20,
            "iv": 0.19,
        }
        result_low = score_risk_conviction(candidate_low, chain_low, 15.0, None, cfg)

        assert result_high.score > result_low.score
        assert result_high.recommended_position_size >= result_low.recommended_position_size
        assert (
            result_high.recommended_stop_loss_pct
            >= result_low.recommended_stop_loss_pct
        )


class TestExtractionHelpers:
    def test_extract_option_chain_data(self):
        candidate = FlowCandidate(
            id="extract-001",
            ticker="AAPL",
            strike=200.0,
            expiry=datetime(2026, 8, 15, tzinfo=timezone.utc),
            option_type=OptionType.CALL,
            fill_type=FillType.SWEEP,
            premium=50_000,
            spot_price=195.0,
            volume=500,
            open_interest=3000,
            oi_change=1.2,
            confluence_score=2,
            signals=[],
            scanned_at=datetime.now(timezone.utc),
            raw_data={},
        )
        api_response = {
            "data": [
                {
                    "strike": 200.0,
                    "expiry": "2026-08-15",
                    "option_type": "call",
                    "bid": 3.80,
                    "ask": 4.20,
                    "volume": 450,
                    "open_interest": 3000,
                    "delta": 0.38,
                    "theta": -0.06,
                }
            ]
        }
        result = extract_option_chain_data(api_response, candidate)
        assert result["bid"] == 3.80
        assert result["spread_pct"] == pytest.approx(10.0, abs=0.5)

    def test_extract_realized_vol(self):
        assert extract_realized_vol({"data": {"realized_volatility": 35.5}}) == 35.5
        assert extract_realized_vol({"data": [{"hv_20": 28.0}]}) == 28.0
        assert extract_realized_vol({}) is None

    def test_extract_days_to_earnings(self):
        future = datetime.now(timezone.utc) + timedelta(days=10)
        resp = {"data": [{"date": future.isoformat()}]}
        result = extract_days_to_earnings(resp)
        assert result is not None and 9 <= result <= 11
