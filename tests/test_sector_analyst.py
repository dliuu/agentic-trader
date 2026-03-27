"""Tests for deterministic sector analyst and sector context parsing."""

from __future__ import annotations

from datetime import date, timedelta

from grader.agents.sector_analyst import SectorAnalystResult, score_sector
from grader.agents.sector_scoring_config import SECTOR_SCORING
from grader.context.sector_ctx import (
    HIGH_IMPACT_EVENT_KEYWORDS,
    EconomicEvent,
    FDADate,
    MarketTide,
    SectorContext,
    SectorETF,
    SectorTide,
    parse_economic_calendar,
    parse_fda_calendar,
    parse_market_tide,
    parse_sector_etfs,
    parse_sector_tide,
)


def _tide(cp_ratio: float, net_premium: float = 0.0) -> SectorTide:
    return SectorTide(
        sector="technology",
        bullish_premium=1.0,
        bearish_premium=1.0,
        net_premium=net_premium,
        call_volume=100.0,
        put_volume=100.0,
        call_put_ratio=cp_ratio,
        raw={},
    )


def _market(cp_ratio: float) -> MarketTide:
    return MarketTide(
        bullish_premium=1.0,
        bearish_premium=1.0,
        net_premium=0.0,
        call_volume=100.0,
        put_volume=100.0,
        call_put_ratio=cp_ratio,
        raw={},
    )


def _make_ctx(
    *,
    sector_tide: SectorTide | None = None,
    market_tide: MarketTide | None = None,
    sector_etf: SectorETF | None = None,
    high_impact_events: list[EconomicEvent] | None = None,
    economic_events: list[EconomicEvent] | None = None,
    is_biotech: bool = False,
    has_upcoming_fda: bool = False,
    fda_dates: list[FDADate] | None = None,
    ticker: str = "TEST",
    ticker_sector: str | None = "Technology",
) -> SectorContext:
    ev = economic_events if economic_events is not None else []
    hi = high_impact_events
    if hi is None:
        hi = [e for e in ev if e.is_high_impact]
    return SectorContext(
        ticker=ticker,
        ticker_sector=ticker_sector,
        sector_slug="technology",
        is_biotech=is_biotech,
        has_upcoming_fda=has_upcoming_fda,
        sector_tide=sector_tide,
        market_tide=market_tide,
        economic_events=ev,
        high_impact_events=hi,
        sector_etf=sector_etf,
        fda_dates=fda_dates or [],
        fetch_errors=[],
    )


class TestSectorFlowScoring:
    def test_strong_bullish_sector(self):
        ctx = _make_ctx(sector_tide=_tide(1.6), market_tide=_market(1.0))
        r = score_sector(ctx)
        assert "sector_flow_strong_bullish" in r.signals

    def test_bullish_sector(self):
        ctx = _make_ctx(sector_tide=_tide(1.2), market_tide=_market(1.0))
        r = score_sector(ctx)
        assert "sector_flow_bullish" in r.signals

    def test_neutral_sector(self):
        ctx = _make_ctx(sector_tide=_tide(1.0), market_tide=_market(1.0))
        r = score_sector(ctx)
        assert "sector_flow_neutral" in r.signals

    def test_bearish_sector(self):
        ctx = _make_ctx(sector_tide=_tide(0.8), market_tide=_market(1.0))
        r = score_sector(ctx)
        assert "sector_flow_bearish" in r.signals

    def test_strong_bearish_sector(self):
        ctx = _make_ctx(sector_tide=_tide(0.5), market_tide=_market(1.0))
        r = score_sector(ctx)
        assert r.score <= 41

    def test_no_sector_tide(self):
        ctx = _make_ctx(sector_tide=None, market_tide=_market(1.0))
        r = score_sector(ctx)
        assert "sector_tide_unavailable" in r.signals

    def test_sector_etf_strong_day(self):
        etf = SectorETF(
            sector="Technology",
            ticker="XLK",
            performance_1d=0.03,
            performance_5d=0.0,
            performance_1m=0.0,
            raw={},
        )
        ctx = _make_ctx(sector_tide=_tide(1.0), market_tide=_market(1.0), sector_etf=etf)
        r = score_sector(ctx)
        assert "sector_etf_strong_day" in r.signals

    def test_sector_etf_weak_day(self):
        etf = SectorETF(
            sector="Technology",
            ticker="XLK",
            performance_1d=-0.03,
            performance_5d=0.0,
            performance_1m=0.0,
            raw={},
        )
        ctx = _make_ctx(sector_tide=_tide(1.0), market_tide=_market(1.0), sector_etf=etf)
        r = score_sector(ctx)
        assert "sector_etf_weak_day" in r.signals


class TestMarketTideScoring:
    def test_strong_bullish_market(self):
        ctx = _make_ctx(sector_tide=_tide(1.0), market_tide=_market(1.5))
        r = score_sector(ctx)
        assert "market_strong_bullish" in r.signals

    def test_bearish_market(self):
        ctx = _make_ctx(sector_tide=_tide(1.0), market_tide=_market(0.85))
        r = score_sector(ctx)
        assert "market_bearish" in r.signals

    def test_no_market_tide(self):
        ctx = _make_ctx(sector_tide=_tide(1.0), market_tide=None)
        r = score_sector(ctx)
        assert "market_tide_unavailable" in r.signals


class TestEconomicCalendar:
    def test_high_impact_within_3_days(self):
        ref = date(2026, 3, 1)
        ev = date(2026, 3, 3)
        hi = [
            EconomicEvent(
                name="FOMC",
                date=ev.isoformat(),
                is_high_impact=True,
                raw={},
            )
        ]
        ctx = _make_ctx(
            sector_tide=_tide(1.0),
            market_tide=_market(1.0),
            high_impact_events=hi,
            economic_events=hi,
        )
        r = score_sector(ctx, reference_date=ref)
        assert "high_impact_econ_within_3d" in r.signals

    def test_high_impact_within_7_days(self):
        ref = date(2026, 3, 1)
        ev = date(2026, 3, 6)
        hi = [
            EconomicEvent(
                name="CPI",
                date=ev.isoformat(),
                is_high_impact=True,
                raw={},
            )
        ]
        ctx = _make_ctx(
            sector_tide=_tide(1.0),
            market_tide=_market(1.0),
            high_impact_events=hi,
            economic_events=hi,
        )
        r = score_sector(ctx, reference_date=ref)
        assert "high_impact_econ_within_7d" in r.signals

    def test_high_impact_distant(self):
        ref = date(2026, 3, 1)
        ev = date(2026, 3, 16)
        hi = [
            EconomicEvent(
                name="Nonfarm Payroll",
                date=ev.isoformat(),
                is_high_impact=True,
                raw={},
            )
        ]
        ctx = _make_ctx(
            sector_tide=_tide(1.0),
            market_tide=_market(1.0),
            high_impact_events=hi,
            economic_events=hi,
        )
        r = score_sector(ctx, reference_date=ref)
        assert "high_impact_econ_distant" in r.signals

    def test_no_events(self):
        ctx = _make_ctx(
            sector_tide=_tide(1.0),
            market_tide=_market(1.0),
            high_impact_events=[],
            economic_events=[],
        )
        r = score_sector(ctx)
        assert "no_high_impact_econ_events" in r.signals


class TestFDAFlag:
    def test_biotech_with_fda_dates(self):
        fda = FDADate(
            ticker="XBI",
            drug_name="DrugA",
            event_type="PDUFA",
            date="2026-06-01",
            raw={},
        )
        ctx = _make_ctx(
            ticker="XBI",
            ticker_sector="Healthcare",
            is_biotech=True,
            has_upcoming_fda=True,
            fda_dates=[fda],
        )
        r = score_sector(ctx)
        assert r.has_fda_flag is True
        assert any(s.startswith("fda_upcoming:") for s in r.signals)

    def test_biotech_without_fda_dates(self):
        ctx = _make_ctx(
            ticker="XBI",
            ticker_sector="Healthcare",
            is_biotech=True,
            has_upcoming_fda=False,
            fda_dates=[],
        )
        r = score_sector(ctx)
        assert r.has_fda_flag is False
        assert f"biotech_no_fda_dates:{ctx.ticker}" in r.signals

    def test_non_biotech_no_fda_check(self):
        ctx = _make_ctx(is_biotech=False, ticker_sector="Technology")
        r = score_sector(ctx)
        assert not any("fda" in s.lower() for s in r.signals)

    def test_fda_flag_does_not_change_score(self):
        base = _make_ctx(
            sector_tide=_tide(1.0),
            market_tide=_market(1.0),
            ticker_sector="Healthcare",
            is_biotech=True,
            has_upcoming_fda=False,
            fda_dates=[],
        )
        fda = FDADate(
            ticker="XBI",
            drug_name="DrugA",
            event_type="PDUFA",
            date=(date.today() + timedelta(days=30)).isoformat(),
            raw={},
        )
        with_fda = _make_ctx(
            sector_tide=_tide(1.0),
            market_tide=_market(1.0),
            ticker_sector="Healthcare",
            is_biotech=True,
            has_upcoming_fda=True,
            fda_dates=[fda],
        )
        r0 = score_sector(base)
        r1 = score_sector(with_fda)
        assert r0.score == r1.score


class TestScoreBoundaries:
    def test_score_never_below_1(self):
        ref = date(2026, 3, 1)
        hi = [
            EconomicEvent(
                name="FOMC",
                date="2026-03-02",
                is_high_impact=True,
                raw={},
            )
        ]
        ctx = _make_ctx(
            sector_tide=_tide(0.3),
            market_tide=_market(0.3),
            high_impact_events=hi,
            economic_events=hi,
        )
        r = score_sector(ctx, reference_date=ref)
        assert r.score >= 1

    def test_score_never_above_100(self):
        ctx = _make_ctx(sector_tide=_tide(2.0), market_tide=_market(2.0))
        r = score_sector(ctx)
        assert r.score <= 100


class TestWeights:
    def test_default_weights_sum_to_1(self):
        c = SECTOR_SCORING
        assert abs(c.weight_sector_flow + c.weight_market_tide + c.weight_economic - 1.0) < 1e-9

    def test_sector_flow_has_most_weight(self):
        c = SECTOR_SCORING
        assert c.weight_sector_flow > c.weight_market_tide > c.weight_economic

    def test_economic_has_least_weight(self):
        c = SECTOR_SCORING
        assert c.weight_economic < c.weight_market_tide
        assert c.weight_economic < c.weight_sector_flow


class TestEndToEnd:
    def test_bullish_everything(self):
        ctx = _make_ctx(sector_tide=_tide(1.6), market_tide=_market(1.5))
        r = score_sector(ctx)
        assert r.score >= 65

    def test_bearish_everything(self):
        ref = date(2026, 3, 1)
        hi = [
            EconomicEvent(
                name="FOMC",
                date="2026-03-02",
                is_high_impact=True,
                raw={},
            )
        ]
        ctx = _make_ctx(
            sector_tide=_tide(0.5),
            market_tide=_market(0.5),
            high_impact_events=hi,
            economic_events=hi,
        )
        r = score_sector(ctx, reference_date=ref)
        assert r.score <= 35

    def test_mixed_signals(self):
        ctx = _make_ctx(sector_tide=_tide(1.2), market_tide=_market(0.85))
        r = score_sector(ctx)
        assert 40 <= r.score <= 60

    def test_biotech_fda_scenario(self):
        fda = FDADate(
            ticker="LAB",
            drug_name="X",
            event_type="PDUFA",
            date=(date.today() + timedelta(days=60)).isoformat(),
            raw={},
        )
        ctx = _make_ctx(
            ticker="LAB",
            ticker_sector="Healthcare",
            is_biotech=True,
            has_upcoming_fda=True,
            fda_dates=[fda],
            sector_tide=_tide(1.0),
            market_tide=_market(1.0),
        )
        r = score_sector(ctx)
        assert 48 <= r.score <= 55
        assert r.has_fda_flag is True

    def test_result_has_component_scores(self):
        ctx = _make_ctx(sector_tide=_tide(1.0), market_tide=_market(1.0))
        r = score_sector(ctx)
        assert r.component_scores.keys() == {"sector_flow", "market_tide", "economic"}

    def test_all_data_missing(self):
        ctx = _make_ctx(sector_tide=None, market_tide=None, high_impact_events=[], economic_events=[])
        r = score_sector(ctx)
        assert 48 <= r.score <= 52


class TestContextParsing:
    def test_parse_sector_tide_data_wrapper(self):
        payload = {
            "data": {
                "sector": "technology",
                "bullish_premium": 1.0,
                "bearish_premium": 1.0,
                "net_premium": 0.5,
                "call_volume": 100.0,
                "put_volume": 100.0,
                "call_put_ratio": 1.2,
            }
        }
        t = parse_sector_tide(payload)
        assert t is not None
        assert t.call_put_ratio == 1.2

    def test_parse_sector_tide_bare_list(self):
        payload = [
            {
                "sector": "technology",
                "call_put_ratio": 1.1,
                "bullish_premium": 1.0,
                "bearish_premium": 1.0,
                "net_premium": 0.0,
                "call_volume": 1.0,
                "put_volume": 1.0,
            }
        ]
        t = parse_sector_tide(payload)
        assert t is not None
        assert t.call_put_ratio == 1.1

    def test_parse_sector_tide_none(self):
        assert parse_sector_tide(None) is None

    def test_parse_market_tide(self):
        payload = {"data": {"call_put_ratio": 1.05, "bullish_premium": 1.0, "bearish_premium": 1.0}}
        m = parse_market_tide(payload)
        assert m is not None
        assert m.call_put_ratio == 1.05

    def test_parse_economic_calendar(self):
        payload = {
            "data": [
                {"name": "FOMC Meeting", "date": "2026-03-15"},
                {"name": "Housing Starts", "date": "2026-03-16"},
            ]
        }
        evs = parse_economic_calendar(payload)
        assert evs[0].is_high_impact is True
        assert evs[1].is_high_impact is False

    def test_parse_fda_calendar_filters_by_ticker(self):
        payload = {
            "data": [
                {"ticker": "AAA", "drug_name": "x", "event_type": "PDUFA", "date": "2026-01-01"},
                {"ticker": "BBB", "drug_name": "y", "event_type": "ADCOM", "date": "2026-02-01"},
            ]
        }
        rows = parse_fda_calendar(payload, "bbb")
        assert len(rows) == 1
        assert rows[0].ticker == "BBB"

    def test_parse_sector_etfs_finds_match(self):
        payload = {
            "data": [
                {
                    "sector": "Technology",
                    "ticker": "XLK",
                    "performance_1d": 0.01,
                    "performance_5d": 0.02,
                    "performance_1m": 0.03,
                }
            ]
        }
        etf = parse_sector_etfs(payload, "technology")
        assert etf is not None
        assert etf.ticker == "XLK"

    def test_parse_sector_etfs_no_match(self):
        payload = {"data": [{"sector": "Energy", "ticker": "XLE", "performance_1d": 0.0}]}
        assert parse_sector_etfs(payload, "technology") is None

    def test_high_impact_event_keywords(self):
        assert "fomc" in HIGH_IMPACT_EVENT_KEYWORDS
        assert "jackson hole" in HIGH_IMPACT_EVENT_KEYWORDS


def test_sector_analyst_result_dataclass():
    r = SectorAnalystResult(
        score=50,
        rationale="x",
        signals=[],
        skipped=False,
        skip_reason=None,
        has_fda_flag=False,
        component_scores={},
    )
    assert r.score == 50
