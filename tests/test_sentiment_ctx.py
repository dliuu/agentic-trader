"""Tests for sentiment context builder."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from grader.context.sentiment_ctx import SentimentContextBuilder
from shared.filters import SentimentConfig
from shared.models import Candidate, SignalMatch


@pytest.fixture
def sample_candidate() -> Candidate:
    return Candidate(
        id="cand-sent-ctx-1",
        source="flow_alert",
        ticker="AAPL",
        direction="bullish",
        strike=220.0,
        expiry="2026-05-15",
        premium_usd=80_000.0,
        underlying_price=205.0,
        implied_volatility=0.35,
        execution_type="Sweep",
        dte=20,
        signals=[SignalMatch(rule_name="premium", weight=1.0, detail="Large premium")],
        confluence_score=1.0,
        dark_pool_confirmation=False,
        market_tide_aligned=True,
        raw_alert_id="raw-sent-1",
    )


def test_ticker_in_post_exact_match():
    assert SentimentContextBuilder._ticker_in_post("AAPL", {"title": "AAPL calls printing"})
    assert SentimentContextBuilder._ticker_in_post("AAPL", {"title": "$AAPL moon"})
    assert not SentimentContextBuilder._ticker_in_post("AAPL", {"title": "AAPLD is different"})
    assert not SentimentContextBuilder._ticker_in_post("AMD", {"title": "DAMAGE control"})


@respx.mock
@pytest.mark.asyncio
async def test_context_builder_handles_all_failures(sample_candidate: Candidate):
    respx.get("https://api.unusualwhales.com/api/news/headlines").mock(
        return_value=httpx.Response(500, json={"error": "uw down"})
    )
    respx.get("https://finnhub.io/api/v1/news-sentiment").mock(
        return_value=httpx.Response(500, json={"error": "finnhub down"})
    )
    respx.get(
        "https://www.reddit.com/r/wallstreetbets/search.json",
    ).mock(return_value=httpx.Response(500, json={"error": "reddit down"}))
    respx.get("https://www.reddit.com/r/options/search.json").mock(
        return_value=httpx.Response(500, json={"error": "reddit down"})
    )
    respx.get("https://www.reddit.com/r/stocks/search.json").mock(
        return_value=httpx.Response(500, json={"error": "reddit down"})
    )
    respx.get("https://www.reddit.com/r/investing/search.json").mock(
        return_value=httpx.Response(500, json={"error": "reddit down"})
    )
    respx.get("https://www.reddit.com/r/thetagang/search.json").mock(
        return_value=httpx.Response(500, json={"error": "reddit down"})
    )
    respx.get("https://www.reddit.com/r/Shortsqueeze/search.json").mock(
        return_value=httpx.Response(500, json={"error": "reddit down"})
    )
    respx.get("https://www.reddit.com/r/unusual_whales/search.json").mock(
        return_value=httpx.Response(500, json={"error": "reddit down"})
    )

    cfg = SentimentConfig(reddit_delay_seconds=0.0)
    async with httpx.AsyncClient() as uw:
        builder = SentimentContextBuilder(
            uw_client=uw,
            uw_api_token="fake",
            finnhub_api_key="fake",
            config=cfg,
        )
        ctx = await builder.build(sample_candidate)

    assert ctx.ticker == "AAPL"
    assert ctx.headline_count_48h == 0
    assert ctx.buzz.articles_last_week == 0
    assert ctx.reddit.total_post_count == 0
    assert ctx.is_quiet is True


@respx.mock
@pytest.mark.asyncio
async def test_reddit_scan_respects_rate_limit(sample_candidate: Candidate):
    respx.get("https://api.unusualwhales.com/api/news/headlines").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    respx.get("https://finnhub.io/api/v1/news-sentiment").mock(
        return_value=httpx.Response(200, json={"buzz": {}, "sentiment": {}})
    )

    call_times: list[float] = []

    async def reddit_handler(_: httpx.Request) -> httpx.Response:
        call_times.append(asyncio.get_event_loop().time())
        return httpx.Response(200, json={"data": {"children": []}})

    for sub in (
        "wallstreetbets",
        "options",
        "stocks",
        "investing",
        "thetagang",
        "Shortsqueeze",
        "unusual_whales",
    ):
        respx.get(f"https://www.reddit.com/r/{sub}/search.json").mock(side_effect=reddit_handler)

    cfg = SentimentConfig(reddit_delay_seconds=0.01)
    async with httpx.AsyncClient() as uw:
        builder = SentimentContextBuilder(
            uw_client=uw,
            uw_api_token="fake",
            finnhub_api_key="fake",
            config=cfg,
        )
        await builder.build(sample_candidate)

    assert len(call_times) == 7
    deltas = [b - a for a, b in zip(call_times, call_times[1:])]
    assert all(delta >= 0.009 for delta in deltas)


@respx.mock
@pytest.mark.asyncio
async def test_catalyst_no_crowd_context_flags(sample_candidate: Candidate):
    now = datetime.now(timezone.utc)
    headlines = [
        {
            "headline": f"Headline {i}",
            "source": "Reuters",
            "published_at": (now - timedelta(hours=i)).isoformat(),
            "tickers": ["AAPL"],
        }
        for i in range(4)
    ]
    respx.get("https://api.unusualwhales.com/api/news/headlines").mock(
        return_value=httpx.Response(200, json={"data": headlines})
    )
    respx.get("https://finnhub.io/api/v1/news-sentiment").mock(
        return_value=httpx.Response(
            200,
            json={
                "buzz": {"articlesInLastWeek": 8, "weeklyAverage": 2},
                "sentiment": {"bullishPercent": 70, "bearishPercent": 30},
                "companyNewsScore": 0.8,
            },
        )
    )
    for sub in (
        "wallstreetbets",
        "options",
        "stocks",
        "investing",
        "thetagang",
        "Shortsqueeze",
        "unusual_whales",
    ):
        respx.get(f"https://www.reddit.com/r/{sub}/search.json").mock(
            return_value=httpx.Response(200, json={"data": {"children": []}})
        )

    cfg = SentimentConfig(reddit_delay_seconds=0.0)
    async with httpx.AsyncClient() as uw:
        builder = SentimentContextBuilder(
            uw_client=uw,
            uw_api_token="fake",
            finnhub_api_key="fake",
            config=cfg,
        )
        ctx = await builder.build(sample_candidate)

    assert ctx.has_catalyst is True
    assert ctx.reddit.total_post_count == 0
    assert ctx.news_aligns_with_direction is True
