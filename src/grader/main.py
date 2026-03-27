"""Grader consumer loop — pull candidates, grade them, push passing trades."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx
import structlog

from grader.agents.insider_tracker import InsiderTracker
from grader.agents.sentiment_analyst import SentimentAnalyst
from grader.context.sentiment_ctx import SentimentContextBuilder
from grader.context_builder import ContextBuilder
from grader.context.sector_cache import get_sector_cache
from grader.gate1 import run_gate1
from grader.gate2 import run_gate2
from grader.gate3 import run_gate3
from grader.grader import Grader
from grader.llm_client import LLMClient
from grader.models import ScoredTrade
from shared.config import load_config
from shared.filters import InsiderScoringConfig
from shared.models import Candidate, SubScore

log = structlog.get_logger()


class _NeutralGate3Agent:
    """Temporary neutral agent placeholder for Gate 3 peers."""

    def __init__(self, name: str):
        self._name = name

    async def score(self, candidate: Candidate) -> SubScore:
        return SubScore(
            agent=self._name,
            score=50,
            rationale=f"{self._name} not implemented yet",
            signals=[],
            skipped=True,
            skip_reason="not_implemented",
        )


async def run_grader(
    candidate_queue: asyncio.Queue[Candidate],
    scored_queue: asyncio.Queue[ScoredTrade],
) -> None:
    """Consumer loop: pull candidates, grade them, push passing trades."""
    config = load_config()
    grader_cfg = config["grader"]

    async with httpx.AsyncClient() as http_client:
        ctx_builder = ContextBuilder(http_client, config["uw_api_token"])
        grader: Grader | None = None
        llm: LLMClient | None = None
        if grader_cfg.get("enabled", True):
            llm = LLMClient(
                api_key=config["anthropic_api_key"],
                model=grader_cfg["model"],
                max_tokens=grader_cfg["max_tokens"],
                timeout=grader_cfg["timeout_seconds"],
            )
            grader = Grader(
                context_builder=ctx_builder,
                llm_client=llm,
                score_threshold=grader_cfg["score_threshold"],
                max_parse_retries=grader_cfg["max_parse_retries"],
            )
            sentiment_ctx = SentimentContextBuilder(
                uw_client=http_client,
                uw_api_token=config["uw_api_token"],
                finnhub_api_key=config.get("finnhub_api_key", ""),
            )
            sentiment_agent = SentimentAnalyst(sentiment_ctx, llm)
            insider_agent = InsiderTracker(
                http_client,
                config["uw_api_token"],
                config.get("finnhub_api_key", ""),
                llm,
                InsiderScoringConfig(),
            )
            sector_agent = _NeutralGate3Agent("sector_analyst")
        else:
            sentiment_agent = None
            insider_agent = None
            sector_agent = None

        while True:
            candidate = await candidate_queue.get()

            if candidate is None:
                break

            passed_gate1, flow_score = await run_gate1(candidate)
            if not passed_gate1:
                candidate_queue.task_done()
                continue

            if not grader_cfg.get("enabled", True):
                # Pass-through mode: skip Gate 2 + LLM grading and forward Gate 1 survivors.
                await scored_queue.put(
                    ScoredTrade(
                        candidate=candidate,
                        grade=None,
                        graded_at=datetime.now(timezone.utc),
                        model_used="pass-through",
                        latency_ms=0,
                        input_tokens=0,
                        output_tokens=0,
                    )
                )
                candidate_queue.task_done()
                continue

            # Gate 2: deterministic volatility + risk in parallel.
            sector_cache = await get_sector_cache(http_client, config["uw_api_token"])
            passed_gate2, _, _ = await run_gate2(
                candidate=candidate,
                flow_score=flow_score,
                client=http_client,
                api_token=config["uw_api_token"],
                sector_cache=sector_cache,
            )
            if not passed_gate2:
                candidate_queue.task_done()
                continue

            if grader is None:
                candidate_queue.task_done()
                continue

            if sentiment_agent is not None and insider_agent is not None and sector_agent is not None:
                gate3_scores = await run_gate3(
                    candidate,
                    sentiment=sentiment_agent,
                    insider=insider_agent,
                    sector=sector_agent,
                )
                log.info(
                    "gate3.result",
                    ticker=candidate.ticker,
                    scores={s.agent: s.score for s in gate3_scores},
                )

            result = await grader.grade(candidate)
            if result is not None:
                await scored_queue.put(result)

            candidate_queue.task_done()

        if llm is not None:
            await llm.close()
