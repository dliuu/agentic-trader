"""UW rate limiting — token bucket (shared with grader via ``shared.uw_rate_limit``)."""

from __future__ import annotations

from shared.uw_rate_limit import TokenBucketRateLimiter


def RateLimiter(calls_per_minute: int = 30, burst: int | None = None) -> TokenBucketRateLimiter:
    """Factory matching legacy ``RateLimiter(calls_per_minute=…)`` tests and scanner."""
    return TokenBucketRateLimiter.from_calls_per_minute(calls_per_minute, burst=burst)
