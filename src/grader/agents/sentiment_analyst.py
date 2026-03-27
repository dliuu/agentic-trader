"""Sentiment analyst agent (Gate 3, LLM-powered)."""

from __future__ import annotations

import structlog

from grader.context.sentiment_ctx import SentimentContextBuilder
from grader.llm_client import LLMClient
from grader.models import SentimentGrade
from grader.parser import ParseError, parse_llm_response
from grader.prompt import SENTIMENT_ANALYST_SYSTEM, build_sentiment_prompt
from shared.models import Candidate, SubScore

log = structlog.get_logger()


class SentimentAnalyst:
    """LLM-powered analyst that scores crowd/noise exposure around a ticker."""

    name = "sentiment_analyst"

    def __init__(self, context_builder: SentimentContextBuilder, llm_client: LLMClient):
        self._ctx_builder = context_builder
        self._llm = llm_client

    async def score(self, candidate: Candidate) -> SubScore:
        try:
            ctx = await self._ctx_builder.build(candidate)
        except Exception as e:
            log.error("sentiment.context_failed", ticker=candidate.ticker, error=str(e))
            return SubScore(
                agent=self.name,
                score=50,
                rationale=f"Context build failed: {e}",
                signals=[],
                skipped=True,
                skip_reason=str(e),
            )

        user_msg = build_sentiment_prompt(ctx)
        try:
            raw_response = await self._llm.complete(SENTIMENT_ANALYST_SYSTEM, user_msg)
            grade = parse_llm_response(raw_response.text, SentimentGrade)
        except (ParseError, Exception) as e:
            log.warning("sentiment.llm_failed", ticker=candidate.ticker, error=str(e))
            return SubScore(
                agent=self.name,
                score=50,
                rationale=f"Sentiment analysis failed: {e}",
                signals=[],
                skipped=True,
                skip_reason=str(e),
            )

        return SubScore(
            agent=self.name,
            score=grade.score,
            rationale=grade.rationale,
            signals=grade.signals_confirmed + [f"crowd_exposure={grade.crowd_exposure}"],
        )
