"""Token-bucket rate limiter for Unusual Whales API calls (asyncio-safe)."""

from __future__ import annotations

import asyncio
import time


class TokenBucketRateLimiter:
    """Smooth rate limiting with burst capacity.

    ``rate_per_second`` tokens refill continuously up to ``capacity``.
    """

    def __init__(self, rate_per_second: float, capacity: float) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be positive")
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._rate = rate_per_second
        self._capacity = capacity
        self._tokens = float(capacity)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    @classmethod
    def from_calls_per_minute(cls, calls_per_minute: int, burst: int | None = None) -> TokenBucketRateLimiter:
        rate = calls_per_minute / 60.0
        cap = float(burst if burst is not None else max(5.0, calls_per_minute / 6.0))
        return cls(rate_per_second=rate, capacity=cap)

    async def acquire(self, cost: float = 1.0) -> None:
        """Block until ``cost`` tokens are available, then consume them."""
        if cost <= 0:
            return
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self._last
                self._last = now
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
                if self._tokens >= cost:
                    self._tokens -= cost
                    return
                deficit = cost - self._tokens
                wait = deficit / self._rate if self._rate > 0 else 0.1
            await asyncio.sleep(min(wait, 2.0))
