"""
Gate 2 - Deterministic gate running vol analyst + risk analyst in parallel.

Entry: Candidates that passed Gate 1 (flow_score >= 40).
Exit: Candidates where avg(flow_score, vol_score, risk_score) >= 45.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from grader.agents.risk_analyst import score_risk_conviction
from grader.agents.volatility_analyst import score_volatility
from grader.context.risk_ctx import fetch_risk_context
from grader.context.sector_cache import SectorBenchmarkCache
from shared.filters import GateThresholds, RiskConvictionConfig
from shared.models import Candidate, SubScore

logger = structlog.get_logger()


async def run_gate2(
    candidate: Candidate,
    flow_score: SubScore,
    client: Any,
    api_token: str,
    sector_cache: SectorBenchmarkCache,
    risk_cfg: RiskConvictionConfig | None = None,
    gate_cfg: GateThresholds | None = None,
) -> tuple[bool, SubScore, SubScore]:
    """Run Gate 2: volatility + risk analysts in parallel.

    Returns (passed, vol_sub_score, risk_sub_score).
    """
    if gate_cfg is None:
        gate_cfg = GateThresholds()

    from grader.agents.flow_analyst import candidate_to_flow

    flow_candidate = candidate_to_flow(candidate)

    risk_ctx_task = fetch_risk_context(flow_candidate, client)
    vol_score_task = score_volatility(candidate, client, api_token, sector_cache)
    risk_ctx, vol_score = await asyncio.gather(risk_ctx_task, vol_score_task)

    risk_score = score_risk_conviction(
        candidate=flow_candidate,
        option_chain_data=risk_ctx["option_chain_data"],
        annualized_realized_vol=risk_ctx["annualized_realized_vol"],
        days_to_earnings=risk_ctx["days_to_earnings"],
        cfg=risk_cfg,
    )

    if risk_score.untradeable or risk_score.recommended_position_size == 0.0:
        logger.info(
            "gate2.short_circuit",
            ticker=candidate.ticker,
            reason="untradeable_or_zero_size",
            data_gaps=risk_score.data_gaps,
        )
        return False, vol_score, risk_score

    gate_avg = (flow_score.score + vol_score.score + risk_score.score) / 3
    passed = gate_avg >= gate_cfg.deterministic_avg_min
    logger.info(
        "gate2.result",
        ticker=candidate.ticker,
        flow=flow_score.score,
        risk=risk_score.score,
        vol=vol_score.score,
        avg=round(gate_avg, 1),
        threshold=gate_cfg.deterministic_avg_min,
        passed=passed,
    )
    return passed, vol_score, risk_score

