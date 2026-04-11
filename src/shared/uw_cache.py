"""Short-TTL JSON response cache for idempotent UW GETs (per-ticker IV/vol, etc.)."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

class JsonTTLCache:
    """In-memory cache: key -> (expiry_monotonic, value)."""

    def __init__(self, default_ttl_seconds: float) -> None:
        if default_ttl_seconds <= 0:
            raise ValueError("default_ttl_seconds must be positive")
        self._default_ttl = default_ttl_seconds
        self._data: dict[str, tuple[float, Any]] = {}
        self._lock = asyncio.Lock()

    async def get_or_set(
        self,
        key: str,
        ttl_seconds: float | None,
        factory: Callable[[], Awaitable[Any]],
    ) -> Any:
        ttl = self._default_ttl if ttl_seconds is None else ttl_seconds
        now = time.monotonic()
        async with self._lock:
            hit = self._data.get(key)
            if hit is not None:
                exp, val = hit
                if now < exp:
                    return val
        value = await factory()
        async with self._lock:
            self._data[key] = (time.monotonic() + ttl, value)
        return value

    def clear(self) -> None:
        self._data.clear()
