"""Deterministic sector analyst (Gate 3) — no LLM calls."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

import httpx
import structlog

from grader.agents.sector_scoring_config import SECTOR_SCORING, SectorScoringConfig
from grader.context.sector_ctx import EconomicEvent, SectorContext, build_sector_context
from shared.models import Candidate, SubScore

log = structlog.get_logger()


@dataclass
class SectorAnalystResult:
    score: int
    rationale: str
    signals: list[str]
    skipped: bool
    skip_reason: str | None
    has_fda_flag: bool
    component_scores: dict[str, int]


def _parse_event_to_date(s: str) -> date | None:
    s = (s or "").strip()
    if not s:
        return None
    if "T" in s:
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
        except ValueError:
            pass
    if len(s) >= 10 and s[4] == "-":
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").date()
        except ValueError:
            pass
    try:
        return datetime.strptime(s[:10], "%m/%d/%Y").date()
    except ValueError:
        return None


def _score_sector_flow(
    ctx: SectorContext, cfg: SectorScoringConfig
) -> tuple[int, list[str]]:
    if ctx.sector_tide is None:
        return 0, ["sector_tide_unavailable"]
    cp = ctx.sector_tide.call_put_ratio
    if cp >= cfg.sector_cp_ratio_strong_bullish:
        pts = cfg.sector_strong_bullish_pts
        sigs = ["sector_flow_strong_bullish"]
    elif cp >= cfg.sector_cp_ratio_bullish:
        pts = cfg.sector_bullish_pts
        sigs = ["sector_flow_bullish"]
    elif cp > cfg.sector_cp_ratio_bearish:
        pts = cfg.sector_neutral_pts
        sigs = ["sector_flow_neutral"]
    elif cp > cfg.sector_cp_ratio_strong_bearish:
        pts = cfg.sector_bearish_pts
        sigs = ["sector_flow_bearish"]
    else:
        pts = cfg.sector_strong_bearish_pts
        sigs = ["sector_flow_strong_bearish"]

    net = ctx.sector_tide.net_premium
    if net > 0:
        sigs.append("sector_net_premium_positive")
    elif net < 0:
        sigs.append("sector_net_premium_negative")

    if ctx.sector_etf is not None:
        p1 = ctx.sector_etf.performance_1d
        if p1 >= cfg.sector_etf_1d_strong:
            pts += cfg.sector_etf_strong_pts
            sigs.append("sector_etf_strong_day")
        elif p1 <= cfg.sector_etf_1d_weak:
            pts += cfg.sector_etf_weak_pts
            sigs.append("sector_etf_weak_day")

    return pts, sigs


def _score_market_tide(
    ctx: SectorContext, cfg: SectorScoringConfig
) -> tuple[int, list[str]]:
    if ctx.market_tide is None:
        return 0, ["market_tide_unavailable"]
    cp = ctx.market_tide.call_put_ratio
    if cp >= cfg.market_cp_ratio_strong_bullish:
        return cfg.market_strong_bullish_pts, ["market_strong_bullish"]
    if cp >= cfg.market_cp_ratio_bullish:
        return cfg.market_bullish_pts, ["market_bullish"]
    if cp > cfg.market_cp_ratio_bearish:
        return cfg.market_neutral_pts, ["market_neutral"]
    if cp > cfg.market_cp_ratio_strong_bearish:
        return cfg.market_bearish_pts, ["market_bearish"]
    return cfg.market_strong_bearish_pts, ["market_strong_bearish"]


def _score_economic_calendar(
    ctx: SectorContext,
    cfg: SectorScoringConfig,
    reference_date: date | None = None,
) -> tuple[int, list[str]]:
    ref = reference_date or date.today()
    events = list(ctx.high_impact_events)

    future_events: list[tuple[date, EconomicEvent]] = []
    for e in events:
        ed = _parse_event_to_date(e.date)
        if ed is None:
            continue
        if ed >= ref:
            future_events.append((ed, e))

    if not future_events:
        return cfg.econ_no_events_pts, ["no_high_impact_econ_events"]

    nearest = min(future_events, key=lambda x: x[0])
    days = (nearest[0] - ref).days
    if days <= 3:
        return cfg.econ_high_impact_within_3d_pts, ["high_impact_econ_within_3d"]
    if days <= 7:
        return cfg.econ_high_impact_within_7d_pts, ["high_impact_econ_within_7d"]
    return 0, ["high_impact_econ_distant"]


def _check_fda_flag(ctx: SectorContext) -> list[str]:
    if not ctx.is_biotech:
        return []
    if ctx.has_upcoming_fda:
        return [
            f"fda_upcoming:{f.ticker} | {f.event_type} | {f.drug_name} | {f.date}"
            for f in ctx.fda_dates
        ]
    return [f"biotech_no_fda_dates:{ctx.ticker}"]


def score_sector(
    ctx: SectorContext,
    cfg: SectorScoringConfig | None = None,
    reference_date: date | None = None,
) -> SectorAnalystResult:
    """Compute deterministic 1–100 score from sector context."""
    cfg = cfg or SECTOR_SCORING
    sector_pts, sector_sigs = _score_sector_flow(ctx, cfg)
    market_pts, market_sigs = _score_market_tide(ctx, cfg)
    econ_pts, econ_sigs = _score_economic_calendar(ctx, cfg, reference_date=reference_date)
    fda_sigs = _check_fda_flag(ctx)

    weighted_delta = (
        sector_pts * cfg.weight_sector_flow
        + market_pts * cfg.weight_market_tide
        + econ_pts * cfg.weight_economic
    )
    raw = cfg.baseline + weighted_delta
    final = int(round(raw))
    final = max(cfg.score_min, min(cfg.score_max, final))

    all_signals = sector_sigs + market_sigs + econ_sigs + fda_sigs

    rationale = (
        f"Score {final}/100 (baseline {cfg.baseline}): "
        f"sector_flow raw {sector_pts} pts (weight {cfg.weight_sector_flow}), "
        f"market_tide raw {market_pts} pts (weight {cfg.weight_market_tide}), "
        f"economic raw {econ_pts} pts (weight {cfg.weight_economic}); "
        f"weighted delta {weighted_delta:.2f}."
    )

    return SectorAnalystResult(
        score=final,
        rationale=rationale,
        signals=all_signals,
        skipped=False,
        skip_reason=None,
        has_fda_flag=ctx.has_upcoming_fda,
        component_scores={
            "sector_flow": sector_pts,
            "market_tide": market_pts,
            "economic": econ_pts,
        },
    )


class SectorAnalyst:
    """Gate 3 deterministic sector analyst — builds `SectorContext` and scores it."""

    name = "sector_analyst"

    def __init__(
        self,
        uw_client: httpx.AsyncClient,
        uw_api_token: str,
        scoring: SectorScoringConfig | None = None,
    ):
        self._client = uw_client
        self._token = uw_api_token
        self._scoring = scoring or SECTOR_SCORING

    async def score(self, candidate: Candidate) -> SubScore:
        try:
            ctx = await build_sector_context(
                candidate.ticker,
                self._client,
                self._token,
                ticker_sector=None,
            )
        except Exception as e:
            log.error("sector_analyst.context_failed", ticker=candidate.ticker, error=str(e))
            return SubScore(
                agent=self.name,
                score=50,
                rationale=f"Sector context build failed: {e}",
                signals=[],
                skipped=True,
                skip_reason=str(e),
            )

        result = score_sector(ctx, self._scoring)
        return SubScore(
            agent=self.name,
            score=result.score,
            rationale=result.rationale,
            signals=result.signals,
            skipped=result.skipped,
            skip_reason=result.skip_reason,
        )
