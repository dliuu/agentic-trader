"""Token bucket rate limiter for API calls.

UW API has rate limits. We enforce our own conservative
limit to stay well under — better to poll slightly less
than get 429'd and miss a cycle entirely.
"""
import asyncio
import time


class RateLimiter:
    def __init__(self, calls_per_minute: int = 30):
        self._interval = 60.0 / calls_per_minute
        self._last_call = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = time.monotonic()
            wait = self._interval - (now - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()
