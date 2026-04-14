"""
Flow analyst — deterministic Gate 1 filter.

Scores the mechanics of an option trade on a 1-100 scale.
No LLM calls, no external API calls, no async needed.
All thresholds imported from shared.filters.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from shared.filters import (
    EXPIRY_DAY_END_HOUR,
    EXPIRY_DAY_END_MINUTE,
    EXPIRY_DAY_END_SECOND,
    FLOW_SCORE_MAX,
    FLOW_SCORE_MIN,
    FLOW_SCORE_SKIPPED,
    FLOW_SCORING,
    OI_CHANGE_DECLINING_BELOW,
    SPOT_PRICE_INVALID_MAX,
    is_excluded_ticker,
)
from shared.models import Candidate, FillType, FlowCandidate, OptionType, SubScore


def candidate_to_flow(candidate: Candidate) -> FlowCandidate:
    """Map scanner Agent A output into the shape used for flow scoring."""
    try:
        d = date.fromisoformat(candidate.expiry)
        expiry_dt = datetime(
            d.year,
            d.month,
            d.day,
            EXPIRY_DAY_END_HOUR,
            EXPIRY_DAY_END_MINUTE,
            EXPIRY_DAY_END_SECOND,
            tzinfo=timezone.utc,
        )
    except (ValueError, TypeError):
        expiry_dt = datetime.now(timezone.utc)

    et = (candidate.execution_type or "").strip().lower()
    if et == "sweep":
        fill_type = FillType.SWEEP
    elif et == "block":
        fill_type = FillType.BLOCK
    else:
        fill_type = FillType.SPLIT

    opt = OptionType.CALL if candidate.direction == "bullish" else OptionType.PUT

    conf = max(len(candidate.signals), int(round(float(candidate.confluence_score))))
    signal_names = [s.rule_name for s in candidate.signals]

    return FlowCandidate(
        id=candidate.id,
        ticker=candidate.ticker,
        strike=candidate.strike,
        expiry=expiry_dt,
        option_type=opt,
        fill_type=fill_type,
        premium=candidate.premium_usd,
        spot_price=float(
            candidate.underlying_price
            if candidate.underlying_price is not None
            else SPOT_PRICE_INVALID_MAX
        ),
        volume=candidate.volume,
        open_interest=candidate.open_interest,
        oi_change=candidate.oi_change,
        confluence_score=conf,
        signals=signal_names,
        scanned_at=candidate.scanned_at,
        raw_data={},
        contract_avg_daily_volume=candidate.contract_avg_daily_volume,
    )


class FlowAnalyst:
    """Deterministic flow scoring engine.

    Evaluates trade mechanics from the FlowCandidate object only.
    Zero external dependencies at runtime.
    """

    def score(self, candidate: FlowCandidate) -> SubScore:
        excluded, reason = is_excluded_ticker(candidate.ticker)
        if excluded:
            return SubScore(
                agent="flow_analyst",
                score=FLOW_SCORE_SKIPPED,
                rationale=f"Excluded: {candidate.ticker} is {reason.value}",
                signals=[],
                skipped=True,
                skip_reason=f"ticker_excluded_{reason.value}",
            )

        cfg = FLOW_SCORING
        score = cfg.baseline
        signals: list[str] = []

        score, signals = self._score_premium(candidate.premium, score, signals, cfg)
        score, signals = self._score_fill_type(candidate.fill_type, score, signals, cfg)
        score, signals = self._score_oi_change(candidate.oi_change, score, signals, cfg)
        score, signals = self._score_otm(
            candidate.strike, candidate.spot_price, score, signals, cfg
        )
        score, signals = self._score_dte(candidate.expiry, score, signals, cfg)
        score, signals = self._score_illiquidity(candidate, score, signals, cfg)
        score, signals = self._score_confluence(
            candidate.confluence_score, score, signals, cfg
        )

        score = max(FLOW_SCORE_MIN, min(FLOW_SCORE_MAX, score))

        rationale = (
            f"Flow score {score}/100 for {candidate.ticker}: "
            f"{', '.join(signals) if signals else 'baseline only'}"
        )

        return SubScore(
            agent="flow_analyst",
            score=score,
            rationale=rationale,
            signals=signals,
        )

    def _score_premium(
        self, premium: float, score: int, signals: list[str], cfg
    ) -> tuple[int, list[str]]:
        if premium >= cfg.premium_tier_3_min:
            score += cfg.premium_tier_3_points
            signals.append("premium_over_500k")
        elif premium >= cfg.premium_tier_2_min:
            score += cfg.premium_tier_2_points
            signals.append("premium_over_100k")
        elif premium >= cfg.premium_tier_1_min:
            score += cfg.premium_tier_1_points
            signals.append("premium_over_25k")
        return score, signals

    def _score_fill_type(
        self, fill_type: FillType, score: int, signals: list[str], cfg
    ) -> tuple[int, list[str]]:
        if fill_type == FillType.SWEEP:
            score += cfg.sweep_points
            signals.append("sweep_fill")
        elif fill_type == FillType.BLOCK:
            score += cfg.block_points
            signals.append("block_fill")
        elif fill_type == FillType.SPLIT:
            score += cfg.split_points
            signals.append("split_fill")
        return score, signals

    def _score_oi_change(
        self, oi_change: float | None, score: int, signals: list[str], cfg
    ) -> tuple[int, list[str]]:
        if oi_change is None:
            return score, signals
        if oi_change >= cfg.oi_change_tier_3_min:
            score += cfg.oi_change_tier_3_points
            signals.append("oi_spike_5x")
        elif oi_change >= cfg.oi_change_tier_2_min:
            score += cfg.oi_change_tier_2_points
            signals.append("oi_spike_3x")
        elif oi_change >= cfg.oi_change_tier_1_min:
            score += cfg.oi_change_tier_1_points
            signals.append("oi_spike_1_5x")
        elif oi_change < OI_CHANGE_DECLINING_BELOW:
            score += cfg.oi_change_declining_points
            signals.append("oi_declining")
        return score, signals

    def _score_otm(
        self, strike: float, spot: float, score: int, signals: list[str], cfg
    ) -> tuple[int, list[str]]:
        if spot <= SPOT_PRICE_INVALID_MAX:
            return score, signals
        otm_pct = abs(strike - spot) / spot
        if otm_pct >= cfg.otm_deep_threshold:
            score += cfg.otm_deep_points
            signals.append("deep_otm_25pct")
        elif otm_pct >= cfg.otm_moderate_threshold:
            score += cfg.otm_moderate_points
            signals.append("otm_15pct")
        elif otm_pct >= cfg.otm_slight_threshold:
            score += cfg.otm_slight_points
            signals.append("otm_5pct")
        else:
            score += cfg.atm_itm_points
            signals.append("atm_or_itm")
        return score, signals

    def _score_dte(
        self, expiry: datetime, score: int, signals: list[str], cfg
    ) -> tuple[int, list[str]]:
        now = datetime.now(timezone.utc)
        if expiry.tzinfo is None:
            dte = (expiry - now.replace(tzinfo=None)).days
        else:
            dte = (expiry - now).days

        if dte <= cfg.dte_weekly_max:
            score += cfg.dte_weekly_points
            signals.append("weekly_dte_penalty")
        elif dte <= cfg.dte_near_max:
            score += cfg.dte_near_points
            signals.append("near_term_dte_neutral")
        elif dte <= cfg.dte_sweet_max:
            score += cfg.dte_sweet_points
            signals.append("sweet_spot_dte")
        elif dte >= cfg.dte_long_min:
            score += cfg.dte_long_points
            signals.append("long_dated_penalty")
        return score, signals

    def _score_illiquidity(
        self, candidate: FlowCandidate, score: int, signals: list[str], cfg
    ) -> tuple[int, list[str]]:
        adv = candidate.contract_avg_daily_volume
        if adv is None or adv < 0:
            return score, signals
        if adv < cfg.illiquidity_dead_chain_adv_max:
            score += cfg.illiquidity_dead_chain_points
            signals.append("dead_chain_adv_bonus")
        elif adv < cfg.illiquidity_low_adv_max:
            score += cfg.illiquidity_low_points
            signals.append("low_liquidity_adv_bonus")
        return score, signals

    def _score_confluence(
        self, confluence: int, score: int, signals: list[str], cfg
    ) -> tuple[int, list[str]]:
        if confluence >= cfg.confluence_high_min:
            score += cfg.confluence_high_points
            signals.append("high_confluence_5plus")
        elif confluence >= cfg.confluence_moderate_min:
            score += cfg.confluence_moderate_points
            signals.append("moderate_confluence_4")
        elif confluence >= cfg.confluence_low_min:
            score += cfg.confluence_low_points
            signals.append("confluence_3")
        return score, signals
