"""Build enriched grading context for a scanner candidate."""

from __future__ import annotations

import asyncio
from datetime import datetime

import httpx
import structlog

from shared.models import Candidate

from grader.models import GradingContext, Greeks, InsiderTrade, NewsItem

log = structlog.get_logger()


class ContextBuilder:
    """Enriches a Candidate with market data, news, and insider activity."""

    def __init__(self, uw_client: httpx.AsyncClient, api_token: str):
        self._client = uw_client
        self._headers = {"Authorization": f"Bearer {api_token}"}

    async def build(self, candidate: Candidate) -> GradingContext:
        """Gather all context in parallel, returning a complete GradingContext."""
        results = await asyncio.gather(
            self._fetch_quote(candidate.ticker),
            self._fetch_greeks(candidate.ticker, candidate.strike, candidate.expiry),
            self._fetch_news(candidate.ticker),
            self._fetch_insider_trades(candidate.ticker),
            self._fetch_congressional_trades(candidate.ticker),
            return_exceptions=True,
        )
        quote, greeks, news, insider, congress = results

        if isinstance(quote, Exception):
            log.warning("context_quote_failed", ticker=candidate.ticker, error=str(quote))
            quote = {}
        if isinstance(greeks, Exception):
            log.warning("context_greeks_failed", ticker=candidate.ticker, error=str(greeks))
            greeks = None
        if isinstance(news, Exception):
            log.warning("context_news_failed", ticker=candidate.ticker, error=str(news))
            news = []
        if isinstance(insider, Exception):
            log.warning("context_insider_failed", ticker=candidate.ticker, error=str(insider))
            insider = []
        if isinstance(congress, Exception):
            log.warning("context_congress_failed", ticker=candidate.ticker, error=str(congress))
            congress = []

        current_spot = float(quote.get("price", candidate.underlying_price or candidate.strike))
        daily_volume = int(quote.get("volume", 0))
        avg_daily_volume = quote.get("avg_volume")
        sector = quote.get("sector")
        market_cap = quote.get("market_cap")

        return GradingContext(
            candidate=candidate,
            current_spot=current_spot,
            daily_volume=daily_volume,
            avg_daily_volume=int(avg_daily_volume) if avg_daily_volume is not None else None,
            greeks=greeks,
            recent_news=news,
            insider_trades=insider,
            congressional_trades=congress,
            sector=sector,
            market_cap=float(market_cap) if market_cap is not None else None,
        )

    async def _fetch_quote(self, ticker: str) -> dict:
        """GET /api/stock/{ticker}/quote"""
        resp = await self._client.get(
            f"https://api.unusualwhales.com/api/stock/{ticker}/quote",
            headers=self._headers,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            return data.get("data", data)
        return {}

    async def _fetch_greeks(self, ticker: str, strike: float, expiry: str) -> Greeks:
        """Fetch contract greeks for a ticker/strike/expiry tuple."""
        resp = await self._client.get(
            f"https://api.unusualwhales.com/api/stock/{ticker}/option-contracts",
            headers=self._headers,
            params={"strike": strike, "expiry": expiry},
        )
        resp.raise_for_status()
        data = resp.json()
        contracts = data.get("data", []) if isinstance(data, dict) else []
        contract = contracts[0] if contracts else {}
        return Greeks(
            delta=contract.get("delta"),
            gamma=contract.get("gamma"),
            theta=contract.get("theta"),
            vega=contract.get("vega"),
            iv=contract.get("implied_volatility"),
        )

    async def _fetch_news(self, ticker: str) -> list[NewsItem]:
        """GET /api/news/headlines?ticker={ticker}"""
        resp = await self._client.get(
            "https://api.unusualwhales.com/api/news/headlines",
            headers=self._headers,
            params={"ticker": ticker, "limit": 5},
        )
        resp.raise_for_status()
        payload = resp.json()
        items = payload.get("data", []) if isinstance(payload, dict) else []
        parsed: list[NewsItem] = []
        for item in items[:5]:
            parsed.append(
                NewsItem(
                    headline=item["headline"],
                    source=item.get("source", "unknown"),
                    published_at=self._parse_dt(item["published_at"]),
                )
            )
        return parsed

    async def _fetch_insider_trades(self, ticker: str) -> list[InsiderTrade]:
        """GET /api/insider/trades?ticker={ticker}"""
        resp = await self._client.get(
            "https://api.unusualwhales.com/api/insider/trades",
            headers=self._headers,
            params={"ticker": ticker, "limit": 5},
        )
        resp.raise_for_status()
        payload = resp.json()
        items = payload.get("data", []) if isinstance(payload, dict) else []
        trades: list[InsiderTrade] = []
        for item in items[:5]:
            trades.append(
                InsiderTrade(
                    name=item["insider_name"],
                    title=item.get("insider_title"),
                    trade_type=item["transaction_type"],
                    shares=int(item.get("shares", 0)),
                    value=float(item.get("value", 0)),
                    filed_at=self._parse_dt(item["filed_at"]),
                )
            )
        return trades

    async def _fetch_congressional_trades(self, ticker: str) -> list[InsiderTrade]:
        """GET /api/congressional-trading?ticker={ticker}"""
        resp = await self._client.get(
            "https://api.unusualwhales.com/api/congressional-trading",
            headers=self._headers,
            params={"ticker": ticker, "limit": 5},
        )
        resp.raise_for_status()
        payload = resp.json()
        items = payload.get("data", []) if isinstance(payload, dict) else []
        trades: list[InsiderTrade] = []
        for item in items[:5]:
            trades.append(
                InsiderTrade(
                    name=item["politician_name"],
                    title=item.get("chamber"),
                    trade_type=item["transaction_type"],
                    shares=int(item.get("shares", 0)),
                    value=float(item.get("value", 0)),
                    filed_at=self._parse_dt(item["filed_at"]),
                )
            )
        return trades

    @staticmethod
    def _parse_dt(value: str) -> datetime:
        if value.endswith("Z"):
            value = value.replace("Z", "+00:00")
        return datetime.fromisoformat(value)
