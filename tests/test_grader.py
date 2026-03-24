"""Integration tests for grader orchestrator."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

from grader.context_builder import ContextBuilder
from grader.grader import Grader
from grader.llm_client import LLMResponse
from grader.main import run_grader
from grader.parser import parse_grade_response
from shared.db import get_db
from shared.models import Candidate, SignalMatch

# Valid GradeResponse JSON fixtures
VALID_HIGH = '{"score": 85, "verdict": "pass", "rationale": "Strong signal.", "signals_confirmed": ["otm"], "likely_directional": true}'
VALID_LOW = '{"score": 45, "verdict": "fail", "rationale": "Low conviction.", "signals_confirmed": [], "likely_directional": false}'


@pytest.fixture
def sample_candidate() -> Candidate:
    return Candidate(
        id="cand-grader-1",
        source="flow_alert",
        ticker="ACME",
        direction="bullish",
        strike=180.0,
        expiry="2026-04-03",
        premium_usd=75_000.0,
        underlying_price=140.0,
        implied_volatility=None,
        execution_type="Sweep",
        dte=14,
        signals=[SignalMatch(rule_name="otm", weight=1.0, detail="OTM 28.6%")],
        confluence_score=1.0,
        dark_pool_confirmation=False,
        market_tide_aligned=False,
        raw_alert_id="raw-1",
        scanned_at=datetime.now(timezone.utc),
    )


def _register_uw_routes() -> None:
    """Mock all context builder API endpoints."""
    respx.get("https://api.unusualwhales.com/api/stock/ACME/quote").mock(
        return_value=httpx.Response(
            200,
            json={
                "price": 150.25,
                "volume": 1234567,
                "avg_volume": 1000000,
                "sector": "Technology",
                "market_cap": 12_500_000_000,
            },
        )
    )
    respx.get("https://api.unusualwhales.com/api/stock/ACME/option-contracts").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    respx.get("https://api.unusualwhales.com/api/news/headlines").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    respx.get("https://api.unusualwhales.com/api/insider/trades").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    respx.get("https://api.unusualwhales.com/api/congressional-trading").mock(
        return_value=httpx.Response(200, json={"data": []})
    )


class FakeLLMClient:
    """LLM client that returns canned responses in sequence."""

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self._call_count = 0

    async def complete(self, system: str, user: str) -> LLMResponse:
        idx = min(self._call_count, len(self._responses) - 1)
        text = self._responses[idx]
        self._call_count += 1
        return LLMResponse(
            text=text,
            input_tokens=100,
            output_tokens=50,
            latency_ms=100,
            model="claude-sonnet-4-20250514",
        )


@pytest.fixture
def temp_grade_db(tmp_path, monkeypatch):
    """Use a temp database for grade logging tests."""
    monkeypatch.setattr("shared.db.DB_PATH", tmp_path / "grades.db")


@respx.mock
@pytest.mark.asyncio
async def test_grade_above_threshold_returns_scored_trade(
    sample_candidate, temp_grade_db
):
    _register_uw_routes()
    async with httpx.AsyncClient() as http_client:
        ctx_builder = ContextBuilder(http_client, api_token="fake")
        llm = FakeLLMClient([VALID_HIGH])
        grader = Grader(
            context_builder=ctx_builder,
            llm_client=llm,
            score_threshold=70,
        )
        result = await grader.grade(sample_candidate)

    assert result is not None
    assert result.grade.score >= 70
    assert result.grade.verdict == "pass"
    assert result.candidate.ticker == "ACME"


@respx.mock
@pytest.mark.asyncio
async def test_grade_below_threshold_returns_none(sample_candidate, temp_grade_db):
    _register_uw_routes()
    async with httpx.AsyncClient() as http_client:
        ctx_builder = ContextBuilder(http_client, api_token="fake")
        llm = FakeLLMClient([VALID_LOW])
        grader = Grader(
            context_builder=ctx_builder,
            llm_client=llm,
            score_threshold=70,
        )
        result = await grader.grade(sample_candidate)

    assert result is None


@respx.mock
@pytest.mark.asyncio
async def test_parse_failure_retries_once(sample_candidate, temp_grade_db):
    _register_uw_routes()
    async with httpx.AsyncClient() as http_client:
        ctx_builder = ContextBuilder(http_client, api_token="fake")
        llm = FakeLLMClient(["I think this trade looks good!", VALID_HIGH])
        grader = Grader(context_builder=ctx_builder, llm_client=llm)
        result = await grader.grade(sample_candidate)

    assert result is not None
    assert result.grade.score == 85


@respx.mock
@pytest.mark.asyncio
async def test_permanent_parse_failure_returns_none(sample_candidate, temp_grade_db):
    _register_uw_routes()
    async with httpx.AsyncClient() as http_client:
        ctx_builder = ContextBuilder(http_client, api_token="fake")
        llm = FakeLLMClient(["garbage", "more garbage"])
        grader = Grader(
            context_builder=ctx_builder,
            llm_client=llm,
            score_threshold=70,
        )
        result = await grader.grade(sample_candidate)

    assert result is None


@respx.mock
@pytest.mark.asyncio
async def test_every_grade_is_logged(sample_candidate, temp_grade_db):
    _register_uw_routes()
    async with httpx.AsyncClient() as http_client:
        ctx_builder = ContextBuilder(http_client, api_token="fake")
        llm = FakeLLMClient([VALID_HIGH])
        grader = Grader(context_builder=ctx_builder, llm_client=llm)
        await grader.grade(sample_candidate)

    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM grades")
        rows = await cursor.fetchall()
        assert len(rows) == 1
        # columns: id, candidate_id, score, verdict, rationale, ...
        assert rows[0][1] == "cand-grader-1"  # candidate_id
        assert rows[0][2] == 85  # score
        assert rows[0][3] == "pass"  # verdict
    finally:
        await db.close()


@respx.mock
@pytest.mark.asyncio
async def test_fail_grade_is_also_logged(sample_candidate, temp_grade_db):
    _register_uw_routes()
    async with httpx.AsyncClient() as http_client:
        ctx_builder = ContextBuilder(http_client, api_token="fake")
        llm = FakeLLMClient([VALID_LOW])
        grader = Grader(context_builder=ctx_builder, llm_client=llm)
        await grader.grade(sample_candidate)

    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM grades")
        rows = await cursor.fetchall()
        assert len(rows) == 1
        assert rows[0][2] == 45  # score
        assert rows[0][3] == "fail"  # verdict
    finally:
        await db.close()


@respx.mock
@pytest.mark.asyncio
async def test_parse_failure_fallback_is_logged(sample_candidate, temp_grade_db):
    _register_uw_routes()
    async with httpx.AsyncClient() as http_client:
        ctx_builder = ContextBuilder(http_client, api_token="fake")
        llm = FakeLLMClient(["garbage", "more garbage"])
        grader = Grader(context_builder=ctx_builder, llm_client=llm)
        await grader.grade(sample_candidate)

    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM grades")
        rows = await cursor.fetchall()
        assert len(rows) == 1
        assert rows[0][2] == 0  # fallback score
        assert rows[0][3] == "fail"  # verdict
    finally:
        await db.close()


@respx.mock
@pytest.mark.asyncio
async def test_enabled_false_pass_through_skips_llm(sample_candidate, temp_grade_db):
    """enabled: false skips LLM calls and puts ScoredTrade with grade=None."""
    _register_uw_routes()
    config = {
        "grader": {
            "score_threshold": 70,
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 512,
            "timeout_seconds": 15,
            "max_parse_retries": 1,
            "enabled": False,
        },
        "uw_api_token": "fake",
        "anthropic_api_key": "fake",
    }

    candidate_queue: asyncio.Queue = asyncio.Queue()
    scored_queue: asyncio.Queue = asyncio.Queue()
    candidate_queue.put_nowait(sample_candidate)
    candidate_queue.put_nowait(None)

    with patch("grader.main.load_config", return_value=config):
        await run_grader(candidate_queue, scored_queue)

    scored = await scored_queue.get()
    assert scored is not None
    assert scored.grade is None
    assert scored.model_used == "pass-through"
    assert scored.candidate.ticker == "ACME"
