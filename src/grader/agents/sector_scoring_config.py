"""Deterministic sector analyst scoring thresholds — single source of truth."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SectorScoringConfig:
    baseline: int = 50

    # Component weights (must sum to 1.0)
    weight_sector_flow: float = 0.50
    weight_market_tide: float = 0.35
    weight_economic: float = 0.15

    # Sector-tide C/P ratio thresholds
    sector_cp_ratio_strong_bullish: float = 1.5
    sector_cp_ratio_bullish: float = 1.15
    sector_cp_ratio_bearish: float = 0.85
    sector_cp_ratio_strong_bearish: float = 0.65

    # Sector-tide point awards
    sector_strong_bullish_pts: int = 20
    sector_bullish_pts: int = 10
    sector_neutral_pts: int = 0
    sector_bearish_pts: int = -10
    sector_strong_bearish_pts: int = -20

    # Sector ETF daily performance modifiers
    sector_etf_1d_strong: float = 0.02  # > +2% daily
    sector_etf_1d_weak: float = -0.02  # < -2% daily
    sector_etf_strong_pts: int = 5
    sector_etf_weak_pts: int = -5

    # Market-tide C/P ratio thresholds
    market_cp_ratio_strong_bullish: float = 1.4
    market_cp_ratio_bullish: float = 1.1
    market_cp_ratio_bearish: float = 0.9
    market_cp_ratio_strong_bearish: float = 0.7

    # Market-tide point awards
    market_strong_bullish_pts: int = 15
    market_bullish_pts: int = 7
    market_neutral_pts: int = 0
    market_bearish_pts: int = -7
    market_strong_bearish_pts: int = -15

    # Economic calendar
    econ_high_impact_within_3d_pts: int = -5
    econ_high_impact_within_7d_pts: int = -3
    econ_no_events_pts: int = 2

    # Score clamping
    score_min: int = 1
    score_max: int = 100


SECTOR_SCORING = SectorScoringConfig()
