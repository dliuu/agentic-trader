"""
Gate 1.5: Explainability Filter.

Deterministic penalty-based filter that kills flow explained by normal
market activity. Runs after Gate 1, before Gate 2.

Penalty categories:
  1. Standard earnings plays (–25)
  2. Hot tickers (–15 to –25)
  3. Sector rotation alignment (–10)
  4. Recent catalyst alignment (–20)

Combined score = gate1_score + total_penalty (capped at –50).
Threshold: combined_score >= 50 to proceed.
"""

from __future__ import annotations

from datetime import date, datetime

import httpx
import structlog

from grader.context.explainability_ctx import ExplainabilityContext, build_explainability_context
from shared.filters import (
    EXPLAINABILITY_CONFIG,
    ExplainabilityConfig,
    GateThresholds,
)
from shared.models import Candidate, SubScore

log = structlog.get_logger()


class Gate15Result:
    """Result of Gate 1.5 explainability check."""

    __slots__ = (
        "passed",
        "penalty",
        "combined_score",
        "reasons",
        "earnings_penalty",
        "hot_ticker_penalty",
        "sector_penalty",
        "catalyst_penalty",
    )

    def __init__(
        self,
        passed: bool,
        penalty: int,
        combined_score: int,
        reasons: list[str],
        earnings_penalty: int = 0,
        hot_ticker_penalty: int = 0,
        sector_penalty: int = 0,
        catalyst_penalty: int = 0,
    ):
        self.passed = passed
        self.penalty = penalty
        self.combined_score = combined_score
        self.reasons = reasons
        self.earnings_penalty = earnings_penalty
        self.hot_ticker_penalty = hot_ticker_penalty
        self.sector_penalty = sector_penalty
        self.catalyst_penalty = catalyst_penalty


def _parse_expiry_date(expiry: str) -> date | None:
    s = (expiry or "").strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        try:
            return date.fromisoformat(s[:10])
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.date()
    except ValueError:
        return None


def _parse_earnings_date(earnings_date: str | None) -> date | None:
    if not earnings_date:
        return None
    s = earnings_date.strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        try:
            return date.fromisoformat(s[:10])
        except ValueError:
            pass
    return None


def _check_earnings_play(
    candidate: Candidate,
    ctx: ExplainabilityContext,
    cfg: ExplainabilityConfig,
) -> int:
    if ctx.days_to_earnings is None:
        return 0
    if ctx.days_to_earnings < 0:
        return 0
    if ctx.days_to_earnings > cfg.earnings_window_days:
        return 0

    earnings_d = _parse_earnings_date(ctx.earnings_date)
    if earnings_d is None:
        return 0

    expiry_d = _parse_expiry_date(candidate.expiry)
    if expiry_d is None:
        return 0

    days_expiry_after_earnings = (expiry_d - earnings_d).days
    if days_expiry_after_earnings < 0:
        return 0
    if days_expiry_after_earnings > cfg.earnings_expiry_buffer_days:
        return 0

    return cfg.earnings_penalty


def _check_hot_ticker(ctx: ExplainabilityContext, cfg: ExplainabilityConfig) -> int:
    count = ctx.flow_alert_count_14d
    if count >= cfg.hot_ticker_threshold_3:
        return cfg.hot_ticker_penalty_3
    if count >= cfg.hot_ticker_threshold_2:
        return cfg.hot_ticker_penalty_2
    if count >= cfg.hot_ticker_threshold_1:
        return cfg.hot_ticker_penalty_1
    return 0


def _check_sector_alignment(
    candidate: Candidate,
    ctx: ExplainabilityContext,
    cfg: ExplainabilityConfig,
) -> int:
    if ctx.sector_call_put_ratio is None:
        return 0

    direction = candidate.direction
    if direction == "bullish" and ctx.sector_call_put_ratio >= cfg.sector_bullish_cp_threshold:
        return cfg.sector_alignment_penalty
    if direction == "bearish" and ctx.sector_call_put_ratio <= cfg.sector_bearish_cp_threshold:
        return cfg.sector_alignment_penalty
    return 0


def _check_catalyst_alignment(
    candidate: Candidate,
    ctx: ExplainabilityContext,
    cfg: ExplainabilityConfig,
) -> int:
    if not ctx.headlines_48h:
        return 0

    direction = candidate.direction

    for headline in ctx.headlines_48h:
        title_lower = headline["title"].lower()

        if direction == "bullish":
            if any(kw in title_lower for kw in cfg.bullish_catalyst_keywords):
                return cfg.catalyst_alignment_penalty
        elif direction == "bearish":
            if any(kw in title_lower for kw in cfg.bearish_catalyst_keywords):
                return cfg.catalyst_alignment_penalty

        if any(kw in title_lower for kw in cfg.neutral_catalyst_keywords):
            if len(ctx.headlines_48h) >= 2:
                return cfg.catalyst_alignment_penalty

    return 0


async def run_gate1_5(
    candidate: Candidate,
    flow_score: SubScore,
    client: httpx.AsyncClient,
    api_token: str,
    *,
    scanner_db_path: str | None = None,
    sector: str | None = None,
    config: ExplainabilityConfig | None = None,
    gate_cfg: GateThresholds | None = None,
) -> Gate15Result:
    cfg = config or EXPLAINABILITY_CONFIG
    thresholds = gate_cfg or GateThresholds()

    ctx = await build_explainability_context(
        candidate,
        client,
        api_token,
        scanner_db_path=scanner_db_path,
        sector=sector,
        config=cfg,
    )

    reasons: list[str] = []
    earnings_pen = _check_earnings_play(candidate, ctx, cfg)
    hot_pen = _check_hot_ticker(ctx, cfg)
    sector_pen = _check_sector_alignment(candidate, ctx, cfg)
    catalyst_pen = _check_catalyst_alignment(candidate, ctx, cfg)

    if earnings_pen:
        reasons.append(f"earnings_play({earnings_pen})")
    if hot_pen:
        reasons.append(f"hot_ticker({hot_pen}, count={ctx.flow_alert_count_14d})")
    if sector_pen:
        reasons.append(f"sector_aligned({sector_pen})")
    if catalyst_pen:
        reasons.append(f"catalyst_aligned({catalyst_pen})")

    raw_penalty = earnings_pen + hot_pen + sector_pen + catalyst_pen
    total_penalty = max(raw_penalty, cfg.max_total_penalty)

    combined = flow_score.score + total_penalty
    passed = combined >= thresholds.gate1_5_combined_min

    log.info(
        "gate1_5.result",
        ticker=candidate.ticker,
        flow_score=flow_score.score,
        penalty=total_penalty,
        combined=combined,
        threshold=thresholds.gate1_5_combined_min,
        passed=passed,
        reasons=reasons,
    )

    return Gate15Result(
        passed=passed,
        penalty=total_penalty,
        combined_score=combined,
        reasons=reasons,
        earnings_penalty=earnings_pen,
        hot_ticker_penalty=hot_pen,
        sector_penalty=sector_pen,
        catalyst_penalty=catalyst_pen,
    )
