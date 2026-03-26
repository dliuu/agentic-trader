"""Gate 1: Flow analyst filter.

Sits between the scanner queue and the rest of the grading pipeline.
Discards candidates that score below the threshold.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import structlog

from grader.agents.flow_analyst import FlowAnalyst, candidate_to_flow
from shared.db import get_db
from shared.filters import GATE_THRESHOLDS
from shared.models import Candidate, SubScore

log = structlog.get_logger()

_analyst = FlowAnalyst()


async def run_gate1(candidate: Candidate) -> tuple[bool, SubScore]:
    """Score a candidate through Gate 1.

    Returns:
        (passed, sub_score) — passed is True if score >= threshold
    """
    flow_input = candidate_to_flow(candidate)
    sub_score = _analyst.score(flow_input)

    await _log_score(candidate, sub_score)

    passed = (
        not sub_score.skipped
        and sub_score.score >= GATE_THRESHOLDS.flow_analyst_min
    )

    if passed:
        log.info(
            "gate1_pass",
            ticker=candidate.ticker,
            score=sub_score.score,
            signals=sub_score.signals,
        )
    else:
        log.info(
            "gate1_reject",
            ticker=candidate.ticker,
            score=sub_score.score,
            reason=sub_score.skip_reason or "below_threshold",
        )

    return passed, sub_score


async def _log_score(candidate: Candidate, sub_score: SubScore) -> None:
    db = await get_db()
    try:
        await db.execute(
            """INSERT OR REPLACE INTO flow_scores
               (candidate_id, score, rationale, signals, skipped, skip_reason, scored_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                candidate.id,
                sub_score.score,
                sub_score.rationale,
                json.dumps(sub_score.signals),
                int(sub_score.skipped),
                sub_score.skip_reason,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await db.commit()
    finally:
        await db.close()
