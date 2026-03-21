import pytest

from scanner.state.dedup import DedupCache


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
