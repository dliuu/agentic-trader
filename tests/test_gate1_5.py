"""Tests for Gate 1.5 explainability filter."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
import httpx
import pytest
import respx

from grader.context.explainability_ctx import ExplainabilityContext, build_explainability_context
from grader.gate1_5 import (
    _check_catalyst_alignment,
    _check_earnings_play,
    _check_hot_ticker,
    _check_sector_alignment,
    run_gate1_5,
)
from shared.filters import EXPLAINABILITY_CONFIG, ExplainabilityConfig, GateThresholds
from shared.models import Candidate, SignalMatch, SubScore


def _make_candidate(ticker: str = "ACME", direction: str = "bullish", expiry: str = "2025-08-15", **kw) -> Candidate:
    defaults = dict(
        id="test-g15-001",
        source="flow_alerts",
        ticker=ticker,
        direction=direction,
        strike=50.0,
        expiry=expiry,
        premium_usd=75_000.0,
        underlying_price=40.0,
        implied_volatility=0.45,
        execution_type="sweep",
        dte=30,
        signals=[SignalMatch(rule_name="otm", weight=1.0, detail="OTM 25%")],
        confluence_score=3.0,
        raw_alert_id="alert-001",
        scanned_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    return Candidate(**defaults)


def _make_flow_score(score: int = 65) -> SubScore:
    return SubScore(agent="flow_analyst", score=score, rationale="test", signals=[])


def _make_context(
    *,
    days_to_earnings: int | None = None,
    earnings_date: str | None = None,
    flow_alert_count_14d: int = 0,
    sector: str | None = None,
    sector_call_put_ratio: float | None = None,
    headlines_48h: list[dict] | None = None,
) -> ExplainabilityContext:
    return ExplainabilityContext(
        ticker="ACME",
        days_to_earnings=days_to_earnings,
        earnings_date=earnings_date,
        flow_alert_count_14d=flow_alert_count_14d,
        sector=sector,
        sector_call_put_ratio=sector_call_put_ratio,
        headlines_48h=headlines_48h or [],
    )


class TestCheckEarningsPlay:
    cfg = EXPLAINABILITY_CONFIG

    def test_no_earnings_data_no_penalty(self):
        c = _make_candidate()
        ctx = _make_context(days_to_earnings=None)
        assert _check_earnings_play(c, ctx, self.cfg) == 0

    def test_earnings_past_no_penalty(self):
        c = _make_candidate()
        ctx = _make_context(days_to_earnings=-3, earnings_date="2025-01-01")
        assert _check_earnings_play(c, ctx, self.cfg) == 0

    def test_earnings_too_far_no_penalty(self):
        c = _make_candidate()
        ctx = _make_context(days_to_earnings=15, earnings_date="2025-09-01")
        assert _check_earnings_play(c, ctx, self.cfg) == 0

    def test_standard_earnings_play_penalized(self):
        c = _make_candidate(expiry="2025-08-12")
        ctx = _make_context(days_to_earnings=3, earnings_date="2025-08-10")
        assert _check_earnings_play(c, ctx, self.cfg) == -25

    def test_long_dated_option_around_earnings_no_penalty(self):
        c = _make_candidate(expiry="2025-09-15")
        ctx = _make_context(days_to_earnings=3, earnings_date="2025-08-10")
        assert _check_earnings_play(c, ctx, self.cfg) == 0

    def test_option_expires_before_earnings_no_penalty(self):
        c = _make_candidate(expiry="2025-08-05")
        ctx = _make_context(days_to_earnings=3, earnings_date="2025-08-10")
        assert _check_earnings_play(c, ctx, self.cfg) == 0

    def test_earnings_boundary_7_days(self):
        c = _make_candidate(expiry="2025-08-12")
        ctx = _make_context(days_to_earnings=7, earnings_date="2025-08-10")
        assert _check_earnings_play(c, ctx, self.cfg) == -25

    def test_earnings_boundary_8_days(self):
        c = _make_candidate(expiry="2025-08-12")
        ctx = _make_context(days_to_earnings=8, earnings_date="2025-08-10")
        assert _check_earnings_play(c, ctx, self.cfg) == 0

    def test_expiry_boundary_5_days_after(self):
        c = _make_candidate(expiry="2025-08-15")
        ctx = _make_context(days_to_earnings=3, earnings_date="2025-08-10")
        assert _check_earnings_play(c, ctx, self.cfg) == -25

    def test_expiry_boundary_6_days_after(self):
        c = _make_candidate(expiry="2025-08-16")
        ctx = _make_context(days_to_earnings=3, earnings_date="2025-08-10")
        assert _check_earnings_play(c, ctx, self.cfg) == 0


class TestCheckHotTicker:
    cfg = EXPLAINABILITY_CONFIG

    def test_cold_ticker_no_penalty(self):
        assert _check_hot_ticker(_make_context(flow_alert_count_14d=0), self.cfg) == 0

    def test_below_threshold_no_penalty(self):
        assert _check_hot_ticker(_make_context(flow_alert_count_14d=4), self.cfg) == 0

    def test_tier_1_penalty(self):
        assert _check_hot_ticker(_make_context(flow_alert_count_14d=5), self.cfg) == -15

    def test_tier_1_upper_boundary(self):
        assert _check_hot_ticker(_make_context(flow_alert_count_14d=9), self.cfg) == -15

    def test_tier_2_penalty(self):
        assert _check_hot_ticker(_make_context(flow_alert_count_14d=10), self.cfg) == -20

    def test_tier_3_penalty(self):
        assert _check_hot_ticker(_make_context(flow_alert_count_14d=20), self.cfg) == -25

    def test_very_hot_ticker(self):
        assert _check_hot_ticker(_make_context(flow_alert_count_14d=100), self.cfg) == -25


class TestCheckSectorAlignment:
    cfg = EXPLAINABILITY_CONFIG

    def test_no_sector_data_no_penalty(self):
        c = _make_candidate()
        assert _check_sector_alignment(c, _make_context(sector_call_put_ratio=None), self.cfg) == 0

    def test_bullish_flow_bullish_sector_penalized(self):
        c = _make_candidate(direction="bullish")
        ctx = _make_context(sector_call_put_ratio=1.8)
        assert _check_sector_alignment(c, ctx, self.cfg) == -10

    def test_bullish_flow_neutral_sector_no_penalty(self):
        c = _make_candidate(direction="bullish")
        ctx = _make_context(sector_call_put_ratio=1.2)
        assert _check_sector_alignment(c, ctx, self.cfg) == 0

    def test_bearish_flow_bearish_sector_penalized(self):
        c = _make_candidate(direction="bearish")
        ctx = _make_context(sector_call_put_ratio=0.5)
        assert _check_sector_alignment(c, ctx, self.cfg) == -10

    def test_bullish_flow_bearish_sector_no_penalty(self):
        c = _make_candidate(direction="bullish")
        ctx = _make_context(sector_call_put_ratio=0.5)
        assert _check_sector_alignment(c, ctx, self.cfg) == 0

    def test_boundary_bullish_exact_threshold(self):
        c = _make_candidate(direction="bullish")
        ctx = _make_context(sector_call_put_ratio=1.5)
        assert _check_sector_alignment(c, ctx, self.cfg) == -10

    def test_boundary_bearish_exact_threshold(self):
        c = _make_candidate(direction="bearish")
        ctx = _make_context(sector_call_put_ratio=0.67)
        assert _check_sector_alignment(c, ctx, self.cfg) == -10


class TestCheckCatalystAlignment:
    cfg = EXPLAINABILITY_CONFIG

    def test_no_headlines_no_penalty(self):
        c = _make_candidate()
        assert _check_catalyst_alignment(c, _make_context(headlines_48h=[]), self.cfg) == 0

    def test_bullish_flow_bullish_headline_penalized(self):
        c = _make_candidate(direction="bullish")
        ctx = _make_context(headlines_48h=[{"title": "ACME gets major upgrade", "source": "x", "published_at": ""}])
        assert _check_catalyst_alignment(c, ctx, self.cfg) == -20

    def test_bullish_flow_bearish_headline_no_penalty(self):
        c = _make_candidate(direction="bullish")
        ctx = _make_context(headlines_48h=[{"title": "ACME hit with downgrade", "source": "x", "published_at": ""}])
        assert _check_catalyst_alignment(c, ctx, self.cfg) == 0

    def test_bearish_flow_bearish_headline_penalized(self):
        c = _make_candidate(direction="bearish")
        ctx = _make_context(
            headlines_48h=[{"title": "ACME misses estimates badly", "source": "x", "published_at": ""}]
        )
        assert _check_catalyst_alignment(c, ctx, self.cfg) == -20

    def test_neutral_catalyst_single_headline_no_penalty(self):
        c = _make_candidate(direction="bullish")
        ctx = _make_context(headlines_48h=[{"title": "analyst weighs in on ACME", "source": "x", "published_at": ""}])
        assert _check_catalyst_alignment(c, ctx, self.cfg) == 0

    def test_neutral_catalyst_multiple_headlines_penalized(self):
        c = _make_candidate(direction="bullish")
        ctx = _make_context(
            headlines_48h=[
                {"title": "merger talk around ACME", "source": "a", "published_at": ""},
                {"title": "deal structure still unclear", "source": "b", "published_at": ""},
            ]
        )
        assert _check_catalyst_alignment(c, ctx, self.cfg) == -20

    def test_case_insensitive(self):
        c = _make_candidate(direction="bullish")
        ctx = _make_context(headlines_48h=[{"title": "ACME UPGRADE FROM STREET", "source": "x", "published_at": ""}])
        assert _check_catalyst_alignment(c, ctx, self.cfg) == -20

    def test_partial_match(self):
        c = _make_candidate(direction="bullish")
        ctx = _make_context(
            headlines_48h=[{"title": "upgraded by Morgan Stanley on ACME", "source": "x", "published_at": ""}]
        )
        assert _check_catalyst_alignment(c, ctx, self.cfg) == -20


@pytest.mark.asyncio
class TestRunGate15:
    async def test_clean_candidate_passes(self, monkeypatch):
        async def fake_build(*args, **kwargs):
            return ExplainabilityContext(ticker="ACME")

        monkeypatch.setattr("grader.gate1_5.build_explainability_context", fake_build)
        c = _make_candidate()
        fs = _make_flow_score(65)
        async with httpx.AsyncClient() as client:
            r = await run_gate1_5(c, fs, client, "tok")
        assert r.passed is True
        assert r.penalty == 0
        assert r.combined_score == 65

    async def test_earnings_play_kills_marginal_candidate(self, monkeypatch):
        async def fake_build(*args, **kwargs):
            return _make_context(days_to_earnings=3, earnings_date="2025-08-10")

        monkeypatch.setattr("grader.gate1_5.build_explainability_context", fake_build)
        c = _make_candidate(expiry="2025-08-12")
        fs = _make_flow_score(55)
        async with httpx.AsyncClient() as client:
            r = await run_gate1_5(c, fs, client, "tok")
        assert r.earnings_penalty == -25
        assert r.combined_score == 30
        assert r.passed is False

    async def test_strong_flow_survives_single_penalty(self, monkeypatch):
        async def fake_build(*args, **kwargs):
            return _make_context(days_to_earnings=3, earnings_date="2025-08-10")

        monkeypatch.setattr("grader.gate1_5.build_explainability_context", fake_build)
        c = _make_candidate(expiry="2025-08-12")
        fs = _make_flow_score(85)
        async with httpx.AsyncClient() as client:
            r = await run_gate1_5(c, fs, client, "tok")
        assert r.combined_score == 60
        assert r.passed is True

    async def test_multiple_penalties_stack(self, monkeypatch):
        async def fake_build(*args, **kwargs):
            return _make_context(days_to_earnings=3, earnings_date="2025-08-10", flow_alert_count_14d=5)

        monkeypatch.setattr("grader.gate1_5.build_explainability_context", fake_build)
        c = _make_candidate(expiry="2025-08-12")
        fs = _make_flow_score(70)
        async with httpx.AsyncClient() as client:
            r = await run_gate1_5(c, fs, client, "tok")
        assert r.earnings_penalty == -25
        assert r.hot_ticker_penalty == -15
        assert r.combined_score == 30
        assert r.passed is False

    async def test_penalty_cap_at_minus_50(self, monkeypatch):
        async def fake_build(*args, **kwargs):
            return ExplainabilityContext(ticker="ACME")

        monkeypatch.setattr("grader.gate1_5.build_explainability_context", fake_build)
        monkeypatch.setattr("grader.gate1_5._check_earnings_play", lambda *a, **k: -25)
        monkeypatch.setattr("grader.gate1_5._check_hot_ticker", lambda *a, **k: -25)
        monkeypatch.setattr("grader.gate1_5._check_sector_alignment", lambda *a, **k: -10)
        monkeypatch.setattr("grader.gate1_5._check_catalyst_alignment", lambda *a, **k: -20)

        c = _make_candidate()
        fs = _make_flow_score(90)
        async with httpx.AsyncClient() as client:
            r = await run_gate1_5(c, fs, client, "tok")
        assert r.penalty == -50
        assert r.combined_score == 40
        assert r.passed is False

    async def test_custom_config_thresholds(self, monkeypatch):
        async def fake_build(*args, **kwargs):
            return _make_context(flow_alert_count_14d=5)

        monkeypatch.setattr("grader.gate1_5.build_explainability_context", fake_build)
        cfg = ExplainabilityConfig(hot_ticker_threshold_1=10)
        c = _make_candidate()
        fs = _make_flow_score(70)
        async with httpx.AsyncClient() as client:
            r = await run_gate1_5(c, fs, client, "tok", config=cfg)
        assert r.hot_ticker_penalty == 0

    async def test_custom_gate_threshold(self, monkeypatch):
        async def fake_build(*args, **kwargs):
            return _make_context(days_to_earnings=3, earnings_date="2025-08-10")

        monkeypatch.setattr("grader.gate1_5.build_explainability_context", fake_build)
        c = _make_candidate(expiry="2025-08-12")
        fs = _make_flow_score(55)
        gates = GateThresholds(gate1_5_combined_min=25)
        async with httpx.AsyncClient() as client:
            r = await run_gate1_5(c, fs, client, "tok", gate_cfg=gates)
        assert r.combined_score == 30
        assert r.passed is True


@respx.mock
@pytest.mark.asyncio
class TestContextBuilder:
    async def test_earnings_fetched_and_parsed(self):
        respx.get("https://api.unusualwhales.com/api/earnings/ACME").mock(
            return_value=httpx.Response(
                200,
                json={"data": [{"date": "2030-12-15T21:00:00+00:00"}]},
            )
        )
        respx.get("https://api.unusualwhales.com/api/news/headlines").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        c = _make_candidate()
        async with httpx.AsyncClient() as client:
            ctx = await build_explainability_context(c, client, "fake", scanner_db_path=None)
        assert ctx.days_to_earnings is not None
        assert ctx.days_to_earnings >= 1000
        assert ctx.earnings_date == "2030-12-15"

    async def test_headlines_fetched_and_filtered(self):
        now = datetime.now(timezone.utc)
        old = (now - timedelta(hours=72)).isoformat()
        recent = (now - timedelta(hours=6)).isoformat()
        respx.get("https://api.unusualwhales.com/api/earnings/ACME").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        respx.get("https://api.unusualwhales.com/api/news/headlines").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {"headline": "old", "published_at": old, "source": "x"},
                        {"headline": "fresh", "published_at": recent, "source": "y"},
                    ]
                },
            )
        )
        c = _make_candidate()
        async with httpx.AsyncClient() as client:
            ctx = await build_explainability_context(c, client, "fake", scanner_db_path=None)
        assert len(ctx.headlines_48h) == 1
        assert ctx.headlines_48h[0]["title"] == "fresh"

    async def test_sector_tide_fetched(self):
        respx.get("https://api.unusualwhales.com/api/earnings/ACME").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        respx.get("https://api.unusualwhales.com/api/news/headlines").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        respx.get("https://api.unusualwhales.com/api/market/technology/sector-tide").mock(
            return_value=httpx.Response(
                200,
                json={"data": [{"call_put_ratio": 1.75, "callPutRatio": 1.75}]},
            )
        )
        c = _make_candidate()
        async with httpx.AsyncClient() as client:
            ctx = await build_explainability_context(
                c, client, "fake", scanner_db_path=None, sector="Technology"
            )
        assert ctx.sector_call_put_ratio == 1.75

    async def test_hot_ticker_query(self, tmp_path):
        respx.get("https://api.unusualwhales.com/api/earnings/ACME").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        respx.get("https://api.unusualwhales.com/api/news/headlines").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        db_path = tmp_path / "scan.db"
        import aiosqlite

        async with aiosqlite.connect(str(db_path)) as db:
            await db.execute(
                """CREATE TABLE raw_alerts (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    received_at TEXT NOT NULL
                )"""
            )
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            for i in range(5):
                await db.execute(
                    "INSERT INTO raw_alerts (id, source, payload_json, received_at) VALUES (?, ?, ?, ?)",
                    (f"id-{i}", "uw", json.dumps({"ticker": "ACME"}), now),
                )
            await db.commit()

        c = _make_candidate()
        async with httpx.AsyncClient() as client:
            ctx = await build_explainability_context(
                c, client, "fake", scanner_db_path=str(db_path.resolve())
            )
        assert ctx.flow_alert_count_14d == 5

    async def test_api_failure_fail_open(self):
        respx.get("https://api.unusualwhales.com/api/earnings/ACME").mock(
            return_value=httpx.Response(500, json={"error": "uw down"})
        )
        respx.get("https://api.unusualwhales.com/api/news/headlines").mock(
            return_value=httpx.Response(500, json={"error": "uw down"})
        )
        c = _make_candidate()
        async with httpx.AsyncClient() as client:
            ctx = await build_explainability_context(c, client, "fake", scanner_db_path=None)
        assert ctx.days_to_earnings is None
        assert ctx.headlines_48h == []

    async def test_scanner_db_missing_fail_open(self):
        respx.get("https://api.unusualwhales.com/api/earnings/ACME").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        respx.get("https://api.unusualwhales.com/api/news/headlines").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        c = _make_candidate()
        async with httpx.AsyncClient() as client:
            ctx = await build_explainability_context(
                c, client, "fake", scanner_db_path="/nonexistent/path/scanner.db"
            )
        assert ctx.flow_alert_count_14d == 0
