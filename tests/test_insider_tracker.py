"""Unit tests for insider tracker context, clustering, and agent helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from grader.agents.insider_tracker import InsiderTracker
from grader.context.insider_ctx import (
    DerivedInsiderSignals,
    InsiderContext,
    _cross_validate_sources,
    _detect_clusters,
    _merge_and_dedup_transactions,
    build_data_availability_section,
    build_insider_context,
    make_skip_score,
    should_skip_insider_analysis,
)
from grader.prompt import build_insider_tracker_user_prompt
from shared.filters import InsiderScoringConfig
from shared.models import Candidate, SignalMatch

from tests.fixtures.insider_fixtures import minimal_insider_context


def _candidate() -> Candidate:
    return Candidate(
        id="1",
        source="t",
        ticker="TEST",
        direction="bullish",
        strike=100.0,
        expiry="2024-12-20",
        premium_usd=50_000,
        underlying_price=99.0,
        implied_volatility=0.3,
        execution_type="sweep",
        dte=30,
        signals=[SignalMatch(rule_name="r", weight=1.0, detail="d")],
        confluence_score=3.0,
        raw_alert_id="x",
        scanned_at=datetime(2024, 3, 15, 16, 0, 0, tzinfo=timezone.utc),
    )


class TestClusterDetection:
    def test_two_insiders_same_week_detected(self):
        txns = [
            {
                "insider_name": "A",
                "transaction_type": "P",
                "filing_date": "2024-03-01",
                "value": 1000,
            },
            {
                "insider_name": "B",
                "transaction_type": "P",
                "filing_date": "2024-03-03",
                "value": 2000,
            },
        ]
        clusters = _detect_clusters(txns, window_days=14, min_insiders=2, direction="buy")
        assert len(clusters) >= 1
        assert set(clusters[0]["insiders"]) == {"A", "B"}

    def test_same_insider_twice_not_cluster(self):
        txns = [
            {
                "insider_name": "A",
                "transaction_type": "P",
                "filing_date": "2024-03-01",
                "value": 1000,
            },
            {
                "insider_name": "A",
                "transaction_type": "P",
                "filing_date": "2024-03-02",
                "value": 2000,
            },
        ]
        clusters = _detect_clusters(txns, window_days=14, min_insiders=2, direction="buy")
        assert clusters == []

    def test_cluster_across_window_boundary(self):
        txns = [
            {
                "insider_name": "A",
                "transaction_type": "P",
                "filing_date": "2024-03-01",
                "value": 1000,
            },
            {
                "insider_name": "B",
                "transaction_type": "P",
                "filing_date": "2024-03-20",
                "value": 2000,
            },
        ]
        clusters = _detect_clusters(txns, window_days=14, min_insiders=2, direction="buy")
        assert clusters == []

    def test_sell_cluster_detected(self):
        txns = [
            {
                "insider_name": "A",
                "transaction_type": "S",
                "filing_date": "2024-03-01",
                "value": 1000,
            },
            {
                "insider_name": "B",
                "transaction_type": "S",
                "filing_date": "2024-03-02",
                "value": 2000,
            },
        ]
        clusters = _detect_clusters(txns, window_days=14, min_insiders=2, direction="sell")
        assert len(clusters) >= 1


class TestCrossValidation:
    def test_agreement_when_both_bullish(self):
        recent = datetime.now(timezone.utc) - timedelta(days=10)
        ds = recent.strftime("%Y-%m-%d")
        uw = [
            {"transaction_type": "P", "filing_date": ds},
            {"transaction_type": "P", "filing_date": ds},
        ]
        fh = [{"change": 100, "filingDate": ds}, {"change": 50, "filingDate": ds}]
        assert _cross_validate_sources(uw, fh, lookback_days=90) is True

    def test_disagreement_when_opposite(self):
        recent = datetime.now(timezone.utc) - timedelta(days=10)
        ds = recent.strftime("%Y-%m-%d")
        uw = [{"transaction_type": "P", "filing_date": ds}]
        fh = [{"change": -100, "filingDate": ds}]
        assert _cross_validate_sources(uw, fh, lookback_days=90) is False

    def test_none_when_insufficient_data(self):
        uw = []
        fh = [{"change": 1, "filingDate": "2024-01-01"}]
        assert _cross_validate_sources(uw, fh, lookback_days=90) is None


class TestEmptyDataHandling:
    def test_all_empty_returns_skip(self):
        d = DerivedInsiderSignals(has_sufficient_data=False)
        ctx = minimal_insider_context(derived=d)
        skip, _ = should_skip_insider_analysis(ctx)
        assert skip is True
        s = make_skip_score()
        assert s.skipped is True
        assert s.score == 50

    def test_only_congressional_data_runs_llm(self):
        d = DerivedInsiderSignals(has_sufficient_data=False, num_political_holders=1)
        ctx = minimal_insider_context(
            derived=d,
            political_holders=[{"politician": "X", "party": "D", "chamber": "House"}],
        )
        skip, _ = should_skip_insider_analysis(ctx)
        assert skip is False

    def test_only_finnhub_data_runs_llm(self):
        d = DerivedInsiderSignals(has_sufficient_data=True)
        ctx = minimal_insider_context(derived=d)
        skip, _ = should_skip_insider_analysis(ctx)
        assert skip is False


class TestConfidenceAdjustment:
    def test_full_data_no_adjustment(self):
        tracker = InsiderTracker(
            MagicMock(spec=httpx.AsyncClient),
            "t",
            "",
            MagicMock(),
            InsiderScoringConfig(),
        )
        ctx = minimal_insider_context(
            data_availability={k: True for k in minimal_insider_context().data_availability},
        )
        assert tracker._apply_confidence_adjustment(80, ctx) == 80

    def test_sparse_data_compresses_high_score(self):
        tracker = InsiderTracker(
            MagicMock(spec=httpx.AsyncClient),
            "t",
            "",
            MagicMock(),
            InsiderScoringConfig(),
        )
        da = {k: False for k in minimal_insider_context().data_availability}
        da["uw_form4"] = True
        da["uw_buy_sells"] = True
        ctx = minimal_insider_context(data_availability=da)
        adj = tracker._apply_confidence_adjustment(90, ctx)
        assert adj < 90
        assert adj > 50

    def test_sparse_data_compresses_low_score(self):
        tracker = InsiderTracker(
            MagicMock(spec=httpx.AsyncClient),
            "t",
            "",
            MagicMock(),
            InsiderScoringConfig(),
        )
        da = {k: False for k in minimal_insider_context().data_availability}
        da["uw_form4"] = True
        da["finnhub_transactions"] = True
        ctx = minimal_insider_context(data_availability=da)
        adj = tracker._apply_confidence_adjustment(20, ctx)
        assert adj > 20
        assert adj < 50


class TestPromptRendering:
    def test_empty_sections_render_cleanly(self):
        ctx = minimal_insider_context()
        text = build_insider_tracker_user_prompt(ctx)
        assert "(No insider transactions found for this ticker)" in text or "None" in text

    def test_data_availability_flags_accurate(self):
        ctx = minimal_insider_context(
            data_availability={
                "uw_form4": True,
                "uw_buy_sells": False,
                "uw_insider_flow": False,
                "uw_political_holders": False,
                "uw_congressional_trades": False,
                "finnhub_transactions": False,
                "finnhub_mspr": False,
            }
        )
        sec = build_data_availability_section(ctx)
        assert "uw_form4" in sec
        assert "Available" in sec

    def test_transaction_dedup_across_sources(self):
        uw = [
            {
                "insider_name": "Jane",
                "transaction_type": "P",
                "filing_date": "2024-03-10",
                "shares": 100,
                "value": 5000,
            }
        ]
        fh = [
            {
                "name": "Jane",
                "change": 100,
                "filingDate": "2024-03-10",
                "transactionDate": "2024-03-10",
            }
        ]
        merged = _merge_and_dedup_transactions(uw, fh)
        assert len(merged) == 1


@pytest.mark.asyncio
async def test_build_insider_context_never_raises(monkeypatch):
    """Parallel fetches are resilient — empty HTTP client mocks return safe defaults."""

    async def fake_get(*args, **kwargs):
        r = MagicMock()
        r.status_code = 404
        r.json.return_value = {}
        return r

    client = MagicMock(spec=httpx.AsyncClient)
    client.get = AsyncMock(side_effect=fake_get)

    from shared.finnhub_client import FinnhubClient

    fh = FinnhubClient(client, "")
    cand = _candidate()

    ctx = await build_insider_context(cand, client, "token", fh, InsiderScoringConfig())
    assert ctx.ticker == "TEST"
    assert isinstance(ctx.data_availability, dict)


@pytest.mark.asyncio
async def test_insider_tracker_llm_path(monkeypatch):
    """Agent returns adjusted SubScore when LLM succeeds."""
    mock_llm = MagicMock()
    mock_llm.complete = AsyncMock(
        return_value=MagicMock(
            text='{"score": 72, "verdict": "pass", "rationale": "ok", "signals_confirmed": ["x"], "risk_factors": [], "likely_directional": true}'
        )
    )

    tracker = InsiderTracker(MagicMock(spec=httpx.AsyncClient), "t", "", mock_llm)

    d = DerivedInsiderSignals(has_sufficient_data=True)
    ctx = InsiderContext(
        ticker="T",
        option_type="call",
        trade_direction="bullish",
        scanned_at=datetime.now(timezone.utc),
        form4_filings=[{"insider_name": "A", "transaction_type": "P", "filing_date": "2024-03-01", "value": 1}],
        buy_sell_summary={},
        insider_flow=[],
        political_holders=[],
        congressional_trades=[],
        finnhub_transactions=[],
        finnhub_mspr=None,
        derived=d,
        data_availability={k: True for k in minimal_insider_context().data_availability},
    )

    async def fake_build(*_a, **_k):
        return ctx

    monkeypatch.setattr("grader.agents.insider_tracker.build_insider_context", fake_build)
    monkeypatch.setattr("grader.agents.insider_tracker.should_skip_insider_analysis", lambda c: (False, ""))

    out = await tracker.score(_candidate())
    assert out.agent == "insider_tracker"
    assert out.skipped is False
    assert out.score == 72


@pytest.mark.asyncio
async def test_insider_tracker_skip_path(monkeypatch):
    mock_llm = MagicMock()
    tracker = InsiderTracker(MagicMock(spec=httpx.AsyncClient), "t", "", mock_llm)

    d = DerivedInsiderSignals(has_sufficient_data=False)
    ctx = InsiderContext(
        ticker="T",
        option_type="call",
        trade_direction="bullish",
        scanned_at=datetime.now(timezone.utc),
        form4_filings=[],
        buy_sell_summary=None,
        insider_flow=[],
        political_holders=[],
        congressional_trades=[],
        finnhub_transactions=[],
        finnhub_mspr=None,
        derived=d,
        data_availability=minimal_insider_context().data_availability,
    )

    async def fake_build(*_a, **_k):
        return ctx

    monkeypatch.setattr("grader.agents.insider_tracker.build_insider_context", fake_build)

    out = await tracker.score(_candidate())
    assert out.skipped is True
    mock_llm.complete.assert_not_called()
