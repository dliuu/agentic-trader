"""Build enriched grading context for a scanner candidate."""

from __future__ import annotations

import asyncio
from datetime import datetime

import httpx
import structlog

from shared.models import Candidate
from shared.uw_http import uw_get
from shared.uw_runtime import get_uw_limiter

from grader.models import GradingContext, Greeks, InsiderTrade, NewsItem

log = structlog.get_logger()

INDEX_TICKERS = {"SPX", "SPXW", "NDX", "RUT", "VIX", "DJX"}


class ContextBuilder:
    """Enriches a Candidate with market data, news, and insider activity."""

    def __init__(self, uw_client: httpx.AsyncClient, api_token: str):
        self._client = uw_client
        # Match scanner's auth headers (client_id is required by many UW endpoints).
        self._headers = {
            "Authorization": f"Bearer {api_token}",
            "UW-CLIENT-API-ID": "100001",
            "Accept": "application/json",
        }

    async def build(self, candidate: Candidate) -> GradingContext:
        """Gather all context in parallel, returning a complete GradingContext."""
        ticker = candidate.ticker
        is_index = ticker in INDEX_TICKERS

        results = await asyncio.gather(
            self._fetch_greeks_screener(ticker, candidate.strike, candidate.expiry),
            self._fetch_news(ticker) if not is_index else self._empty_list(),
            self._fetch_insider_trades(ticker) if not is_index else self._empty_list(),
            self._fetch_congressional_trades(ticker) if not is_index else self._empty_list(),
            return_exceptions=True,
        )
        greeks, news, insider, congress = results

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

        # Quote endpoints are not on the validated whitelist and 404 for index tickers.
        # Fall back to scanner-provided underlying_price.
        current_spot = float(candidate.underlying_price or candidate.strike)
        daily_volume = 0
        avg_daily_volume = None
        sector = None
        market_cap = None

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

    async def _fetch_greeks_screener(self, ticker: str, strike: float, expiry: str) -> Greeks:
        """Fetch greeks via the validated options screener endpoint."""
        resp = await uw_get(
            self._client,
            "https://api.unusualwhales.com/api/screener/option-contracts",
            limiter=get_uw_limiter(),
            headers=self._headers,
            params={"ticker": ticker, "strike": strike, "expiry": expiry, "limit": 1},
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
        resp = await uw_get(
            self._client,
            "https://api.unusualwhales.com/api/news/headlines",
            limiter=get_uw_limiter(),
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
                    headline=item.get("headline") or item.get("title") or "",
                    source=item.get("source", "unknown"),
                    published_at=self._parse_dt(
                        item.get("published_at")
                        or item.get("created_at")
                        or item.get("timestamp")
                        or ""
                    ),
                )
            )
        return parsed

    async def _fetch_insider_trades(self, ticker: str) -> list[InsiderTrade]:
        """GET /api/insider/trades?ticker={ticker}"""
        resp = await uw_get(
            self._client,
            "https://api.unusualwhales.com/api/insider/trades",
            limiter=get_uw_limiter(),
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
                    name=item.get("insider_name") or item.get("name") or "",
                    title=item.get("insider_title") or item.get("title"),
                    trade_type=item.get("transaction_type") or item.get("trade_type") or "unknown",
                    shares=int(item.get("shares", 0)),
                    value=float(item.get("value", 0)),
                    filed_at=self._parse_dt(
                        item.get("filed_at") or item.get("created_at") or item.get("timestamp") or ""
                    ),
                )
            )
        return trades

    async def _fetch_congressional_trades(self, ticker: str) -> list[InsiderTrade]:
        """GET /api/congressional-trading?ticker={ticker}"""
        resp = await uw_get(
            self._client,
            "https://api.unusualwhales.com/api/congressional-trading",
            limiter=get_uw_limiter(),
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
                    name=item.get("politician_name") or item.get("name") or "",
                    title=item.get("chamber") or item.get("title"),
                    trade_type=item.get("transaction_type") or item.get("trade_type") or "unknown",
                    shares=int(item.get("shares", 0)),
                    value=float(item.get("value", 0)),
                    filed_at=self._parse_dt(
                        item.get("filed_at") or item.get("created_at") or item.get("timestamp") or ""
                    ),
                )
            )
        return trades

    @staticmethod
    def _parse_dt(value: str) -> datetime:
        if not value:
            return datetime.utcnow()
        if value.endswith("Z"):
            value = value.replace("Z", "+00:00")
        return datetime.fromisoformat(value)

    @staticmethod
    async def _empty_list() -> list:
        return []
