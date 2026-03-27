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

import structlog

from grader.agents.risk_analyst import (
    extract_days_to_earnings,
    extract_option_chain_data,
    extract_realized_vol,
)
from shared.models import FlowCandidate

logger = structlog.get_logger()


async def fetch_risk_context(
    candidate: FlowCandidate,
    api_client: Any,
) -> dict[str, Any]:
    """Fetch all data needed by the risk analyst in parallel."""
    ticker = candidate.ticker

    async def _fetch_option_chains() -> dict | None:
        try:
            return await api_client.get(f"/api/stock/{ticker}/option-chains")
        except Exception as e:  # pragma: no cover - logging branch
            logger.warning("risk_ctx.option_chains_failed", ticker=ticker, error=str(e))
            return None

    async def _fetch_vol_stats() -> dict | None:
        try:
            return await api_client.get(f"/api/stock/{ticker}/volatility/stats")
        except Exception as e:  # pragma: no cover - logging branch
            logger.warning("risk_ctx.vol_stats_failed", ticker=ticker, error=str(e))
            return None

    async def _fetch_earnings() -> dict | None:
        try:
            return await api_client.get(f"/api/earnings/{ticker}")
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
