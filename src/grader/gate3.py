"""Gate 3 - LLM analyst layer (sentiment + insider + sector) in parallel."""

from __future__ import annotations

import asyncio
from typing import Any

from shared.models import Candidate, SubScore

AGENT_ORDER = ("sentiment_analyst", "insider_tracker", "sector_analyst")


async def run_gate3(
    candidate: Candidate,
    sentiment: Any,
    insider: Any,
    sector: Any,
) -> list[SubScore]:
    """Run Gate 3 analysts in parallel and return resilient SubScores."""
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
