"""Grader consumer loop — pull candidates, grade them, push passing trades."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx

from grader.context_builder import ContextBuilder
from grader.context.sector_cache import get_sector_cache
from grader.gate1 import run_gate1
from grader.gate2 import run_gate2
from grader.grader import Grader
from grader.llm_client import LLMClient
from grader.models import ScoredTrade
from shared.config import load_config
from shared.models import Candidate


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

        while True:
            candidate = await candidate_queue.get()

            if candidate is None:
                break

            passed_gate1, flow_score = await run_gate1(candidate)
            if not passed_gate1:
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

            if not grader_cfg.get("enabled", True):
                # Pass-through mode: skip LLM grading (Gate 1 + Gate 2 already applied)
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

            if grader is None:
                candidate_queue.task_done()
                continue

            result = await grader.grade(candidate)
            if result is not None:
                await scored_queue.put(result)

            candidate_queue.task_done()

        if llm is not None:
            await llm.close()
