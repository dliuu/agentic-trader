"""
Volatility Analyst — deterministic option pricing quality scorer.

Grades whether the option buyer is getting a good deal based on:
  1. Absolute value (IV rank, IV/RV spread, theta, vega exposure)
  2. Relative to ticker history (percentile divergence, term structure, RV regime)
  3. Market context (vs SPY, vs sector peers, delta positioning)

Zero LLM calls. ~200ms per candidate (4 UW API calls).
"""

from __future__ import annotations

import structlog

from grader.context.sector_cache import SectorBenchmarkCache
from grader.context.vol_ctx import VolContext, build_vol_context
from shared.filters import VolScoringConfig
from shared.models import Candidate, SubScore

logger = structlog.get_logger()

# Default config — importable, overridable for testing
VOL_CONFIG = VolScoringConfig()


async def score_volatility(
    candidate: Candidate,
    client,
    api_token: str,
    sector_cache: SectorBenchmarkCache,
    config: VolScoringConfig = VOL_CONFIG,
) -> SubScore:
    """Score a candidate's option pricing quality. Returns SubScore.

    If API data is unavailable, returns a neutral score (50) with skipped=True.
    """

    ctx = await build_vol_context(candidate, client, api_token)

    if ctx is None:
        return SubScore(
            agent="volatility_analyst",
            score=50,
            rationale="Could not fetch volatility data — returning neutral score.",
            signals=[],
            skipped=True,
            skip_reason="API data unavailable",
        )

    return _score_from_context(ctx, sector_cache, config)


def _score_from_context(
    ctx: VolContext,
    sector_cache: SectorBenchmarkCache,
    config: VolScoringConfig,
) -> SubScore:
    """Pure scoring function. Takes VolContext + cache, returns SubScore.

    This is the primary unit-test target.
    """

    signals: list[str] = []
    rationale_parts: list[str] = []

    abs_score, abs_signals, abs_rationale = _score_absolute_value(ctx, config)
    hist_score, hist_signals, hist_rationale = _score_relative_to_history(ctx, config)
    mkt_score, mkt_signals, mkt_rationale = _score_market_context(ctx, sector_cache, config)

    signals.extend(abs_signals)
    signals.extend(hist_signals)
    signals.extend(mkt_signals)
    rationale_parts.extend(abs_rationale)
    rationale_parts.extend(hist_rationale)
    rationale_parts.extend(mkt_rationale)

    raw_score = (
        abs_score * config.absolute_weight
        + hist_score * config.historical_weight
        + mkt_score * config.market_context_weight
    )

    final_score = int(max(config.min_score, min(config.max_score, round(raw_score))))

    if final_score >= 65:
        verdict = "Buyer is getting a good deal."
    elif final_score >= 45:
        verdict = "Pricing is roughly fair."
    else:
        verdict = "Buyer is overpaying."

    rationale = " ".join(rationale_parts) + f" {verdict}"

    logger.info(
        "vol_analyst.scored",
        ticker=ctx.ticker,
        score=final_score,
        abs_score=round(abs_score, 1),
        hist_score=round(hist_score, 1),
        mkt_score=round(mkt_score, 1),
        signal_count=len(signals),
    )

    return SubScore(
        agent="volatility_analyst",
        score=final_score,
        rationale=rationale,
        signals=signals,
        skipped=False,
        skip_reason=None,
    )


def _score_absolute_value(
    ctx: VolContext,
    config: VolScoringConfig,
) -> tuple[float, list[str], list[str]]:
    """Is the option cheap or expensive right now?

    Returns (score, signals, rationale_parts).
    """

    score = 50.0
    signals: list[str] = []
    rationale: list[str] = []

    # --- IV Rank ---
    if ctx.iv_rank <= config.iv_rank_low_threshold:
        score += config.iv_rank_low_bonus
        signals.append("iv_rank_low")
        rationale.append(f"IV rank at {ctx.iv_rank:.0f}th percentile — cheap vol.")
    elif ctx.iv_rank <= config.iv_rank_mid_low_threshold:
        score += config.iv_rank_mid_low_bonus
        signals.append("iv_rank_mid_low")
        rationale.append(f"IV rank at {ctx.iv_rank:.0f}th percentile — below average.")
    elif ctx.iv_rank >= config.iv_rank_high_threshold:
        score -= config.iv_rank_high_penalty
        signals.append("iv_rank_high")
        rationale.append(f"IV rank at {ctx.iv_rank:.0f}th percentile — expensive vol.")
    elif ctx.iv_rank >= config.iv_rank_mid_high_threshold:
        score -= config.iv_rank_mid_high_penalty
        signals.append("iv_rank_mid_high")
        rationale.append(f"IV rank at {ctx.iv_rank:.0f}th percentile — above average.")

    # --- IV vs Realized Vol ---
    if ctx.iv_rv_ratio <= config.iv_rv_deep_discount_threshold:
        score += config.iv_rv_deep_discount_bonus
        signals.append("iv_deep_below_rv")
        rationale.append(
            f"IV/RV ratio {ctx.iv_rv_ratio:.2f} — buying well below realized vol."
        )
    elif ctx.iv_rv_ratio <= config.iv_rv_discount_threshold:
        score += config.iv_rv_discount_bonus
        signals.append("iv_below_rv")
        rationale.append(f"IV/RV ratio {ctx.iv_rv_ratio:.2f} — buying below realized vol.")
    elif ctx.iv_rv_ratio >= config.iv_rv_extreme_premium_threshold:
        score -= config.iv_rv_extreme_premium_penalty
        signals.append("iv_extreme_premium")
        rationale.append(
            f"IV/RV ratio {ctx.iv_rv_ratio:.2f} — extreme premium over realized."
        )
    elif ctx.iv_rv_ratio >= config.iv_rv_premium_threshold:
        score -= config.iv_rv_premium_penalty
        signals.append("iv_premium")
        rationale.append(
            f"IV/RV ratio {ctx.iv_rv_ratio:.2f} — paying heavy premium over realized."
        )
    elif ctx.iv_rv_ratio >= config.iv_rv_mild_premium_threshold:
        score -= config.iv_rv_mild_premium_penalty
        signals.append("iv_mild_premium")
        rationale.append(
            f"IV/RV ratio {ctx.iv_rv_ratio:.2f} — moderate premium over realized."
        )

    # --- Theta decay rate ---
    if ctx.theta_pct_of_premium >= config.theta_extreme_threshold:
        score -= config.theta_extreme_penalty
        signals.append("theta_extreme")
        rationale.append(f"Theta decay {ctx.theta_pct_of_premium:.1%}/day — burning fast.")
    elif ctx.theta_pct_of_premium >= config.theta_high_threshold:
        score -= config.theta_high_penalty
        signals.append("theta_high")
        rationale.append(
            f"Theta decay {ctx.theta_pct_of_premium:.1%}/day — elevated bleed."
        )
    elif ctx.theta_pct_of_premium <= config.theta_low_threshold:
        score += config.theta_low_bonus
        signals.append("theta_low")
        rationale.append(f"Theta decay {ctx.theta_pct_of_premium:.1%}/day — slow bleed.")

    # --- Vega × IV rank interaction ---
    vega_as_pct = (
        ctx.contract_vega / ctx.contract_mid_price if ctx.contract_mid_price > 0.01 else 0.0
    )
    if vega_as_pct >= config.vega_high_threshold:
        if ctx.iv_rank <= config.iv_rank_low_threshold:
            score += config.vega_iv_synergy_bonus
            signals.append("vega_iv_synergy")
            rationale.append("High vega at low IV — leveraged to vol expansion.")
        elif ctx.iv_rank >= config.iv_rank_high_threshold:
            score -= config.vega_iv_conflict_penalty
            signals.append("vega_iv_conflict")
            rationale.append("High vega at high IV — exposed to vol contraction.")

    return (score, signals, rationale)


def _score_relative_to_history(
    ctx: VolContext,
    config: VolScoringConfig,
) -> tuple[float, list[str], list[str]]:
    """Is this cheap relative to what this ticker usually prices?

    Returns (score, signals, rationale_parts).
    """

    score = 50.0
    signals: list[str] = []
    rationale: list[str] = []

    # --- IV percentile vs rank divergence ---
    divergence = ctx.iv_percentile - ctx.iv_rank
    if abs(divergence) >= config.pctl_rank_divergence_threshold:
        if divergence < 0:
            score += config.pctl_below_rank_bonus
            signals.append("pctl_below_rank")
            rationale.append(
                f"IV percentile ({ctx.iv_percentile:.0f}) below rank ({ctx.iv_rank:.0f}) "
                "— historically vol was higher, current level is cheap."
            )
        else:
            score -= config.pctl_above_rank_penalty
            signals.append("pctl_above_rank")
            rationale.append(
                f"IV percentile ({ctx.iv_percentile:.0f}) above rank ({ctx.iv_rank:.0f}) "
                "— historically vol was lower, current level is elevated."
            )

    # --- Term structure shape ---
    is_near_term = ctx.candidate_dte <= config.near_term_dte_threshold

    if ctx.term_structure_slope < config.term_inversion_threshold:
        if is_near_term:
            score += config.term_inversion_near_expiry_bonus
            signals.append("term_inverted_near")
            rationale.append(
                "Term structure inverted — market pricing near-term catalyst. "
                "Candidate expiry aligns with the expected event window."
            )
        else:
            score += config.term_inversion_far_expiry_bonus
            signals.append("term_inverted_far")
            rationale.append(
                "Term structure inverted (near-term catalyst expected), "
                "but candidate is far-dated — partial alignment."
            )
    elif ctx.term_structure_slope > config.term_contango_threshold:
        if is_near_term:
            score -= config.term_contango_near_penalty
            signals.append("term_contango_near")
            rationale.append(
                f"Steep contango (slope {ctx.term_structure_slope:.1%}) — "
                "near-term vol elevated vs structure, buyer may be overpaying."
            )
        else:
            score += config.term_contango_far_bonus
            signals.append("term_contango_far")
            rationale.append(
                "Contango is normal; far-dated expiry benefits from lower near-term decay."
            )

    # --- Realized vol regime shift ---
    if ctx.rv_regime_ratio >= config.rv_expansion_threshold:
        score += config.rv_expansion_bonus
        signals.append("rv_expanding")
        rationale.append(
            f"RV expanding (20d/60d ratio {ctx.rv_regime_ratio:.2f}) — "
            "stock is becoming more volatile, good timing for buyers."
        )
    elif ctx.rv_regime_ratio <= config.rv_compression_threshold:
        score -= config.rv_compression_penalty
        signals.append("rv_compressing")
        rationale.append(
            f"RV compressing (20d/60d ratio {ctx.rv_regime_ratio:.2f}) — "
            "stock is calming down, buyer may be late."
        )

    return (score, signals, rationale)


def _score_market_context(
    ctx: VolContext,
    sector_cache: SectorBenchmarkCache,
    config: VolScoringConfig,
) -> tuple[float, list[str], list[str]]:
    """Is this cheap compared to the broader market and sector peers?

    Returns (score, signals, rationale_parts).
    """

    score = 50.0
    signals: list[str] = []
    rationale: list[str] = []

    # --- Ticker IV rank vs market (SPY) IV rank ---
    market_rank = sector_cache.market_iv_rank
    rank_gap = ctx.iv_rank - market_rank

    ticker_expensive_vs_market = False
    if abs(rank_gap) >= config.market_divergence_threshold:
        if rank_gap < 0:
            score += config.ticker_cheap_market_expensive_bonus
            signals.append("ticker_cheap_vs_market")
            rationale.append(
                f"Ticker IV rank ({ctx.iv_rank:.0f}) well below market ({market_rank:.0f}) "
                "— relative bargain vs broad market."
            )
        else:
            score -= config.ticker_expensive_market_cheap_penalty
            signals.append("ticker_expensive_vs_market")
            ticker_expensive_vs_market = True
            rationale.append(
                f"Ticker IV rank ({ctx.iv_rank:.0f}) well above market ({market_rank:.0f}) "
                "— paying a ticker-specific premium."
            )

    # --- IV/RV ratio vs sector peers ---
    sector_bench = sector_cache.get_sector_fuzzy(ctx.sector)
    if sector_bench is not None:
        if (not ticker_expensive_vs_market) and (ctx.iv_rv_ratio < sector_bench.iv_rv_ratio_median):
            score += config.sector_below_median_bonus
            signals.append("sector_cheap")
            rationale.append(
                f"IV/RV ratio ({ctx.iv_rv_ratio:.2f}) below sector median "
                f"({sector_bench.iv_rv_ratio_median:.2f}) — cheaper than peers."
            )
        elif ctx.iv_rv_ratio > sector_bench.iv_rv_ratio_p75:
            score -= config.sector_above_p75_penalty
            signals.append("sector_expensive")
            rationale.append(
                f"IV/RV ratio ({ctx.iv_rv_ratio:.2f}) above sector 75th percentile "
                f"({sector_bench.iv_rv_ratio_p75:.2f}) — pricier than most peers."
            )
    else:
        rationale.append(
            f"No sector benchmark available for '{ctx.sector}' — skipping peer comparison."
        )

    # --- Delta positioning ---
    if (not ticker_expensive_vs_market) and (config.delta_sweet_low <= ctx.contract_delta <= config.delta_sweet_high):
        score += config.delta_sweet_bonus
        signals.append("delta_sweet_spot")
        rationale.append(
            f"Delta {ctx.contract_delta:.2f} in sweet spot — reasonable probability of profit."
        )
    elif ctx.contract_delta < config.delta_deep_otm_threshold:
        score -= config.delta_deep_otm_penalty
        signals.append("delta_lottery")
        rationale.append(f"Delta {ctx.contract_delta:.2f} — deep OTM lottery ticket pricing.")
    elif ctx.contract_delta > config.delta_deep_itm_threshold:
        score -= config.delta_deep_itm_penalty
        signals.append("delta_deep_itm")
        rationale.append(
            f"Delta {ctx.contract_delta:.2f} — deep ITM, mostly paying for intrinsic value."
        )

    return (score, signals, rationale)

