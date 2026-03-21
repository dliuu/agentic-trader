"""Unusual Whales API client.

All endpoints are GET. Auth is via Bearer token.
We ONLY use endpoints from the validated whitelist in the
UW skill.md — never guess or fabricate endpoint paths.
"""
import httpx
import structlog

from scanner.client.rate_limiter import RateLimiter
from scanner.models.flow_alert import FlowAlert
from scanner.models.dark_pool import DarkPoolPrint
from scanner.models.market_tide import MarketTide

logger = structlog.get_logger()


class UWClient:
    BASE_URL = "https://api.unusualwhales.com"

    FLOW_ALERTS = "/api/option-trades/flow-alerts"
    DARK_POOL_RECENT = "/api/darkpool/recent"
    DARK_POOL_TICKER = "/api/darkpool/{ticker}"
    MARKET_TIDE = "/api/market/market-tide"
    OPTIONS_SCREENER = "/api/screener/option-contracts"

    def __init__(self, api_token: str, rate_limiter: RateLimiter):
        self._token = api_token
        self._limiter = rate_limiter
        self._client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={
                "Authorization": f"Bearer {api_token}",
                "UW-CLIENT-API-ID": "100001",
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(10.0, connect=5.0),
        )

    async def get_flow_alerts(
        self,
        is_otm: bool = True,
        min_premium: int = 25000,
        size_greater_oi: bool = True,
        limit: int = 50,
        ticker: str | None = None,
    ) -> list[FlowAlert]:
        """Fetch unusual options flow alerts."""
        await self._limiter.acquire()
        params = {
            "is_otm": str(is_otm).lower(),
            "min_premium": min_premium,
            "size_greater_oi": str(size_greater_oi).lower(),
            "limit": limit,
        }
        if ticker:
            params["ticker_symbol"] = ticker

        resp = await self._client.get(self.FLOW_ALERTS, params=params)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            data = data.get("data", [])
        if not isinstance(data, list):
            data = []

        alerts = []
        for item in data:
            try:
                alerts.append(FlowAlert.model_validate(item))
            except Exception as e:
                logger.warning("parse_error", item_id=item.get("id"), error=str(e))
        return alerts

    async def get_dark_pool_recent(self) -> list[DarkPoolPrint]:
        """Fetch recent market-wide dark pool prints."""
        await self._limiter.acquire()
        resp = await self._client.get(self.DARK_POOL_RECENT)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            data = data.get("data", [])
        if not isinstance(data, list):
            return []
        result = []
        for item in data:
            try:
                # Model accepts ticker, premium, executed_at (and aliases) directly
                result.append(DarkPoolPrint.model_validate(item))
            except Exception as e:
                logger.warning("dark_pool_parse_error", item=item, error=str(e))
        return result

    async def get_dark_pool_ticker(self, ticker: str) -> list[DarkPoolPrint]:
        """Fetch dark pool prints for a specific ticker."""
        await self._limiter.acquire()
        path = self.DARK_POOL_TICKER.format(ticker=ticker)
        resp = await self._client.get(path)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            data = data.get("data", [])
        if not isinstance(data, list):
            return []
        return [DarkPoolPrint.model_validate(item) for item in data]

    async def get_market_tide(self) -> MarketTide:
        """Fetch current market sentiment (net call/put premium)."""
        await self._limiter.acquire()
        resp = await self._client.get(self.MARKET_TIDE)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            data = data.get("data", data)
        return MarketTide.from_raw(data)

    async def close(self):
        await self._client.aclose()
