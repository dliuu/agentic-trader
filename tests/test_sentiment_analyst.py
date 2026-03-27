"""Tests for sentiment analyst agent and prompt flow."""

from __future__ import annotations

from datetime import datetime

import pytest

from grader.agents.sentiment_analyst import SentimentAnalyst
from grader.llm_client import LLMResponse
from grader.models import NewsBuzz, RedditSummary, SentimentContext
from grader.prompt import build_sentiment_prompt
from shared.models import Candidate, SignalMatch


@pytest.fixture
def sample_candidate() -> Candidate:
    return Candidate(
        id="cand-sent-agent-1",
        source="flow_alert",
        ticker="MEME",
        direction="bullish",
        strike=50.0,
        expiry="2026-04-30",
        premium_usd=40_000.0,
        underlying_price=40.0,
        implied_volatility=0.9,
        execution_type="Sweep",
        dte=12,
        signals=[SignalMatch(rule_name="otm", weight=1.0, detail="OTM")],
        confluence_score=1.0,
        dark_pool_confirmation=False,
        market_tide_aligned=False,
        raw_alert_id="raw-sent-agent",
    )


class StubContextBuilder:
    def __init__(self, ctx: SentimentContext | Exception):
        self._ctx = ctx

    async def build(self, candidate: Candidate) -> SentimentContext:
        if isinstance(self._ctx, Exception):
            raise self._ctx
        return self._ctx


class StubLLM:
    def __init__(self, text: str | Exception):
        self._text = text

    async def complete(self, system: str, user: str) -> LLMResponse:
        if isinstance(self._text, Exception):
            raise self._text
        return LLMResponse(
            text=self._text,
            input_tokens=50,
            output_tokens=30,
            latency_ms=80,
            model="claude-sonnet-4-20250514",
        )


@pytest.mark.asyncio
async def test_quiet_ticker_scores_neutral(sample_candidate: Candidate):
    ctx = SentimentContext(
        ticker="OBSCR",
        option_type="call",
        trade_direction="bullish",
        headline_count_48h=0,
        reddit=RedditSummary(total_post_count=0),
        is_quiet=True,
    )
    llm_json = (
        '{"score": 50, "verdict": "pass", "rationale": "No crowd attention.", '
        '"signals_confirmed": ["quiet_ticker"], "risk_factors": [], "crowd_exposure": "none"}'
    )
    agent = SentimentAnalyst(StubContextBuilder(ctx), StubLLM(llm_json))
    sub = await agent.score(sample_candidate)
    assert sub.score == 50
    assert "crowd_exposure=none" in sub.signals


@pytest.mark.asyncio
async def test_meme_ticker_scores_low(sample_candidate: Candidate):
    ctx = SentimentContext(
        ticker="MEME",
        option_type="call",
        trade_direction="bullish",
        reddit=RedditSummary(
            total_subreddits_with_mentions=5,
            total_post_count=25,
            is_meme_candidate=True,
            is_crowded=True,
        ),
    )
    llm_json = (
        '{"score": 28, "verdict": "fail", "rationale": "Crowded meme flow.", '
        '"signals_confirmed": ["meme_candidate"], "risk_factors": ["crowded"], '
        '"crowd_exposure": "high"}'
    )
    agent = SentimentAnalyst(StubContextBuilder(ctx), StubLLM(llm_json))
    sub = await agent.score(sample_candidate)
    assert 20 <= sub.score <= 35
    assert "crowd_exposure=high" in sub.signals


@pytest.mark.asyncio
async def test_catalyst_no_crowd_scores_high(sample_candidate: Candidate):
    ctx = SentimentContext(
        ticker="CATA",
        option_type="call",
        trade_direction="bullish",
        headline_count_48h=4,
        buzz=NewsBuzz(articles_last_week=8, weekly_average=2, buzz_ratio=4.0),
        reddit=RedditSummary(total_post_count=0),
        has_catalyst=True,
        is_quiet=False,
        news_aligns_with_direction=True,
    )
    llm_json = (
        '{"score": 78, "verdict": "pass", "rationale": "Catalyst with low crowd chatter.", '
        '"signals_confirmed": ["catalyst_uncrowded"], "risk_factors": [], '
        '"crowd_exposure": "low"}'
    )
    agent = SentimentAnalyst(StubContextBuilder(ctx), StubLLM(llm_json))
    sub = await agent.score(sample_candidate)
    assert 70 <= sub.score <= 85


@pytest.mark.asyncio
async def test_context_failure_returns_neutral_skipped(sample_candidate: Candidate):
    agent = SentimentAnalyst(StubContextBuilder(RuntimeError("boom")), StubLLM("{}"))
    sub = await agent.score(sample_candidate)
    assert sub.score == 50
    assert sub.skipped is True


def test_sentiment_prompt_contains_core_sections():
    ctx = SentimentContext(
        ticker="TEST",
        option_type="call",
        trade_direction="bullish",
        headline_count_48h=1,
        has_catalyst=False,
        is_quiet=False,
        news_aligns_with_direction=None,
    )
    prompt = build_sentiment_prompt(ctx)
    assert "=== NEWS ===" in prompt
    assert "=== BUZZ METRICS (Finnhub) ===" in prompt
    assert "=== REDDIT TRADING SUBS (last 7 days) ===" in prompt
    assert "=== PRE-COMPUTED FLAGS ===" in prompt
