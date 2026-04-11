"""HTTP helpers for Unusual Whales: rate-limited GET with 429 retry + Retry-After."""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
import structlog

logger = structlog.get_logger()


def parse_retry_after_header(value: str | None) -> float | None:
    """Return seconds to wait from a Retry-After header (seconds or HTTP-date)."""
    if value is None or not str(value).strip():
        return None
    v = str(value).strip()
    try:
        return max(0.0, float(v))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delay = (dt - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, delay)
    except (TypeError, ValueError, OverflowError):
        return None


async def uw_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    limiter: TokenBucketRateLimiter,
    max_retries: int = 5,
    base_backoff_seconds: float = 1.0,
    **kwargs: Any,
) -> httpx.Response:
    """Perform an HTTP request with token-bucket pacing and 429 handling.

    Respects ``Retry-After`` when present; otherwise exponential backoff with jitter.
    Returns the final response (may still be 429 if retries exhausted).
    """
    last: httpx.Response | None = None
    for attempt in range(max_retries + 1):
        await limiter.acquire(1.0)
        last = await client.request(method, url, **kwargs)
        if last.status_code != 429:
            return last
        ra = parse_retry_after_header(last.headers.get("retry-after"))
        if ra is not None:
            delay = ra
        else:
            exp = base_backoff_seconds * (2**attempt)
            jitter = random.uniform(0, 0.5 * exp)
            delay = exp + jitter
        logger.warning(
            "uw.http_429_backoff",
            url=url[:120],
            attempt=attempt,
            delay_seconds=round(delay, 2),
        )
        await asyncio.sleep(delay)
    assert last is not None
    return last


async def uw_get(
    client: httpx.AsyncClient,
    url: str,
    *,
    limiter: TokenBucketRateLimiter | None = None,
    max_retries: int = 5,
    **kwargs: Any,
) -> httpx.Response:
    from shared.uw_runtime import get_uw_limiter

    lim = limiter or get_uw_limiter()
    return await uw_request(
        client, "GET", url, limiter=lim, max_retries=max_retries, **kwargs
    )


async def uw_get_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
    limiter: TokenBucketRateLimiter | None = None,
    use_cache: bool = True,
    cache_key: str | None = None,
    ttl_seconds: float | None = None,
    max_retries: int = 5,
    **kwargs: Any,
) -> dict[str, Any]:
    """GET JSON with optional TTL cache (shared ``JsonTTLCache`` when use_cache)."""
    from shared.uw_runtime import get_iv_vol_cache_ttl, get_uw_json_cache, get_uw_limiter

    lim = limiter or get_uw_limiter()
    key = cache_key or url

    async def _fetch() -> dict[str, Any]:
        resp = await uw_get(client, url, limiter=lim, max_retries=max_retries, headers=headers, **kwargs)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else {}

    if not use_cache:
        return await _fetch()
    cache = get_uw_json_cache()
    ttl = ttl_seconds if ttl_seconds is not None else get_iv_vol_cache_ttl()
    return await cache.get_or_set(key, ttl, _fetch)
