"""Grader consumer loop — pull candidates, grade them, push passing trades."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import httpx
import structlog

from grader.aggregator import Aggregator
from grader.agents.insider_tracker import InsiderTracker
from grader.agents.sector_analyst import SectorAnalyst
from grader.agents.sentiment_analyst import SentimentAnalyst
from grader.context.sentiment_ctx import SentimentContextBuilder
from grader.gate0 import run_gate0
from grader.gate1 import run_gate1
from grader.gate1_5 import run_gate1_5
from grader.gate2 import run_gate2
from grader.gate3 import run_gate3
from grader.llm_client import LLMClient
from grader.models import ScoredTrade
from grader.synthesis import SynthesisAgent
from grader.context.sector_cache import get_sector_cache
from shared.config import load_config
from shared.filters import InsiderScoringConfig
from shared.models import Candidate
from shared.uw_validation import bootstrap_uw_runtime_from_config, require_uw_api_token

log = structlog.get_logger()


async def run_grader(
    candidate_queue: asyncio.Queue[Candidate],
    scored_queue: asyncio.Queue[ScoredTrade | None],
    *,
    uw_already_bootstrapped: bool = False,
) -> None:
    """Consumer loop: pull candidates, grade them, push passing trades."""
    config_path = Path(__file__).resolve().parent.parent.parent / "config" / "rules.yaml"
    if not config_path.exists():
        config_path = Path("config/rules.yaml")
    config = load_config(config_path)
    scanner_db_path = config["output"]["sqlite_db_path"]
    if not Path(scanner_db_path).is_absolute():
        project_root = config_path.resolve().parent.parent
        scanner_db_path = str(project_root / scanner_db_path)
    grader_cfg = config["grader"]
    uw_cfg = config.get("unusual_whales") or {}
    sector_refresh = float(uw_cfg.get("sector_cache_refresh_seconds", 8 * 3600))
    sector_conc = int(uw_cfg.get("sector_fetch_concurrency", 2))

    if not uw_already_bootstrapped and grader_cfg.get("enabled", True):
        await bootstrap_uw_runtime_from_config(config)
    elif grader_cfg.get("enabled", True):
        require_uw_api_token()

    async with httpx.AsyncClient() as http_client:
        llm: LLMClient | None = None
        sentiment_agent = None
        insider_agent = None
        sector_agent = None
        synthesis_agent = None
        aggregator = None
        if grader_cfg.get("enabled", True):
            llm = LLMClient(
                api_key=config["anthropic_api_key"],
                model=grader_cfg["model"],
                max_tokens=grader_cfg["max_tokens"],
                timeout=grader_cfg["timeout_seconds"],
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
            sector_agent = SectorAnalyst(http_client, config["uw_api_token"])
            synthesis_agent = SynthesisAgent(
                llm,
                max_retries=grader_cfg["max_parse_retries"],
                max_tokens=grader_cfg["max_tokens"],
            )
            aggregator = Aggregator()

        while True:
            candidate = await candidate_queue.get()

            if candidate is None:
                break

            gate0_result = await run_gate0(candidate, http_client, config["uw_api_token"])
            if not gate0_result.passed:
                log.info(
                    "pipeline.gate0_reject",
                    ticker=candidate.ticker,
                    reason=gate0_result.reason.value if gate0_result.reason else "unknown",
                )
                candidate_queue.task_done()
                continue

            passed_gate1, flow_score = await run_gate1(candidate)
            if not passed_gate1:
                candidate_queue.task_done()
                continue

            gate1_5_result = await run_gate1_5(
                candidate=candidate,
                flow_score=flow_score,
                client=http_client,
                api_token=config["uw_api_token"],
                scanner_db_path=scanner_db_path,
                sector=gate0_result.sector,
            )
            if not gate1_5_result.passed:
                log.info(
                    "pipeline.gate1_5_reject",
                    ticker=candidate.ticker,
                    flow_score=flow_score.score,
                    penalty=gate1_5_result.penalty,
                    combined=gate1_5_result.combined_score,
                    reasons=gate1_5_result.reasons,
                )
                candidate_queue.task_done()
                continue

            if not grader_cfg.get("enabled", True):
                # Pass-through mode: skip Gate 2 + LLM grading and forward Gate 1 survivors.
                await scored_queue.put(
                    ScoredTrade(
                        candidate=candidate,
                        grade=None,
                        risk=None,
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
            sector_cache = await get_sector_cache(
                http_client,
                config["uw_api_token"],
                min_refresh_interval_seconds=sector_refresh,
                max_fetch_concurrency=sector_conc,
            )
            passed_gate2, vol_score, risk_score = await run_gate2(
                candidate=candidate,
                flow_score=flow_score,
                client=http_client,
                api_token=config["uw_api_token"],
                sector_cache=sector_cache,
            )
            if not passed_gate2:
                candidate_queue.task_done()
                continue

            if (
                sentiment_agent is None
                or insider_agent is None
                or sector_agent is None
                or synthesis_agent is None
                or aggregator is None
            ):
                candidate_queue.task_done()
                continue

            scored_trade = await run_gate3(
                candidate=candidate,
                flow_score=flow_score,
                vol_score=vol_score,
                risk_score=risk_score,
                sentiment=sentiment_agent,
                insider=insider_agent,
                sector=sector_agent,
                synthesis_agent=synthesis_agent,
                aggregator=aggregator,
                final_threshold=grader_cfg["score_threshold"],
            )
            if scored_trade is not None:
                await scored_queue.put(scored_trade)

            candidate_queue.task_done()

        # Signal to intake that grading is done
        await scored_queue.put(None)

        if llm is not None:
            await llm.close()
