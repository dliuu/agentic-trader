"""Deduplication cache.

Prevents the same trade from being flagged repeatedly
across polling cycles. Uses a TTL-based in-memory cache.
"""
from __future__ import annotations

import hashlib
import time

import structlog

logger = structlog.get_logger()


class DedupCache:
    def __init__(self, ttl_minutes: int, key_fields: list[str]):
        self._ttl = ttl_minutes * 60
        self._key_fields = key_fields
        self._seen: dict[str, float] = {}

    def _make_key(self, data: dict) -> str:
        parts = [str(data.get(f, "")) for f in self._key_fields]
        raw = "|".join(parts)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def is_duplicate(self, data: dict) -> bool:
        """Check if we've seen this trade recently."""
        key = self._make_key(data)
        now = time.time()

        self._seen = {k: v for k, v in self._seen.items() if now - v < self._ttl}

        if key in self._seen:
            return True

        self._seen[key] = now
        return False

    def mark_seen(self, data: dict):
        """Explicitly mark a trade as seen."""
        key = self._make_key(data)
        self._seen[key] = time.time()

    @property
    def size(self) -> int:
        return len(self._seen)
