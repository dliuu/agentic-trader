"""Risk analyst (Gate 2) — placeholder implementation.

The volatility analyst spec requires Gate 2 wiring to run a risk analyst in parallel.
Risk scoring is implemented separately; this stub keeps the pipeline importable and
returns a neutral skipped SubScore.
"""

from __future__ import annotations

from shared.models import Candidate, SubScore


async def score_risk(candidate: Candidate, client, api_token: str) -> SubScore:
    return SubScore(
        agent="risk_analyst",
        score=50,
        rationale="Risk analyst not implemented — returning neutral score.",
        signals=[],
        skipped=True,
        skip_reason="not_implemented",
    )

