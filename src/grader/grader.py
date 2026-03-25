"""Agent B — LLM-powered trade grading orchestrator."""

import json
import uuid
from datetime import datetime, timezone

import structlog

from shared.db import get_db
from shared.models import Candidate

from grader.context_builder import ContextBuilder
from grader.llm_client import LLMClient, LLMResponse
from grader.models import GradingContext, GradeResponse, ScoredTrade
from grader.parser import ParseError, parse_grade_response
from grader.parser import RETRY_PROMPT
from grader.prompt import build_system_prompt, build_user_prompt

log = structlog.get_logger()


class Grader:
    """Agent B — LLM-powered trade grading."""

    def __init__(
        self,
        context_builder: ContextBuilder,
        llm_client: LLMClient,
        score_threshold: int = 70,
        max_parse_retries: int = 1,
    ):
        self._ctx_builder = context_builder
        self._llm = llm_client
        self._threshold = score_threshold
        self._max_retries = max_parse_retries

    async def grade(self, candidate: Candidate) -> ScoredTrade | None:
        """
        Grade a candidate. Returns ScoredTrade if score >= threshold,
        None otherwise. Every call is logged to SQLite regardless of outcome.
        """
        log.info("grading_start", ticker=candidate.ticker, id=candidate.id)

        # Step 1: Build context
        context = await self._ctx_builder.build(candidate)

        # Step 2: Render prompt
        system = build_system_prompt()
        user = build_user_prompt(context)

        # Step 3: Call LLM (with parse retry)
        grade_response, llm_response = await self._call_and_parse(system, user)

        # Step 4: Log to SQLite
        await self._log_grade(candidate, grade_response, llm_response)

        # Step 5: Route based on score
        if grade_response.score >= self._threshold:
            log.info(
                "grade_pass",
                ticker=candidate.ticker,
                score=grade_response.score,
                verdict=grade_response.verdict,
            )
            return ScoredTrade(
                candidate=candidate,
                grade=grade_response,
                graded_at=datetime.now(timezone.utc),
                model_used=llm_response.model,
                latency_ms=llm_response.latency_ms,
                input_tokens=llm_response.input_tokens,
                output_tokens=llm_response.output_tokens,
            )
        else:
            log.info(
                "grade_fail",
                ticker=candidate.ticker,
                score=grade_response.score,
                rationale=grade_response.rationale[:100],
            )
            return None

    async def _call_and_parse(
        self, system: str, user: str
    ) -> tuple[GradeResponse, LLMResponse]:
        """Call the LLM and parse the response. Retry once on parse failure."""

        llm_response = await self._llm.complete(system, user)

        try:
            grade = parse_grade_response(llm_response.text)
            return grade, llm_response
        except ParseError as e:
            log.warning("parse_failed_retrying", attempt=1)
            first_error = str(e)

        # Retry: ask the LLM to fix its output
        retry_prompt = RETRY_PROMPT.format(error=first_error)
        llm_response = await self._llm.complete(system, retry_prompt)

        try:
            grade = parse_grade_response(llm_response.text)
            return grade, llm_response
        except ParseError as e:
            log.error("parse_failed_permanently", error=str(e))
            # Return a failing grade so the pipeline doesn't stall (model_construct bypasses score validator)
            fallback = GradeResponse.model_construct(
                score=0,
                verdict="fail",
                rationale=f"LLM response could not be parsed: {e}",
                signals_confirmed=[],
                likely_directional=False,
            )
            return fallback, llm_response

    async def _log_grade(
        self,
        candidate: Candidate,
        grade: GradeResponse,
        llm_resp: LLMResponse,
    ) -> None:
        """Write every grading decision to SQLite."""
        db = await get_db()
        try:
            await db.execute(
                """INSERT INTO grades
                   (id, candidate_id, score, verdict, rationale,
                    signals_confirmed, model, input_tokens,
                    output_tokens, latency_ms, graded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(uuid.uuid4()),
                    candidate.id,
                    grade.score,
                    grade.verdict,
                    grade.rationale,
                    json.dumps(grade.signals_confirmed),
                    llm_resp.model,
                    llm_resp.input_tokens,
                    llm_resp.output_tokens,
                    llm_resp.latency_ms,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            await db.commit()
        finally:
            await db.close()
