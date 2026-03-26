"""
Centralized filter configuration for all grading agents.

ALL thresholds, exclusion lists, scoring weights, and tunable parameters
live in this file. No magic numbers in agent code — every threshold
references a constant from here.

To tune the system, edit ONLY this file.
"""

from dataclasses import dataclass
from enum import Enum


# ──────────────────────────────────────────────
# TICKER EXCLUSIONS
# ──────────────────────────────────────────────


class TickerExclusionReason(str, Enum):
    """Why a ticker was excluded from grading."""

    ETF = "etf"
    INDEX = "index"
    LEVERAGED_ETF = "leveraged_etf"
    INVERSE_ETF = "inverse_etf"
    VIX_PRODUCT = "vix_product"
    PENNY_STOCK = "penny_stock"


# NOTE: The UW /api/option-trades/flow-alerts endpoint does NOT support
# issue_types[] filtering (only the /api/screener/option-contracts does).
# The scanner receives flow for ALL tickers including ETFs, so we must
# filter post-fetch. This set is checked by the flow analyst before scoring.
#
# The /api/stock/{ticker}/info endpoint returns an "issue_type" field
# (e.g. "Common Stock", "ETF", "ADR") which can be used to build this
# list dynamically on startup. For MVP, we use a static set of the
# most liquid ETFs + index products that would pollute our flow signals.

EXCLUDED_TICKERS: dict[str, TickerExclusionReason] = {
    # ── Major Index ETFs ──
    "SPY": TickerExclusionReason.ETF,
    "QQQ": TickerExclusionReason.ETF,
    "IWM": TickerExclusionReason.ETF,
    "DIA": TickerExclusionReason.ETF,
    "VOO": TickerExclusionReason.ETF,
    "VTI": TickerExclusionReason.ETF,
    "IVV": TickerExclusionReason.ETF,
    "RSP": TickerExclusionReason.ETF,
    "MDY": TickerExclusionReason.ETF,
    # ── Sector ETFs ──
    "XLF": TickerExclusionReason.ETF,
    "XLK": TickerExclusionReason.ETF,
    "XLE": TickerExclusionReason.ETF,
    "XLV": TickerExclusionReason.ETF,
    "XLI": TickerExclusionReason.ETF,
    "XLP": TickerExclusionReason.ETF,
    "XLU": TickerExclusionReason.ETF,
    "XLY": TickerExclusionReason.ETF,
    "XLB": TickerExclusionReason.ETF,
    "XLRE": TickerExclusionReason.ETF,
    "XLC": TickerExclusionReason.ETF,
    "SMH": TickerExclusionReason.ETF,
    "XBI": TickerExclusionReason.ETF,
    "XOP": TickerExclusionReason.ETF,
    "KRE": TickerExclusionReason.ETF,
    "XHB": TickerExclusionReason.ETF,
    "GDX": TickerExclusionReason.ETF,
    "GDXJ": TickerExclusionReason.ETF,
    # ── Bond / Fixed Income ETFs ──
    "TLT": TickerExclusionReason.ETF,
    "HYG": TickerExclusionReason.ETF,
    "LQD": TickerExclusionReason.ETF,
    "SHY": TickerExclusionReason.ETF,
    "IEF": TickerExclusionReason.ETF,
    "AGG": TickerExclusionReason.ETF,
    "BND": TickerExclusionReason.ETF,
    "JNK": TickerExclusionReason.ETF,
    # ── Commodity ETFs ──
    "GLD": TickerExclusionReason.ETF,
    "SLV": TickerExclusionReason.ETF,
    "USO": TickerExclusionReason.ETF,
    "UNG": TickerExclusionReason.ETF,
    # ── International ETFs ──
    "EEM": TickerExclusionReason.ETF,
    "EFA": TickerExclusionReason.ETF,
    "FXI": TickerExclusionReason.ETF,
    "KWEB": TickerExclusionReason.ETF,
    "EWZ": TickerExclusionReason.ETF,
    "INDA": TickerExclusionReason.ETF,
    # ── Leveraged / Inverse ETFs ──
    "TQQQ": TickerExclusionReason.LEVERAGED_ETF,
    "SQQQ": TickerExclusionReason.INVERSE_ETF,
    "SPXL": TickerExclusionReason.LEVERAGED_ETF,
    "SPXS": TickerExclusionReason.INVERSE_ETF,
    "UPRO": TickerExclusionReason.LEVERAGED_ETF,
    "SDOW": TickerExclusionReason.INVERSE_ETF,
    "UDOW": TickerExclusionReason.LEVERAGED_ETF,
    "SOXL": TickerExclusionReason.LEVERAGED_ETF,
    "SOXS": TickerExclusionReason.INVERSE_ETF,
    "LABU": TickerExclusionReason.LEVERAGED_ETF,
    "LABD": TickerExclusionReason.INVERSE_ETF,
    "TNA": TickerExclusionReason.LEVERAGED_ETF,
    "TZA": TickerExclusionReason.INVERSE_ETF,
    "ARKK": TickerExclusionReason.ETF,
    "ARKW": TickerExclusionReason.ETF,
    "ARKF": TickerExclusionReason.ETF,
    # ── VIX Products ──
    "VXX": TickerExclusionReason.VIX_PRODUCT,
    "UVXY": TickerExclusionReason.VIX_PRODUCT,
    "SVXY": TickerExclusionReason.VIX_PRODUCT,
    "VIXY": TickerExclusionReason.VIX_PRODUCT,
    # ── Index Options (if they appear in flow) ──
    "SPX": TickerExclusionReason.INDEX,
    "NDX": TickerExclusionReason.INDEX,
    "RUT": TickerExclusionReason.INDEX,
    "VIX": TickerExclusionReason.INDEX,
    "DJX": TickerExclusionReason.INDEX,
    "XSP": TickerExclusionReason.INDEX,
}


def is_excluded_ticker(ticker: str) -> tuple[bool, TickerExclusionReason | None]:
    """Check if a ticker should be excluded from grading.
    Returns (is_excluded, reason)."""
    reason = EXCLUDED_TICKERS.get(ticker.upper())
    return (reason is not None, reason)


# ──────────────────────────────────────────────
# FLOW ANALYST SCORE CLAMP (no magic numbers in agent code)
# ──────────────────────────────────────────────

FLOW_SCORE_MIN = 1
FLOW_SCORE_MAX = 100
# SubScore for excluded tickers (not clamped to FLOW_SCORE_MIN)
FLOW_SCORE_SKIPPED = 0
# Parsed scanner expiry strings are anchored to end-of-day UTC for DTE alignment
EXPIRY_DAY_END_HOUR = 23
EXPIRY_DAY_END_MINUTE = 59
EXPIRY_DAY_END_SECOND = 59
# OTM ratio is undefined at or below this spot (avoid div-by-zero)
SPOT_PRICE_INVALID_MAX = 0.0
# OI change strictly below this counts as declining open interest
OI_CHANGE_DECLINING_BELOW = 0.0


# ──────────────────────────────────────────────
# FLOW ANALYST SCORING WEIGHTS
# ──────────────────────────────────────────────


@dataclass(frozen=True)
class FlowScoringConfig:
    """All tunable parameters for the flow analyst.
    Change values here to re-weight the scoring algorithm."""

    # Baseline score — every candidate starts here
    baseline: int = 50

    # Premium thresholds and point awards
    premium_tier_1_min: float = 25_000  # Minimum for any premium bonus
    premium_tier_1_points: int = 5
    premium_tier_2_min: float = 100_000
    premium_tier_2_points: int = 10  # Replaces tier 1, not additive
    premium_tier_3_min: float = 500_000
    premium_tier_3_points: int = 15  # Replaces tier 2, not additive

    # Fill type points
    sweep_points: int = 12
    block_points: int = 8
    split_points: int = 3

    # OI change thresholds (multiples of 20-day average)
    oi_change_tier_1_min: float = 1.5
    oi_change_tier_1_points: int = 5
    oi_change_tier_2_min: float = 3.0
    oi_change_tier_2_points: int = 10  # Replaces tier 1
    oi_change_tier_3_min: float = 5.0
    oi_change_tier_3_points: int = 15  # Replaces tier 2
    oi_change_declining_points: int = -5  # OI declining = closing, not opening

    # Strike distance from spot (OTM percentage)
    otm_deep_threshold: float = 0.25  # 25%+ OTM
    otm_deep_points: int = 13
    otm_moderate_threshold: float = 0.15  # 15-25% OTM
    otm_moderate_points: int = 8
    otm_slight_threshold: float = 0.05  # 5-15% OTM
    otm_slight_points: int = 3
    atm_itm_points: int = -3  # ATM or ITM = likely hedging

    # Days to expiry
    dte_weekly_max: int = 5  # 0-5 days
    dte_weekly_points: int = 10
    dte_near_max: int = 14  # 6-14 days
    dte_near_points: int = 7
    dte_swing_max: int = 30  # 15-30 days
    dte_swing_points: int = 3
    dte_long_min: int = 60  # 60+ days = likely hedging/LEAPS
    dte_long_points: int = -5

    # Confluence bonus (scanner already requires 2+ signals)
    confluence_high_min: int = 5
    confluence_high_points: int = 8
    confluence_moderate_min: int = 4
    confluence_moderate_points: int = 5
    confluence_low_min: int = 3
    confluence_low_points: int = 3

    # Bid-ask side aggression (if available)
    ask_side_points: int = 5  # Bought at ask = aggressive
    mid_to_ask_points: int = 3
    at_or_below_mid_points: int = -3  # Filled at/below mid = passive


FLOW_SCORING = FlowScoringConfig()


# ──────────────────────────────────────────────
# GATE THRESHOLDS
# ──────────────────────────────────────────────


@dataclass(frozen=True)
class GateThresholds:
    """Score thresholds for each pipeline gate.
    Candidates below these thresholds are discarded."""

    # Gate 1: Flow analyst minimum score to proceed
    flow_analyst_min: int = 40

    # Gate 2: Average of (flow + vol + risk) minimum to proceed to LLM layer
    deterministic_avg_min: int = 45

    # Gate 3: Final synthesis score minimum to proceed to execution
    final_score_min: int = 70


GATE_THRESHOLDS = GateThresholds()


# ──────────────────────────────────────────────
# VOL ANALYST SCORING (for later implementation)
# ──────────────────────────────────────────────


@dataclass(frozen=True)
class VolScoringConfig:
    """Tunable parameters for the volatility analyst."""

    baseline: int = 50
    iv_rank_low_threshold: int = 25
    iv_rank_low_points: int = 15
    iv_rank_below_median_threshold: int = 50
    iv_rank_below_median_points: int = 5
    iv_rank_high_threshold: int = 75
    iv_rank_high_points: int = -15
    iv_rank_extreme_threshold: int = 90
    iv_rank_extreme_points: int = -10
    iv_premium_high_threshold: float = 0.3
    iv_premium_high_points: int = -10
    iv_discount_points: int = 10
    term_structure_inverted_points: int = 8
    low_delta_threshold: float = 0.15
    low_delta_points: int = 5
    high_theta_decay_pct: float = 5.0
    high_theta_decay_points: int = -10


VOL_SCORING = VolScoringConfig()


# ──────────────────────────────────────────────
# RISK ANALYST SCORING (for later implementation)
# ──────────────────────────────────────────────


@dataclass(frozen=True)
class RiskScoringConfig:
    """Tunable parameters for the risk analyst."""

    baseline: int = 80
    wide_spread_threshold_1: float = 0.10
    wide_spread_points_1: int = -15
    wide_spread_threshold_2: float = 0.20
    wide_spread_points_2: int = -10
    earnings_before_expiry_points: int = -15
    position_over_5pct_points: int = -10
    position_over_10pct_points: int = -15
    near_max_positions_threshold: int = 4
    near_max_positions_points: int = -10
    expiry_within_3d_points: int = -15
    expiry_within_7d_points: int = -5
    illiquid_volume_threshold: int = 100
    illiquid_points: int = -15


RISK_SCORING = RiskScoringConfig()


# ──────────────────────────────────────────────
# LLM AGENT WEIGHTS (for synthesis agent)
# ──────────────────────────────────────────────


@dataclass(frozen=True)
class AgentWeights:
    """How much each agent's score contributes to the final weighted average."""

    flow_analyst: float = 0.25
    volatility_analyst: float = 0.20
    risk_analyst: float = 0.15
    sentiment_analyst: float = 0.15
    insider_tracker: float = 0.15
    sector_analyst: float = 0.10


AGENT_WEIGHTS = AgentWeights()
