"""Process-wide UW rate limiter + JSON cache (set by scanner/grader/pipeline startup)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shared.uw_cache import JsonTTLCache
    from shared.uw_rate_limit import TokenBucketRateLimiter

_limiter: TokenBucketRateLimiter | None = None
_default_limiter: TokenBucketRateLimiter | None = None
_json_cache: JsonTTLCache | None = None
_iv_vol_ttl_seconds: float = 90.0


def configure_uw_runtime(
    *,
    limiter: TokenBucketRateLimiter,
    json_cache: JsonTTLCache | None = None,
    iv_vol_cache_ttl_seconds: float | None = None,
) -> None:
    """Initialize shared UW pacing and optional JSON cache for grader paths."""
    global _limiter, _json_cache, _iv_vol_ttl_seconds
    _limiter = limiter
    if json_cache is not None:
        _json_cache = json_cache
    if iv_vol_cache_ttl_seconds is not None:
        _iv_vol_ttl_seconds = iv_vol_cache_ttl_seconds


def get_uw_limiter() -> TokenBucketRateLimiter:
    global _default_limiter
    if _limiter is not None:
        return _limiter
    if _default_limiter is None:
        from shared.uw_rate_limit import TokenBucketRateLimiter as TB

        _default_limiter = TB.from_calls_per_minute(45, burst=10)
    return _default_limiter


def get_uw_json_cache() -> JsonTTLCache:
    global _json_cache
    if _json_cache is None:
        from shared.uw_cache import JsonTTLCache

        _json_cache = JsonTTLCache(default_ttl_seconds=_iv_vol_ttl_seconds)
    return _json_cache


def get_iv_vol_cache_ttl() -> float:
    return _iv_vol_ttl_seconds


def reset_uw_runtime_for_tests() -> None:
    global _limiter, _default_limiter, _json_cache, _iv_vol_ttl_seconds
    if _json_cache is not None:
        _json_cache.clear()
    _limiter = None
    _default_limiter = None
    _json_cache = None
    _iv_vol_ttl_seconds = 90.0
