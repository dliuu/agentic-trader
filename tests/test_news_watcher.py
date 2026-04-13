"""Tests for news watcher — catalyst detection, cadence, EDGAR parsing, dedup."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from tracker.config import NewsWatcherConfig
from tracker.models import NewsEvent, NewsEventType, Signal, SignalState
from tracker.news_watcher import EDGAR_SEARCH_URL, NewsWatcher, detect_catalysts


def _cfg(**kwargs) -> NewsWatcherConfig:
    base = NewsWatcherConfig(
        headline_interval_seconds=86_400,
        edgar_interval_seconds=86_400,
    )
    return replace(base, **kwargs) if kwargs else base


def _make_signal(**overrides) -> Signal:
    now = datetime.now(timezone.utc)
    d = dict(
        id="sig-news",
        ticker="ACME",
        strike=50.0,
        expiry=(now.date() + timedelta(days=30)).isoformat(),
        option_type="call",
        direction="bullish",
        state=SignalState.PENDING,
        initial_score=82,
        initial_premium=50_000,
        initial_oi=100,
        initial_volume=500,
        initial_contract_adv=0,
        grade_id="g1",
        conviction_score=82.0,
        created_at=now - timedelta(days=1),
        last_polled_at=now - timedelta(hours=2),
    )
    d.update(overrides)
    return Signal(**d)


@pytest.fixture(autouse=True)
def _isolated_trades_db(tmp_path, monkeypatch):
    monkeypatch.setattr("shared.db.DB_PATH", tmp_path / "trades_news.db")


class TestDetectCatalysts:
    def test_tier1_acquisition(self):
        cfg = NewsWatcherConfig()
        is_cat, kws, tier1 = detect_catalysts("Company announces acquisition target", cfg)
        assert is_cat
        assert tier1
        assert "acquisition" in kws

    def test_tier2_only(self):
        cfg = NewsWatcherConfig()
        is_cat, kws, tier1 = detect_catalysts("Analyst upgrade on price target raised", cfg)
        assert is_cat
        assert not tier1
        assert "upgrade" in kws or "price target" in kws


class TestNewsWatcherHeadlines:
    @respx.mock
    async def test_headlines_parsed_and_cutoff(self):
        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=1)).isoformat()
        recent = (now - timedelta(minutes=30)).isoformat()

        respx.get("https://api.unusualwhales.com/api/news/headlines").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "n1",
                            "headline": "ACME merger talks",
                            "published_at": old,
                        },
                        {
                            "id": "n2",
                            "headline": "ACME confirms merger timeline",
                            "published_at": recent,
                        },
                    ]
                },
            )
        )
        respx.get(EDGAR_SEARCH_URL).mock(
            return_value=httpx.Response(200, json={"hits": {"hits": []}})
        )

        cfg = _cfg(headline_interval_seconds=60, edgar_interval_seconds=86_400)
        async with httpx.AsyncClient() as client:
            nw = NewsWatcher(client, "tok", config=cfg)
            sig = _make_signal(last_polled_at=now - timedelta(hours=1))
            res = await nw.check(sig)

        assert len(res.events) == 1
        assert res.events[0].source_id == "n2"
        assert res.events[0].event_type == NewsEventType.HEADLINE
        assert res.events[0].catalyst_matched
        assert "merger" in res.events[0].catalyst_keywords

    @respx.mock
    async def test_cadence_skips_second_headline_poll(self):
        now = datetime.now(timezone.utc)
        recent = (now - timedelta(minutes=30)).isoformat()
        route = respx.get("https://api.unusualwhales.com/api/news/headlines").mock(
            return_value=httpx.Response(
                200,
                json={"data": [{"id": "x1", "headline": "ACME merger", "published_at": recent}]},
            )
        )
        respx.get(EDGAR_SEARCH_URL).mock(
            return_value=httpx.Response(200, json={"hits": {"hits": []}})
        )

        cfg = _cfg(headline_interval_seconds=3600, edgar_interval_seconds=86_400)
        async with httpx.AsyncClient() as client:
            nw = NewsWatcher(client, "tok", config=cfg)
            sig = _make_signal(last_polled_at=now - timedelta(hours=1))
            await nw.check(sig)
            await nw.check(sig)

        assert route.call_count == 1


class TestNewsWatcherEdgar:
    @respx.mock
    async def test_edgar_parses_hits(self):
        cfg = _cfg(headline_interval_seconds=86_400, edgar_interval_seconds=60)
        respx.get("https://api.unusualwhales.com/api/news/headlines").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        respx.get("https://efts.sec.gov/LATEST/search-index").mock(
            return_value=httpx.Response(
                200,
                json={
                    "hits": {
                        "hits": [
                            {
                                "_id": "acc-1",
                                "_source": {
                                    "form_type": "8-K",
                                    "file_date": "2026-04-01",
                                    "accession_no": "0001234567-26-000001",
                                    "entity_name": "ACME Corp",
                                    "tickers": ["ACME"],
                                },
                            }
                        ]
                    }
                },
            )
        )
        async with httpx.AsyncClient() as client:
            nw = NewsWatcher(client, "tok", config=cfg)
            sig = _make_signal(ticker="ACME")
            res = await nw.check(sig)

        assert any(e.event_type == NewsEventType.SEC_FILING for e in res.events)
        filing = next(e for e in res.events if e.event_type == NewsEventType.SEC_FILING)
        assert filing.filing_type == "8-K"
        assert filing.source_id == "0001234567-26-000001"
        assert "sec.gov" in (filing.url or "")


class TestNewsWatcherDedupAndRegrade:
    @respx.mock
    async def test_dedup_filters_existing_source_id(self):
        now = datetime.now(timezone.utc)
        recent = (now - timedelta(minutes=30)).isoformat()
        respx.get("https://api.unusualwhales.com/api/news/headlines").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [{"id": "dup1", "headline": "ACME merger story", "published_at": recent}]
                },
            )
        )
        respx.get(EDGAR_SEARCH_URL).mock(
            return_value=httpx.Response(200, json={"hits": {"hits": []}})
        )
        cfg = _cfg(headline_interval_seconds=60, edgar_interval_seconds=86_400)
        async with httpx.AsyncClient() as client:
            nw = NewsWatcher(client, "tok", config=cfg)
            sig = _make_signal(last_polled_at=now - timedelta(hours=1))
            ev = NewsEvent(
                id="pre",
                signal_id=sig.id,
                ticker=sig.ticker,
                event_type=NewsEventType.HEADLINE,
                title="old",
                source="uw_headlines",
                published_at=now - timedelta(hours=3),
                detected_at=now,
                source_id="dup1",
            )
            await nw.persist_events([ev])
            res = await nw.check(sig)

        assert res.events == []

    @respx.mock
    async def test_regrade_two_tier2_headlines(self):
        now = datetime.now(timezone.utc)
        recent = (now - timedelta(minutes=20)).isoformat()
        respx.get("https://api.unusualwhales.com/api/news/headlines").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "a", "headline": "Analyst upgrade on ACME", "published_at": recent},
                        {"id": "b", "headline": "Price target raised on ACME", "published_at": recent},
                    ]
                },
            )
        )
        respx.get(EDGAR_SEARCH_URL).mock(
            return_value=httpx.Response(200, json={"hits": {"hits": []}})
        )
        cfg = _cfg(headline_interval_seconds=60, edgar_interval_seconds=86_400)
        async with httpx.AsyncClient() as client:
            nw = NewsWatcher(client, "tok", config=cfg)
            sig = _make_signal(last_polled_at=now - timedelta(hours=1))
            res = await nw.check(sig)

        assert res.regrade_recommended
        assert len(res.events) == 2

    async def test_disabled_short_circuits(self):
        cfg = NewsWatcherConfig(enabled=False)
        async with httpx.AsyncClient() as client:
            nw = NewsWatcher(client, "tok", config=cfg)
            res = await nw.check(_make_signal())

        assert res.events == []
        assert not res.regrade_recommended
