"""Startup validation and optional health check for Unusual Whales API."""

from __future__ import annotations

import os
from typing import Any

import httpx

from shared.uw_cache import JsonTTLCache
from shared.uw_http import uw_get
from shared.uw_rate_limit import TokenBucketRateLimiter
from shared.uw_runtime import configure_uw_runtime

UW_BASE = "https://api.unusualwhales.com"
SKIP_HEALTH_ENV = "UW_SKIP_HEALTH_CHECK"


class UWTokenError(RuntimeError):
    """Raised when no valid API token is configured."""


def resolve_uw_api_token() -> str:
    """Read token from environment (UW_API_TOKEN or UNUSUAL_WHALES_API_TOKEN)."""
    raw = (os.environ.get("UW_API_TOKEN") or os.environ.get("UNUSUAL_WHALES_API_TOKEN") or "").strip()
    return raw


def require_uw_api_token() -> str:
    """Fail fast with a clear message if the token is missing."""
    token = resolve_uw_api_token()
    if not token:
        raise UWTokenError(
            "Missing Unusual Whales API token. Set UW_API_TOKEN (or UNUSUAL_WHALES_API_TOKEN) "
            "in your environment or .env file."
        )
    return token


def uw_auth_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "UW-CLIENT-API-ID": "100001",
        "Accept": "application/json",
    }


async def uw_health_check(
    client: httpx.AsyncClient,
    token: str,
    *,
    limiter: TokenBucketRateLimiter,
) -> None:
    """Lightweight authenticated GET; raises if credentials are invalid.

    Skipped when UW_SKIP_HEALTH_CHECK is set to 1/true/yes.
    """
    skip = os.environ.get(SKIP_HEALTH_ENV, "").strip().lower() in ("1", "true", "yes", "on")
    if skip:
        return
    url = f"{UW_BASE}/api/market/market-tide"
    resp = await uw_get(client, url, limiter=limiter, headers=uw_auth_headers(token), timeout=15.0)
    if resp.status_code == 401:
        raise UWTokenError(
            "Unusual Whales API returned 401 Unauthorized. Check UW_API_TOKEN / "
            "UNUSUAL_WHALES_API_TOKEN and subscription access."
        )
    resp.raise_for_status()


def _uw_section(config: dict[str, Any]) -> dict[str, Any]:
    return config.get("unusual_whales") or {}


async def bootstrap_uw_runtime_from_config(config: dict[str, Any]) -> str:
    """Configure shared limiter + JSON cache from rules.yaml and validate token.

    Runs an optional authenticated health check unless UW_SKIP_HEALTH_CHECK is set.
    Returns the resolved API token.
    """
    uw_cfg = _uw_section(config)
    rl = uw_cfg.get("rate_limit") or {}
    cpm = int(rl.get("calls_per_minute", 45))
    burst = int(rl.get("burst", 10))
    ttl = float(uw_cfg.get("iv_vol_cache_ttl_seconds", 90))
    limiter = TokenBucketRateLimiter.from_calls_per_minute(cpm, burst=burst)
    json_cache = JsonTTLCache(default_ttl_seconds=ttl)
    configure_uw_runtime(
        limiter=limiter,
        json_cache=json_cache,
        iv_vol_cache_ttl_seconds=ttl,
    )
    token = require_uw_api_token()
    async with httpx.AsyncClient(timeout=15.0) as hc:
        await uw_health_check(hc, token, limiter=limiter)
    return token
