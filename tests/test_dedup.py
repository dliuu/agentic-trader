import pytest
from unittest.mock import patch

from scanner.state.dedup import DedupCache


def test_dedup_expires_after_ttl():
    """Entries expire after TTL; previously seen key becomes fresh again."""
    with patch("scanner.state.dedup.time") as mock_time:
        mock_time.time.side_effect = [0, 0, 61]
        cache = DedupCache(ttl_minutes=1, key_fields=["ticker"])
        key = {"ticker": "X"}
        assert cache.is_duplicate(key) is False  # t=0, marks seen
        assert cache.is_duplicate(key) is True  # t=0, duplicate
        assert cache.is_duplicate(key) is False  # t=61, evicted, fresh again


def test_dedup_identifies_duplicate():
    cache = DedupCache(ttl_minutes=60, key_fields=["ticker", "strike", "expiry", "direction"])
    key = {"ticker": "AAPL", "strike": 150, "expiry": "2026-04-03", "direction": "bullish"}
    assert cache.is_duplicate(key) is False
    assert cache.is_duplicate(key) is True


def test_dedup_different_keys():
    cache = DedupCache(ttl_minutes=60, key_fields=["ticker", "strike"])
    assert cache.is_duplicate({"ticker": "A", "strike": 1}) is False
    assert cache.is_duplicate({"ticker": "B", "strike": 1}) is False
    assert cache.is_duplicate({"ticker": "A", "strike": 1}) is True


def test_dedup_mark_seen():
    cache = DedupCache(ttl_minutes=60, key_fields=["ticker"])
    cache.mark_seen({"ticker": "X"})
    assert cache.is_duplicate({"ticker": "X"}) is True
    assert cache.size == 1
