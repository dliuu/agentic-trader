"""Tests for Gate 0: Ticker Universe Filter."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
import respx

from grader.gate0 import UW_BASE, run_gate0
from shared.filters import (
    ALLOW_LIST,
    BLOCKED_CHINA_ADRS,
    BLOCKED_MEGA_CAPS,
    BLOCKED_MEME_TICKERS,
    EXCLUDED_TICKERS,
    TickerExclusionReason,
    UniverseConfig,
    is_universe_blocked,
)
from shared.models import Candidate, SignalMatch


def _make_candidate(ticker: str = "ACME", **overrides) -> Candidate:
    """Factory for test candidates with sensible defaults."""
    defaults = dict(
        id="test-gate0-001",
        source="flow_alerts",
        ticker=ticker,
        direction="bullish",
        strike=50.0,
        expiry="2025-08-15",
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
    defaults.update(overrides)
    return Candidate(**defaults)


def _stock_info_response(
    ticker: str = "ACME",
    market_cap: float = 2_000_000_000,
    issue_type: str = "Common Stock",
    sector: str = "Technology",
) -> dict:
    """Build a mock /api/stock/{ticker}/info response."""
    return {
        "data": {
            "ticker": ticker,
            "marketCap": market_cap,
            "issue_type": issue_type,
            "sector": sector,
            "name": f"{ticker} Corp",
        }
    }


class TestIsUniverseBlocked:
    """Test the static block list check (is_universe_blocked)."""

    def test_normal_ticker_not_blocked(self):
        blocked, reason = is_universe_blocked("ACME")
        assert blocked is False
        assert reason is None

    def test_etf_blocked(self):
        blocked, reason = is_universe_blocked("SPY")
        assert blocked is True
        assert reason == TickerExclusionReason.ETF

    def test_mega_cap_blocked(self):
        blocked, reason = is_universe_blocked("AAPL")
        assert blocked is True
        assert reason == TickerExclusionReason.MEGA_CAP

    def test_mega_cap_case_insensitive(self):
        blocked, reason = is_universe_blocked("aapl")
        assert blocked is True
        assert reason == TickerExclusionReason.MEGA_CAP

    def test_meme_blocked(self):
        blocked, reason = is_universe_blocked("GME")
        assert blocked is True
        assert reason == TickerExclusionReason.MEME_STOCK

    def test_china_adr_blocked(self):
        blocked, reason = is_universe_blocked("BABA")
        assert blocked is True
        assert reason == TickerExclusionReason.CHINA_ADR

    def test_leveraged_etf_blocked(self):
        blocked, reason = is_universe_blocked("TQQQ")
        assert blocked is True
        assert reason == TickerExclusionReason.LEVERAGED_ETF

    def test_vix_product_blocked(self):
        blocked, reason = is_universe_blocked("UVXY")
        assert blocked is True
        assert reason == TickerExclusionReason.VIX_PRODUCT

    def test_all_mega_caps_blocked(self):
        for ticker in BLOCKED_MEGA_CAPS:
            blocked, reason = is_universe_blocked(ticker)
            assert blocked is True, f"{ticker} should be blocked"
            assert reason == TickerExclusionReason.MEGA_CAP

    def test_all_meme_tickers_blocked(self):
        for ticker in BLOCKED_MEME_TICKERS:
            blocked, reason = is_universe_blocked(ticker)
            assert blocked is True, f"{ticker} should be blocked"
            assert reason == TickerExclusionReason.MEME_STOCK

    def test_all_china_adrs_blocked(self):
        for ticker in BLOCKED_CHINA_ADRS:
            blocked, reason = is_universe_blocked(ticker)
            assert blocked is True, f"{ticker} should be blocked"
            assert reason == TickerExclusionReason.CHINA_ADR

    def test_allow_list_override(self, monkeypatch):
        monkeypatch.setattr("shared.filters.ALLOW_LIST", {"ACME", "BETA"})
        blocked, _ = is_universe_blocked("ACME")
        assert blocked is False
        blocked, _ = is_universe_blocked("OTHER")
        assert blocked is True

    def test_allow_list_does_not_override_etf_block(self, monkeypatch):
        monkeypatch.setattr("shared.filters.ALLOW_LIST", {"SPY", "ACME"})
        blocked, reason = is_universe_blocked("SPY")
        assert blocked is True
        assert reason == TickerExclusionReason.ETF


class TestRunGate0:
    """Test the full async Gate 0 pipeline with mocked UW API."""

    @respx.mock
    async def test_normal_small_cap_passes(self):
        respx.get(f"{UW_BASE}/api/stock/ACME/info").mock(
            return_value=httpx.Response(
                200,
                json=_stock_info_response(
                    ticker="ACME",
                    market_cap=2_000_000_000,
                    issue_type="Common Stock",
                ),
            )
        )
        async with httpx.AsyncClient() as client:
            result = await run_gate0(_make_candidate("ACME"), client, "fake-token")
        assert result.passed is True
        assert result.market_cap == 2_000_000_000
        assert result.issue_type == "Common Stock"

    @respx.mock
    async def test_mid_cap_passes(self):
        respx.get(f"{UW_BASE}/api/stock/MIDCO/info").mock(
            return_value=httpx.Response(200, json=_stock_info_response(market_cap=15_000_000_000))
        )
        async with httpx.AsyncClient() as client:
            result = await run_gate0(_make_candidate("MIDCO"), client, "fake-token")
        assert result.passed is True

    @respx.mock
    async def test_mega_cap_blocked_statically(self):
        async with httpx.AsyncClient() as client:
            result = await run_gate0(_make_candidate("AAPL"), client, "fake-token")
        assert result.passed is False
        assert result.reason == TickerExclusionReason.MEGA_CAP

    @respx.mock
    async def test_meme_blocked_statically(self):
        async with httpx.AsyncClient() as client:
            result = await run_gate0(_make_candidate("GME"), client, "fake-token")
        assert result.passed is False
        assert result.reason == TickerExclusionReason.MEME_STOCK

    @respx.mock
    async def test_too_small_market_cap_blocked(self):
        respx.get(f"{UW_BASE}/api/stock/TINY/info").mock(
            return_value=httpx.Response(
                200,
                json=_stock_info_response(ticker="TINY", market_cap=100_000_000),
            )
        )
        async with httpx.AsyncClient() as client:
            result = await run_gate0(_make_candidate("TINY"), client, "fake-token")
        assert result.passed is False
        assert result.reason == TickerExclusionReason.MARKET_CAP_OUT_OF_RANGE
        assert result.market_cap == 100_000_000

    @respx.mock
    async def test_too_large_market_cap_blocked(self):
        respx.get(f"{UW_BASE}/api/stock/HUGE/info").mock(
            return_value=httpx.Response(
                200,
                json=_stock_info_response(ticker="HUGE", market_cap=50_000_000_000),
            )
        )
        async with httpx.AsyncClient() as client:
            result = await run_gate0(_make_candidate("HUGE"), client, "fake-token")
        assert result.passed is False
        assert result.reason == TickerExclusionReason.MARKET_CAP_OUT_OF_RANGE

    @respx.mock
    async def test_etf_issue_type_blocked(self):
        respx.get(f"{UW_BASE}/api/stock/NEWETF/info").mock(
            return_value=httpx.Response(
                200,
                json=_stock_info_response(ticker="NEWETF", market_cap=5_000_000_000, issue_type="ETF"),
            )
        )
        async with httpx.AsyncClient() as client:
            result = await run_gate0(_make_candidate("NEWETF"), client, "fake-token")
        assert result.passed is False
        assert result.reason == TickerExclusionReason.NON_COMMON_STOCK

    @respx.mock
    async def test_adr_issue_type_blocked(self):
        respx.get(f"{UW_BASE}/api/stock/FORADR/info").mock(
            return_value=httpx.Response(
                200,
                json=_stock_info_response(ticker="FORADR", market_cap=3_000_000_000, issue_type="ADR"),
            )
        )
        async with httpx.AsyncClient() as client:
            result = await run_gate0(_make_candidate("FORADR"), client, "fake-token")
        assert result.passed is False
        assert result.reason == TickerExclusionReason.NON_COMMON_STOCK

    @respx.mock
    async def test_api_error_fails_open(self):
        respx.get(f"{UW_BASE}/api/stock/ERRCO/info").mock(return_value=httpx.Response(500))
        async with httpx.AsyncClient() as client:
            result = await run_gate0(_make_candidate("ERRCO"), client, "fake-token")
        assert result.passed is True

    @respx.mock
    async def test_api_timeout_fails_open(self):
        respx.get(f"{UW_BASE}/api/stock/SLOWCO/info").mock(
            side_effect=httpx.ReadTimeout("timeout", request=httpx.Request("GET", "http://test"))
        )
        async with httpx.AsyncClient() as client:
            result = await run_gate0(_make_candidate("SLOWCO"), client, "fake-token")
        assert result.passed is True

    @respx.mock
    async def test_missing_market_cap_fails_open(self):
        respx.get(f"{UW_BASE}/api/stock/NOVAL/info").mock(
            return_value=httpx.Response(
                200,
                json={"data": {"ticker": "NOVAL", "issue_type": "Common Stock"}},
            )
        )
        async with httpx.AsyncClient() as client:
            result = await run_gate0(_make_candidate("NOVAL"), client, "fake-token")
        assert result.passed is True

    @respx.mock
    async def test_boundary_min_market_cap(self):
        respx.get(f"{UW_BASE}/api/stock/BOUND/info").mock(
            return_value=httpx.Response(
                200,
                json=_stock_info_response(ticker="BOUND", market_cap=250_000_000),
            )
        )
        async with httpx.AsyncClient() as client:
            result = await run_gate0(_make_candidate("BOUND"), client, "fake-token")
        assert result.passed is True

    @respx.mock
    async def test_boundary_max_market_cap(self):
        respx.get(f"{UW_BASE}/api/stock/TOPCO/info").mock(
            return_value=httpx.Response(
                200,
                json=_stock_info_response(ticker="TOPCO", market_cap=20_000_000_000),
            )
        )
        async with httpx.AsyncClient() as client:
            result = await run_gate0(_make_candidate("TOPCO"), client, "fake-token")
        assert result.passed is True

    @respx.mock
    async def test_just_below_min_blocked(self):
        respx.get(f"{UW_BASE}/api/stock/SMOL/info").mock(
            return_value=httpx.Response(
                200,
                json=_stock_info_response(ticker="SMOL", market_cap=249_000_000),
            )
        )
        async with httpx.AsyncClient() as client:
            result = await run_gate0(_make_candidate("SMOL"), client, "fake-token")
        assert result.passed is False

    @respx.mock
    async def test_just_above_max_blocked(self):
        respx.get(f"{UW_BASE}/api/stock/BIGCO/info").mock(
            return_value=httpx.Response(
                200,
                json=_stock_info_response(ticker="BIGCO", market_cap=20_100_000_000),
            )
        )
        async with httpx.AsyncClient() as client:
            result = await run_gate0(_make_candidate("BIGCO"), client, "fake-token")
        assert result.passed is False

    @respx.mock
    async def test_custom_config(self):
        custom = UniverseConfig(
            min_market_cap=1_000_000_000,
            max_market_cap=5_000_000_000,
        )
        respx.get(f"{UW_BASE}/api/stock/MEDCO/info").mock(
            return_value=httpx.Response(200, json=_stock_info_response(market_cap=500_000_000))
        )
        async with httpx.AsyncClient() as client:
            result = await run_gate0(_make_candidate("MEDCO"), client, "fake-token", config=custom)
        assert result.passed is False

    @respx.mock
    async def test_sector_extracted(self):
        respx.get(f"{UW_BASE}/api/stock/ACME/info").mock(
            return_value=httpx.Response(200, json=_stock_info_response(sector="Healthcare"))
        )
        async with httpx.AsyncClient() as client:
            result = await run_gate0(_make_candidate("ACME"), client, "fake-token")
        assert result.passed is True
        assert result.sector == "Healthcare"

    @respx.mock
    async def test_flat_response_format(self):
        respx.get(f"{UW_BASE}/api/stock/FLAT/info").mock(
            return_value=httpx.Response(
                200,
                json={
                    "ticker": "FLAT",
                    "marketCap": 3_000_000_000,
                    "issue_type": "Common Stock",
                    "sector": "Technology",
                },
            )
        )
        async with httpx.AsyncClient() as client:
            result = await run_gate0(_make_candidate("FLAT"), client, "fake-token")
        assert result.passed is True
        assert result.market_cap == 3_000_000_000


class TestBlockListCompleteness:
    """Verify block lists have expected sizes."""

    def test_mega_cap_count(self):
        assert len(BLOCKED_MEGA_CAPS) >= 25, "Expected at least 25 mega-cap tickers"

    def test_meme_count(self):
        assert len(BLOCKED_MEME_TICKERS) >= 12, "Expected at least 12 meme tickers"

    def test_china_adr_count(self):
        assert len(BLOCKED_CHINA_ADRS) >= 8, "Expected at least 8 China ADR tickers"

    def test_no_overlap_mega_and_excluded(self):
        overlap = set(BLOCKED_MEGA_CAPS.keys()) & set(EXCLUDED_TICKERS.keys())
        assert len(overlap) == 0, f"Unexpected overlap: {overlap}"

    def test_no_overlap_meme_and_excluded(self):
        overlap = set(BLOCKED_MEME_TICKERS.keys()) & set(EXCLUDED_TICKERS.keys())
        assert len(overlap) == 0, f"Unexpected overlap: {overlap}"


def test_allow_list_module_default_is_set():
    """Sanity: ALLOW_LIST is a set (may be populated from GATE0_ALLOW_LIST at import)."""
    assert isinstance(ALLOW_LIST, set)
