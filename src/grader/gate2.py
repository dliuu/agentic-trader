"""Gate 2: deterministic volatility + risk scoring.

Runs volatility analyst and risk analyst in parallel and checks the combined threshold:
avg(flow, vol, risk) >= GateThresholds.gate2_avg_threshold (default: 45).
"""

from __future__ import annotations

import asyncio

from grader.agents.risk_analyst import score_risk
from grader.agents.volatility_analyst import score_volatility
from grader.context.sector_cache import SectorBenchmarkCache
from shared.filters import GateThresholds
from shared.models import Candidate, SubScore


async def run_gate2(
    candidate: Candidate,
    flow_score: SubScore,
    client,
    api_token: str,
    sector_cache: SectorBenchmarkCache,
) -> tuple[bool, SubScore, SubScore]:
    """Run Gate 2: volatility + risk analysts in parallel.

    Returns (passed, vol_sub_score, risk_sub_score).
    """

    vol_score, risk_score = await asyncio.gather(
        score_volatility(candidate, client, api_token, sector_cache),
        score_risk(candidate, client, api_token),
    )

    gate_avg = (flow_score.score + vol_score.score + risk_score.score) / 3
    thresholds = GateThresholds()
    passed = gate_avg >= thresholds.gate2_avg_threshold
    return passed, vol_score, risk_score

