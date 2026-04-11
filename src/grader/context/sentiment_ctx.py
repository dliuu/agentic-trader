"""Sentiment context builder for Gate 3 sentiment analyst."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone

import httpx
import structlog

from grader.models import NewsBuzz, NewsHeadline, RedditPresence, RedditSummary, SentimentContext
from shared.uw_http import uw_get
from shared.uw_runtime import get_uw_limiter
from shared.filters import SentimentConfig
from shared.models import Candidate

log = structlog.get_logger()


class SentimentContextBuilder:
    """Build SentimentContext from UW headlines, Finnhub buzz, and Reddit presence."""

    def __init__(
        self,
        uw_client: httpx.AsyncClient,
        uw_api_token: str,
        finnhub_api_key: str,
        config: SentimentConfig | None = None,
        reddit_client: httpx.AsyncClient | None = None,
        finnhub_client: httpx.AsyncClient | None = None,
    ):
        self._uw = uw_client
        self._uw_headers = {
            "Authorization": f"Bearer {uw_api_token}",
            "UW-CLIENT-API-ID": "100001",
            "Accept": "application/json",
        }
        self._finnhub_key = finnhub_api_key
        self._cfg = config or SentimentConfig()
        self._reddit_client = reddit_client or httpx.AsyncClient(
            headers={"User-Agent": "whale-scanner/1.0 (sentiment-context)"},
            timeout=10.0,
        )
        self._finnhub_client = finnhub_client or httpx.AsyncClient(timeout=10.0)

    async def build(self, candidate: Candidate) -> SentimentContext:
        ticker = candidate.ticker.upper()
        option_type = "call" if candidate.direction == "bullish" else "put"
        direction = candidate.direction

        headlines_task = self._fetch_uw_headlines(ticker)
        buzz_task = self._fetch_finnhub_buzz(ticker)
        headlines, buzz = await asyncio.gather(headlines_task, buzz_task, return_exceptions=True)

        if isinstance(headlines, Exception):
            log.warning("sentiment.uw_headlines_failed", ticker=ticker, error=str(headlines))
            headlines = []
        if isinstance(buzz, Exception):
            log.warning("sentiment.finnhub_buzz_failed", ticker=ticker, error=str(buzz))
            buzz = NewsBuzz()

        reddit = await self._scan_reddit(ticker)

        now = datetime.now(timezone.utc)
        headline_count_48h = len(
            [h for h in headlines if h.published_at >= now - timedelta(hours=48)]
        )
        has_catalyst = headline_count_48h >= self._cfg.catalyst_headline_min

        news_aligns: bool | None = None
        if buzz.bullish_pct > 0 or buzz.bearish_pct > 0:
            if direction == "bullish" and buzz.bullish_pct > buzz.bearish_pct:
                news_aligns = True
            elif direction == "bearish" and buzz.bearish_pct > buzz.bullish_pct:
                news_aligns = True
            else:
                news_aligns = False

        is_quiet = headline_count_48h == 0 and reddit.total_post_count == 0

        return SentimentContext(
            ticker=ticker,
            option_type=option_type,
            trade_direction=direction,
            headline_count_48h=headline_count_48h,
            headlines=headlines[:5],
            buzz=buzz,
            reddit=reddit,
            has_catalyst=has_catalyst,
            is_quiet=is_quiet,
            news_aligns_with_direction=news_aligns,
        )

    async def _fetch_uw_headlines(self, ticker: str) -> list[NewsHeadline]:
        resp = await uw_get(
            self._uw,
            "https://api.unusualwhales.com/api/news/headlines",
            limiter=get_uw_limiter(),
            headers=self._uw_headers,
            params={"ticker": ticker, "limit": 20},
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])

        parsed: list[NewsHeadline] = []
        for item in data[:20]:
            published_raw = (
                item.get("published_at") or item.get("created_at") or datetime.utcnow().isoformat()
            )
            published_at = datetime.fromisoformat(str(published_raw).replace("Z", "+00:00"))
            if published_at.tzinfo is None:
                published_at = published_at.replace(tzinfo=timezone.utc)
            parsed.append(
                NewsHeadline(
                    title=item.get("headline") or item.get("title") or "",
                    source=item.get("source", "unknown"),
                    published_at=published_at,
                    tickers=item.get("tickers", []),
                )
            )
        return parsed

    async def _fetch_finnhub_buzz(self, ticker: str) -> NewsBuzz:
        if not self._finnhub_key:
            return NewsBuzz()

        resp = await self._finnhub_client.get(
            "https://finnhub.io/api/v1/news-sentiment",
            params={"symbol": ticker, "token": self._finnhub_key},
        )
        resp.raise_for_status()
        data = resp.json()
        buzz_data = data.get("buzz", {})
        sentiment_data = data.get("sentiment", {})
        articles = int(buzz_data.get("articlesInLastWeek", 0) or 0)
        avg = float(buzz_data.get("weeklyAverage", 0.0) or 0.0)
        return NewsBuzz(
            articles_last_week=articles,
            weekly_average=avg,
            buzz_ratio=(articles / avg) if avg > 0 else 0.0,
            bullish_pct=float(sentiment_data.get("bullishPercent", 0.0) or 0.0),
            bearish_pct=float(sentiment_data.get("bearishPercent", 0.0) or 0.0),
            news_score=float(data.get("companyNewsScore", 0.0) or 0.0),
        )

    async def _scan_reddit(self, ticker: str) -> RedditSummary:
        presences: list[RedditPresence] = []
        for subreddit in self._cfg.reddit_subs:
            try:
                presence = await self._search_subreddit(subreddit, ticker)
                presences.append(presence)
            except Exception as exc:
                log.warning(
                    "sentiment.reddit_scan_failed",
                    subreddit=subreddit,
                    ticker=ticker,
                    error=str(exc),
                )
                presences.append(RedditPresence(subreddit=subreddit, post_count=0))
            await asyncio.sleep(self._cfg.reddit_delay_seconds)

        total_posts = sum(p.post_count for p in presences)
        subs_with_mentions = sum(1 for p in presences if p.post_count > 0)
        meme_mentions = any(
            p.post_count > 0 and p.subreddit in self._cfg.meme_subs for p in presences
        )

        return RedditSummary(
            total_subreddits_with_mentions=subs_with_mentions,
            total_post_count=total_posts,
            subreddits=presences,
            is_meme_candidate=meme_mentions,
            is_crowded=subs_with_mentions >= self._cfg.crowded_sub_threshold,
        )

    async def _search_subreddit(self, subreddit: str, ticker: str) -> RedditPresence:
        resp = await self._reddit_client.get(
            f"https://www.reddit.com/r/{subreddit}/search.json",
            params={
                "q": ticker,
                "restrict_sr": "1",
                "sort": "new",
                "t": self._cfg.reddit_search_period,
                "limit": str(self._cfg.reddit_search_limit),
            },
        )
        resp.raise_for_status()
        children = resp.json().get("data", {}).get("children", [])
        matching = [c for c in children if self._ticker_in_post(ticker, c.get("data", {}))]
        top_post = matching[0].get("data", {}) if matching else {}

        return RedditPresence(
            subreddit=subreddit,
            post_count=len(matching),
            top_post_title=top_post.get("title"),
            top_post_score=int(top_post.get("score", 0) or 0),
        )

    @staticmethod
    def _ticker_in_post(ticker: str, post_data: dict) -> bool:
        text = f"{post_data.get('title', '')} {post_data.get('selftext', '')}"
        pattern = rf"(?<!\w)\$?{re.escape(ticker)}(?!\w)"
        return bool(re.search(pattern, text, re.IGNORECASE))
