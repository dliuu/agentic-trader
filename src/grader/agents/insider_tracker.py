"""Insider tracker agent (Gate 3, LLM-powered)."""

from __future__ import annotations

import httpx
import structlog

from grader.context.insider_ctx import (
    InsiderContext,
    build_insider_context,
    make_skip_score,
    should_skip_insider_analysis,
)
from grader.llm_client import LLMClient
from grader.models import GradeResponse
from grader.parser import ParseError, parse_llm_response
from grader.prompt import INSIDER_TRACKER_SYSTEM_PROMPT, build_insider_tracker_user_prompt
from shared.finnhub_client import FinnhubClient
from shared.filters import InsiderScoringConfig
from shared.models import Candidate, SubScore

log = structlog.get_logger()


class InsiderTracker:
    """LLM-powered agent that evaluates insider and congressional alignment."""

    name = "insider_tracker"

    def __init__(
        self,
        uw_client: httpx.AsyncClient,
        uw_api_token: str,
        finnhub_api_key: str,
        llm_client: LLMClient,
        config: InsiderScoringConfig | None = None,
    ):
        self._uw = uw_client
        self._token = uw_api_token
        self._finnhub = FinnhubClient(uw_client, finnhub_api_key)
        self._llm = llm_client
        self._cfg = config or InsiderScoringConfig()

    def _apply_confidence_adjustment(self, raw_score: int, ctx: InsiderContext) -> int:
        """Pull extreme scores toward 50 when data is sparse."""
        available_count = sum(1 for v in ctx.data_availability.values() if v)
        total_sources = len(ctx.data_availability)
        if available_count >= self._cfg.min_sources_for_full_confidence:
            return raw_score
        confidence = max(
            self._cfg.min_confidence_factor,
            available_count / total_sources if total_sources else self._cfg.min_confidence_factor,
        )
        adjusted = 50 + (raw_score - 50) * confidence
        return max(1, min(100, round(adjusted)))

    async def score(self, candidate: Candidate) -> SubScore:
        try:
            ctx = await build_insider_context(
                candidate,
                self._uw,
                self._token,
                self._finnhub,
                self._cfg,
            )
        except Exception as e:
            log.error("insider.context_failed", ticker=candidate.ticker, error=str(e))
            return SubScore(
                agent=self.name,
                score=50,
                rationale=f"Context build failed: {e}",
                signals=[],
                skipped=True,
                skip_reason=str(e),
            )

        should_skip, _reason = should_skip_insider_analysis(ctx)
        if should_skip:
            return make_skip_score()

        user_prompt = build_insider_tracker_user_prompt(ctx)
        try:
            raw_response = await self._llm.complete(
                INSIDER_TRACKER_SYSTEM_PROMPT,
                user_prompt,
                max_tokens=300,
            )
            grade = parse_llm_response(raw_response.text, GradeResponse)
        except (ParseError, Exception) as e:
            log.warning("insider.llm_failed", ticker=candidate.ticker, error=str(e))
            return SubScore(
                agent=self.name,
                score=50,
                rationale=f"Insider analysis failed: {e}",
                signals=[],
                skipped=True,
                skip_reason=str(e),
            )

        adjusted_score = self._apply_confidence_adjustment(grade.score, ctx)

        return SubScore(
            agent=self.name,
            score=adjusted_score,
            rationale=grade.rationale,
            signals=grade.signals_confirmed,
            skipped=False,
            skip_reason=None,
        )
