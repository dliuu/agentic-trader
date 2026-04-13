"""Context builder for Gate 1.5 explainability filter.

Fetches:
  1. Earnings dates — /api/earnings/{ticker} (cached, shared with risk_ctx)
  2. News headlines — /api/news/headlines?ticker={TICKER} (last 48h)
  3. Sector tide — /api/market/{sector-slug}/sector-tide (conditional, only if sector known)
  4. Scanner DB — raw_alerts count for hot ticker check (local SQLite query)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite
import httpx
import structlog

from grader.agents.risk_analyst import (
    extract_days_to_earnings,
    extract_next_earnings_datetime,
)
from grader.context.sector_ctx import _resolve_sector_slug, parse_sector_tide
from shared.filters import ExplainabilityConfig, EXPLAINABILITY_CONFIG
from shared.models import Candidate
from shared.uw_http import uw_get_json
from shared.uw_runtime import get_uw_limiter
from shared.uw_validation import uw_auth_headers

log = structlog.get_logger()

UW_BASE = "https://api.unusualwhales.com"


@dataclass
class ExplainabilityContext:
    """Everything Gate 1.5 needs to evaluate a candidate."""

    ticker: str

    # Earnings
    days_to_earnings: int | None = None  # None if no upcoming earnings found
    earnings_date: str | None = None

    # Hot ticker
    flow_alert_count_14d: int = 0

    # Sector
    sector: str | None = None
    sector_call_put_ratio: float | None = None

    # Headlines
    headlines_48h: list[dict[str, Any]] = field(default_factory=list)

    # Fetch diagnostics
    fetch_errors: list[str] = field(default_factory=list)


def _parse_headline_time(published_raw: Any) -> datetime | None:
    if published_raw is None:
        return None
    try:
        published_at = datetime.fromisoformat(str(published_raw).replace("Z", "+00:00"))
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=timezone.utc)
        return published_at
    except (ValueError, TypeError):
        return None


async def _fetch_earnings_json(
    client: httpx.AsyncClient,
    ticker: str,
    headers: dict[str, str],
    fetch_errors: list[str],
) -> dict[str, Any] | None:
    limiter = get_uw_limiter()
    try:
        return await uw_get_json(
            client,
            f"{UW_BASE}/api/earnings/{ticker}",
            headers=headers,
            limiter=limiter,
            use_cache=True,
            cache_key=f"uw:earnings:{ticker}",
        )
    except Exception as e:  # pragma: no cover - logging branch
        log.warning("explainability_ctx.earnings_failed", ticker=ticker, error=str(e))
        fetch_errors.append("earnings_failed")
        return None


async def _fetch_headlines_json(
    client: httpx.AsyncClient,
    ticker: str,
    headers: dict[str, str],
    fetch_errors: list[str],
) -> dict[str, Any] | None:
    limiter = get_uw_limiter()
    try:
        return await uw_get_json(
            client,
            f"{UW_BASE}/api/news/headlines",
            headers=headers,
            limiter=limiter,
            use_cache=True,
            cache_key=f"gate15:headlines:{ticker}",
            ttl_seconds=300.0,
            params={"ticker": ticker, "limit": 20},
        )
    except Exception as e:  # pragma: no cover
        log.warning("explainability_ctx.headlines_failed", ticker=ticker, error=str(e))
        fetch_errors.append("headlines_failed")
        return None


async def _fetch_sector_tide_json(
    client: httpx.AsyncClient,
    sector_slug: str,
    headers: dict[str, str],
    fetch_errors: list[str],
) -> dict[str, Any] | None:
    limiter = get_uw_limiter()
    try:
        return await uw_get_json(
            client,
            f"{UW_BASE}/api/market/{sector_slug}/sector-tide",
            headers=headers,
            limiter=limiter,
            use_cache=True,
            cache_key=f"gate15:sector-tide:{sector_slug}",
            ttl_seconds=300.0,
        )
    except Exception as e:  # pragma: no cover
        log.warning(
            "explainability_ctx.sector_tide_failed",
            sector_slug=sector_slug,
            error=str(e),
        )
        fetch_errors.append("sector_tide_failed")
        return None


async def _count_hot_ticker_alerts(
    scanner_db_path: str | None,
    ticker: str,
    lookback_days: int,
) -> int:
    if not scanner_db_path or not Path(scanner_db_path).is_file():
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    try:
        p = Path(scanner_db_path).resolve()
        uri = p.as_uri() + "?mode=ro"
        async with aiosqlite.connect(uri, uri=True) as db:
            cur = await db.execute(
                """
                SELECT COUNT(DISTINCT id) FROM raw_alerts
                WHERE UPPER(COALESCE(json_extract(payload_json, '$.ticker'), '')) = ?
                  AND datetime(received_at) >= datetime(?)
                """,
                (ticker.upper(), cutoff),
            )
            row = await cur.fetchone()
            return int(row[0]) if row and row[0] is not None else 0
    except Exception as e:
        log.warning(
            "explainability_ctx.hot_ticker_query_failed",
            path=scanner_db_path,
            error=str(e),
        )
        return 0


async def build_explainability_context(
    candidate: Candidate,
    client: httpx.AsyncClient,
    api_token: str,
    *,
    scanner_db_path: str | None = None,
    sector: str | None = None,
    config: ExplainabilityConfig | None = None,
) -> ExplainabilityContext:
    """Fetch earnings, headlines, optional sector tide, and hot-ticker count in parallel."""
    cfg = config or EXPLAINABILITY_CONFIG
    ticker = candidate.ticker.upper()
    headers = uw_auth_headers(api_token)
    fetch_errors: list[str] = []

    async def earnings_task() -> dict[str, Any] | None:
        return await _fetch_earnings_json(client, ticker, headers, fetch_errors)

    async def headlines_task() -> dict[str, Any] | None:
        return await _fetch_headlines_json(client, ticker, headers, fetch_errors)

    async def hot_task() -> int:
        return await _count_hot_ticker_alerts(
            scanner_db_path, ticker, cfg.hot_ticker_lookback_days
        )

    sector_slug: str | None = _resolve_sector_slug(sector) if sector else None

    async def sector_task() -> dict[str, Any] | None:
        if not sector_slug:
            return None
        return await _fetch_sector_tide_json(client, sector_slug, headers, fetch_errors)

    earnings_raw, headlines_raw, flow_count, sector_raw = await asyncio.gather(
        earnings_task(),
        headlines_task(),
        hot_task(),
        sector_task(),
    )

    days_to_earnings = extract_days_to_earnings(earnings_raw)
    edt = extract_next_earnings_datetime(earnings_raw)
    earnings_date = edt.date().isoformat() if edt else None

    headlines_48h: list[dict[str, Any]] = []
    if headlines_raw is not None:
        data = headlines_raw.get("data", [])
        if not isinstance(data, list):
            data = []
        now = datetime.now(timezone.utc)
        cutoff_48h = now - timedelta(hours=cfg.catalyst_lookback_hours)
        for item in data[:20]:
            if not isinstance(item, dict):
                continue
            published_raw = item.get("published_at") or item.get("created_at")
            published_at = _parse_headline_time(published_raw)
            if published_at is None or published_at < cutoff_48h:
                continue
            title = item.get("headline") or item.get("title") or ""
            headlines_48h.append(
                {
                    "title": str(title),
                    "source": str(item.get("source", "unknown")),
                    "published_at": published_at.isoformat(),
                }
            )

    sector_call_put_ratio: float | None = None
    if sector_raw is not None:
        tide = parse_sector_tide(sector_raw)
        if tide is not None:
            sector_call_put_ratio = tide.call_put_ratio

    return ExplainabilityContext(
        ticker=ticker,
        days_to_earnings=days_to_earnings,
        earnings_date=earnings_date,
        flow_alert_count_14d=flow_count,
        sector=sector,
        sector_call_put_ratio=sector_call_put_ratio,
        headlines_48h=headlines_48h,
        fetch_errors=list(fetch_errors),
    )
