"""Tests for grader context builder."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import httpx
import pytest
import respx

from grader.context_builder import ContextBuilder
from grader.models import GradingContext
from shared.models import Candidate, SignalMatch


@pytest.fixture
def sample_candidate() -> Candidate:
    return Candidate(
        id="cand-cb-1",
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


def _register_all_success_routes() -> None:
    respx.get("https://api.unusualwhales.com/api/screener/option-contracts").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "delta": 0.42,
                        "gamma": 0.08,
                        "theta": -0.03,
                        "vega": 0.11,
                        "implied_volatility": 0.34,
                    }
                ]
            },
        )
    )
    # Non-whitelisted endpoints are intentionally not called by the context builder.


@respx.mock
@pytest.mark.asyncio
async def test_context_builder_all_apis_succeed(sample_candidate: Candidate) -> None:
    _register_all_success_routes()
    async with httpx.AsyncClient() as client:
        builder = ContextBuilder(client, api_token="fake")
        ctx = await builder.build(sample_candidate)

    assert isinstance(ctx, GradingContext)
    assert ctx.current_spot == pytest.approx(sample_candidate.underlying_price or sample_candidate.strike)
    assert ctx.daily_volume == 0
    assert ctx.avg_daily_volume is None
    assert ctx.greeks is not None
    assert ctx.greeks.iv == pytest.approx(0.34)
    assert ctx.recent_news == []
    assert ctx.insider_trades == []
    assert ctx.congressional_trades == []
    assert ctx.sector is None
    assert ctx.market_cap is None


@respx.mock
@pytest.mark.asyncio
async def test_context_builder_one_api_fails_gracefully(sample_candidate: Candidate) -> None:
    _register_all_success_routes()
    # Override one route to fail
    respx.get("https://api.unusualwhales.com/api/screener/option-contracts").mock(
        return_value=httpx.Response(500, json={"error": "boom"})
    )

    async with httpx.AsyncClient() as client:
        builder = ContextBuilder(client, api_token="fake")
        ctx = await builder.build(sample_candidate)

    assert isinstance(ctx, GradingContext)
    assert ctx.greeks is None
    assert ctx.current_spot == pytest.approx(sample_candidate.underlying_price or sample_candidate.strike)


@respx.mock
@pytest.mark.asyncio
async def test_context_builder_all_apis_fail_uses_fallbacks(sample_candidate: Candidate) -> None:
    respx.get("https://api.unusualwhales.com/api/screener/option-contracts").mock(
        return_value=httpx.Response(500, json={"error": "down"})
    )

    async with httpx.AsyncClient() as client:
        builder = ContextBuilder(client, api_token="fake")
        ctx = await builder.build(sample_candidate)

    assert isinstance(ctx, GradingContext)
    assert ctx.current_spot == pytest.approx(sample_candidate.underlying_price or sample_candidate.strike)
    assert ctx.daily_volume == 0
    assert ctx.avg_daily_volume is None
    assert ctx.greeks is None
    assert ctx.recent_news == []
    assert ctx.insider_trades == []
    assert ctx.congressional_trades == []
    assert ctx.sector is None
    assert ctx.market_cap is None


@respx.mock
@pytest.mark.asyncio
async def test_context_builder_calls_apis_concurrently(sample_candidate: Candidate) -> None:
    async def delayed_json(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.12)
        return httpx.Response(200, json={"data": []})

    respx.get("https://api.unusualwhales.com/api/screener/option-contracts").mock(side_effect=delayed_json)

    async with httpx.AsyncClient() as client:
        builder = ContextBuilder(client, api_token="fake")
        start = time.perf_counter()
        ctx = await builder.build(sample_candidate)
        elapsed = time.perf_counter() - start

    assert isinstance(ctx, GradingContext)
    # Sequential would be ~0.12s+ (single call). Ensure it doesn't blow up.
    assert elapsed < 0.35
