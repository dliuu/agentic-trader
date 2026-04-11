"""
Risk context builder - fetches and extracts data for the risk analyst.

Makes 3 UW API calls per candidate:
  1. /api/stock/{ticker}/option-chains
  2. /api/stock/{ticker}/volatility/stats
  3. /api/earnings/{ticker}
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import structlog

from grader.agents.risk_analyst import (
    extract_days_to_earnings,
    extract_option_chain_data,
    extract_realized_vol,
)
from shared.models import FlowCandidate
from shared.uw_http import uw_get_json
from shared.uw_runtime import get_uw_limiter

logger = structlog.get_logger()

UW_BASE = "https://api.unusualwhales.com"


async def fetch_risk_context(
    candidate: FlowCandidate,
    client: httpx.AsyncClient,
    api_token: str,
) -> dict[str, Any]:
    """Fetch all data needed by the risk analyst in parallel."""
    ticker = candidate.ticker
    headers = {
        "Authorization": f"Bearer {api_token}",
        "UW-CLIENT-API-ID": "100001",
        "Accept": "application/json",
    }
    limiter = get_uw_limiter()

    async def _fetch_option_chains() -> dict | None:
        try:
            return await uw_get_json(
                client,
                f"{UW_BASE}/api/stock/{ticker}/option-chains",
                headers=headers,
                limiter=limiter,
                cache_key=f"uw:option-chains:{ticker}",
            )
        except Exception as e:  # pragma: no cover - logging branch
            logger.warning("risk_ctx.option_chains_failed", ticker=ticker, error=str(e))
            return None

    async def _fetch_vol_stats() -> dict | None:
        try:
            return await uw_get_json(
                client,
                f"{UW_BASE}/api/stock/{ticker}/volatility/stats",
                headers=headers,
                limiter=limiter,
                cache_key=f"uw:vol-stats:{ticker}",
            )
        except Exception as e:  # pragma: no cover - logging branch
            logger.warning("risk_ctx.vol_stats_failed", ticker=ticker, error=str(e))
            return None

    async def _fetch_earnings() -> dict | None:
        try:
            return await uw_get_json(
                client,
                f"{UW_BASE}/api/earnings/{ticker}",
                headers=headers,
                limiter=limiter,
                use_cache=False,
                cache_key=f"uw:earnings:{ticker}",
            )
        except Exception as e:  # pragma: no cover - logging branch
            logger.warning("risk_ctx.earnings_failed", ticker=ticker, error=str(e))
            return None

    chains_raw, vol_raw, earnings_raw = await asyncio.gather(
        _fetch_option_chains(),
        _fetch_vol_stats(),
        _fetch_earnings(),
    )

    option_chain_data = extract_option_chain_data(chains_raw, candidate)
    annualized_realized_vol = extract_realized_vol(vol_raw)
    days_to_earnings = extract_days_to_earnings(earnings_raw)

    logger.info(
        "risk_ctx.fetched",
        ticker=ticker,
        has_spread=option_chain_data.get("spread_pct") is not None,
        has_vol=annualized_realized_vol is not None,
        has_earnings=days_to_earnings is not None,
    )

    return {
        "option_chain_data": option_chain_data,
        "annualized_realized_vol": annualized_realized_vol,
        "days_to_earnings": days_to_earnings,
    }
