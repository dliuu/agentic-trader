"""Async Finnhub REST client for insider endpoints (cross-reference / MSPR)."""

from __future__ import annotations

import structlog
from datetime import date, timedelta

import httpx

log = structlog.get_logger()

FINNHUB_BASE = "https://finnhub.io/api/v1"


class FinnhubClient:
    """Thin async wrapper around Finnhub stock insider APIs."""

    def __init__(self, http_client: httpx.AsyncClient, api_key: str):
        self._client = http_client
        self._key = api_key or ""

    async def stock_insider_transactions(self, symbol: str) -> list[dict]:
        """GET /stock/insider-transactions — Form 3/4/5 style rows."""
        if not self._key:
            return []
        try:
            resp = await self._client.get(
                f"{FINNHUB_BASE}/stock/insider-transactions",
                params={"symbol": symbol.upper(), "token": self._key},
            )
            if resp.status_code >= 400:
                log.warning(
                    "finnhub.insider_transactions_http",
                    symbol=symbol,
                    status=resp.status_code,
                )
                return []
            data = resp.json()
            if isinstance(data, dict):
                inner = data.get("data")
                if isinstance(inner, list):
                    return inner
            return []
        except Exception as e:
            log.warning("finnhub.insider_transactions_failed", symbol=symbol, error=str(e))
            return []

    async def stock_insider_sentiment(self, symbol: str) -> dict | None:
        """GET /stock/insider-sentiment — MSPR series. Returns None on 401/403 or error."""
        if not self._key:
            return None
        today = date.today()
        from_d = today - timedelta(days=400)
        try:
            resp = await self._client.get(
                f"{FINNHUB_BASE}/stock/insider-sentiment",
                params={
                    "symbol": symbol.upper(),
                    "from": from_d.isoformat(),
                    "to": today.isoformat(),
                    "token": self._key,
                },
            )
            if resp.status_code in (401, 403):
                log.info(
                    "finnhub.insider_sentiment_forbidden",
                    symbol=symbol,
                    status=resp.status_code,
                )
                return None
            if resp.status_code >= 400:
                log.warning(
                    "finnhub.insider_sentiment_http",
                    symbol=symbol,
                    status=resp.status_code,
                )
                return None
            data = resp.json()
            return data if isinstance(data, dict) else None
        except Exception as e:
            log.warning("finnhub.insider_sentiment_failed", symbol=symbol, error=str(e))
            return None
