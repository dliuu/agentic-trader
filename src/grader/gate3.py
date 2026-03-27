"""Gate 3 — parallel LLM specialists, aggregation, synthesis, final threshold."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import structlog

from grader.aggregator import Aggregator
from grader.models import ScoredTrade
from grader.synthesis import SynthesisAgent, SynthesisParseError, log_synthesis_grade
from shared.filters import GATE_THRESHOLDS, GateThresholds
from shared.models import Candidate, SubScore

log = structlog.get_logger()

AGENT_ORDER = ("sentiment_analyst", "insider_tracker", "sector_analyst")


async def _run_llm_specialists(
    candidate: Candidate,
    sentiment: Any,
    insider: Any,
    sector: Any,
) -> list[SubScore]:
    """Run sentiment, insider, sector in parallel; resilient SubScores on failure."""
    results = await asyncio.gather(
        sentiment.score(candidate),
        insider.score(candidate),
        sector.score(candidate),
        return_exceptions=True,
    )

    scores: list[SubScore] = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            agent_name = AGENT_ORDER[i]
            log.error(
                "gate3.agent_error",
                agent=agent_name,
                ticker=candidate.ticker,
                error=str(result),
            )
            scores.append(
                SubScore(
                    agent=agent_name,
                    score=50,
                    rationale=f"Agent failed: {result}",
                    signals=[],
                    skipped=True,
                    skip_reason=str(result),
                )
            )
        else:
            scores.append(result)
    return scores


async def run_gate3(
    candidate: Candidate,
    flow_score: SubScore,
    vol_score: SubScore,
    risk_score: SubScore,
    sentiment: Any,
    insider: Any,
    sector: Any,
    synthesis_agent: SynthesisAgent,
    aggregator: Aggregator,
    final_threshold: int | None = None,
    gate_cfg: GateThresholds | None = None,
) -> ScoredTrade | None:
    """Full Gate 3: specialists → aggregate → synthesis → optional pass ScoredTrade."""
    if gate_cfg is None:
        gate_cfg = GATE_THRESHOLDS
    threshold = final_threshold if final_threshold is not None else gate_cfg.final_score_min

    log.info("gate3.llm_agents.start", ticker=candidate.ticker)

    g3_scores = await _run_llm_specialists(candidate, sentiment, insider, sector)

    log.info(
        "gate3.llm_agents.complete",
        ticker=candidate.ticker,
        sentiment=g3_scores[0].score,
        insider=g3_scores[1].score,
        sector=g3_scores[2].score,
    )

    sub_scores: dict[str, SubScore] = {
        flow_score.agent: flow_score,
        vol_score.agent: vol_score,
        risk_score.agent: risk_score,
        g3_scores[0].agent: g3_scores[0],
        g3_scores[1].agent: g3_scores[1],
        g3_scores[2].agent: g3_scores[2],
    }

    aggregated = aggregator.aggregate(sub_scores)
    log.info(
        "gate3.aggregated",
        ticker=candidate.ticker,
        weighted_avg=round(aggregated.weighted_average, 2),
        stdev=round(aggregated.score_stdev, 2),
        agreement=aggregated.agent_agreement,
        conflicts=[c.name for c in aggregated.conflict_flags],
    )

    try:
        grade, llm_resp, risk_params = await synthesis_agent.synthesize(
            candidate, sub_scores, aggregated
        )
    except SynthesisParseError as e:
        log.error("gate3.synthesis_failed", ticker=candidate.ticker, error=str(e))
        return None

    await log_synthesis_grade(candidate, grade, llm_resp)

    if grade.score >= threshold:
        log.info(
            "gate3.passed",
            ticker=candidate.ticker,
            score=grade.score,
            verdict=grade.verdict,
            threshold=threshold,
        )
        return ScoredTrade(
            candidate=candidate,
            grade=grade,
            risk=risk_params,
            graded_at=datetime.now(timezone.utc),
            model_used=llm_resp.model,
            latency_ms=llm_resp.latency_ms,
            input_tokens=llm_resp.input_tokens,
            output_tokens=llm_resp.output_tokens,
        )

    log.info(
        "gate3.filtered",
        ticker=candidate.ticker,
        score=grade.score,
        threshold=threshold,
    )
    return None
