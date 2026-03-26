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
    gate2_avg_threshold: int = 45
    deterministic_avg_min: int = 45

    # Gate 3: Final synthesis score minimum to proceed to execution
    final_score_min: int = 70


GATE_THRESHOLDS = GateThresholds()


# ──────────────────────────────────────────────
# VOLATILITY ANALYST SCORING
# ──────────────────────────────────────────────


@dataclass(frozen=True)
class VolScoringConfig:
    """All volatility analyst scoring thresholds and weights.

    Edit these values and restart to tune. All fields are named —
    no magic constants in scoring logic.
    """

    # === Dimension weights (must sum to 1.0) ===
    absolute_weight: float = 0.45
    historical_weight: float = 0.30
    market_context_weight: float = 0.25

    # === Dimension 1: Absolute value ===

    # IV Rank thresholds
    iv_rank_low_threshold: float = 25.0  # Below this = cheap vol
    iv_rank_low_bonus: int = 15
    iv_rank_mid_low_threshold: float = 40.0  # Below this = moderately cheap
    iv_rank_mid_low_bonus: int = 7
    iv_rank_high_threshold: float = 75.0  # Above this = expensive vol
    iv_rank_high_penalty: int = 15
    iv_rank_mid_high_threshold: float = 60.0  # Above this = moderately expensive
    iv_rank_mid_high_penalty: int = 7

    # IV vs Realized Vol ratio
    iv_rv_deep_discount_threshold: float = 0.80  # IV far below RV = great deal
    iv_rv_deep_discount_bonus: int = 15
    iv_rv_discount_threshold: float = 0.90  # IV below RV = good deal
    iv_rv_discount_bonus: int = 10
    iv_rv_mild_premium_threshold: float = 1.20  # Moderate premium
    iv_rv_mild_premium_penalty: int = 6
    iv_rv_premium_threshold: float = 1.40  # Heavy premium
    iv_rv_premium_penalty: int = 12
    iv_rv_extreme_premium_threshold: float = 1.60  # Extreme premium
    iv_rv_extreme_premium_penalty: int = 18

    # Theta decay (daily decay as fraction of premium)
    theta_low_threshold: float = 0.02  # <2% daily decay = slow bleed, good
    theta_low_bonus: int = 5
    theta_high_threshold: float = 0.05  # >5% daily decay = fast bleed, bad
    theta_high_penalty: int = 10
    theta_extreme_threshold: float = 0.08  # >8% = ticket will be worthless soon
    theta_extreme_penalty: int = 15

    # Vega × IV rank interaction
    # "High vega" defined as vega > vega_high_threshold (as % of premium)
    vega_high_threshold: float = 0.10  # Vega > 10% of premium = vol-sensitive
    vega_iv_synergy_bonus: int = 8  # High vega + low IV rank
    vega_iv_conflict_penalty: int = 8  # High vega + high IV rank

    # === Dimension 2: Relative to history ===

    # IV percentile vs rank divergence
    pctl_rank_divergence_threshold: float = 15.0  # |percentile - rank| > 15
    pctl_below_rank_bonus: int = 6  # percentile << rank = cheap vs distribution
    pctl_above_rank_penalty: int = 4  # percentile >> rank = expensive vs distribution

    # Term structure shape
    term_inversion_threshold: float = -0.05  # slope < -5% = inverted
    term_inversion_near_expiry_bonus: int = 10  # Inverted + candidate in near term
    term_inversion_far_expiry_bonus: int = 4  # Inverted but candidate is far-dated
    term_contango_threshold: float = 0.15  # slope > 15% = steep contango
    term_contango_near_penalty: int = 8  # Steep contango + near-term = overpaying
    term_contango_far_bonus: int = 3  # Steep contango but far-dated = ok

    # Near-term DTE threshold for term structure scoring
    near_term_dte_threshold: int = 30  # <= 30 DTE = "near term"

    # Realized vol regime shift (20d vs 60d)
    rv_expansion_threshold: float = 1.20  # RV20/RV60 > 1.2 = vol expanding
    rv_expansion_bonus: int = 8
    rv_compression_threshold: float = 0.80  # RV20/RV60 < 0.8 = vol compressing
    rv_compression_penalty: int = 6

    # === Dimension 3: Market context ===

    # Ticker IV rank vs market IV rank (SPY proxy) divergence
    market_divergence_threshold: float = 20.0  # |ticker_rank - market_rank| > 20
    ticker_cheap_market_expensive_bonus: int = 10  # ticker low + market high
    ticker_expensive_market_cheap_penalty: int = 10

    # IV/RV ratio vs sector median
    sector_below_median_bonus: int = 8  # Ticker IV/RV < sector median
    sector_above_p75_penalty: int = 8  # Ticker IV/RV > sector 75th percentile

    # Delta-adjusted moneyness
    delta_sweet_low: float = 0.20  # Reasonable probability range
    delta_sweet_high: float = 0.50
    delta_sweet_bonus: int = 5
    delta_deep_otm_threshold: float = 0.10  # Lottery ticket
    delta_deep_otm_penalty: int = 8
    delta_deep_itm_threshold: float = 0.80  # Paying mostly intrinsic value
    delta_deep_itm_penalty: int = 4

    # === Score clamping ===
    min_score: int = 5
    max_score: int = 95


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
