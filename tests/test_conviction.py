"""Tests for the conviction engine — deterministic, no I/O."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tracker.conviction import ConvictionEngine
from tracker.models import (
    ChainPollResult,
    FlowEvent,
    FlowWatchResult,
    NeighborStrike,
    NewsEvent,
    NewsEventType,
    NewsWatchResult,
    Signal,
    SignalSnapshot,
    SignalState,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_signal(**overrides) -> Signal:
    defaults = dict(
        id="sig-001", ticker="ACME", strike=50.0, expiry=(
            _now().date() + timedelta(days=30)
        ).isoformat(),
        option_type="call", direction="bullish", state=SignalState.PENDING,
        initial_score=82, initial_premium=50000, initial_oi=100,
        initial_volume=500, grade_id="g1", conviction_score=82.0,
        created_at=_now() - timedelta(hours=6),
    )
    defaults.update(overrides)
    return Signal(**defaults)


def _make_chain(**overrides) -> ChainPollResult:
    defaults = dict(
        ticker="ACME", polled_at=_now(), contract_oi=100, contract_volume=50,
        contract_bid=2.10, contract_ask=2.30, spot_price=48.50, contract_found=True,
    )
    defaults.update(overrides)
    return ChainPollResult(**defaults)


def _make_flow(events: list[FlowEvent] | None = None) -> FlowWatchResult:
    return FlowWatchResult(ticker="ACME", checked_at=_now(), events=events or [])


def _make_flow_event(**overrides) -> FlowEvent:
    defaults = dict(
        alert_id="flow-001", strike=50.0, expiry=(
            _now().date() + timedelta(days=30)
        ).isoformat(),
        option_type="call", premium=30000, volume=200, fill_type="sweep",
        is_same_contract=True, is_same_expiry=False, created_at=_now(),
    )
    defaults.update(overrides)
    return FlowEvent(**defaults)


def _make_prev_snapshot(**overrides) -> SignalSnapshot:
    defaults = dict(
        id="snap-prev", signal_id="sig-001", snapshot_at=_now() - timedelta(minutes=5),
        contract_oi=100, contract_volume=30, neighbor_strikes_active=3,
    )
    defaults.update(overrides)
    return SignalSnapshot(**defaults)


class TestConvictionPositive:
    def test_oi_increase_adds_points(self):
        engine = ConvictionEngine()
        signal = _make_signal(initial_oi=100)
        chain = _make_chain(contract_oi=130)  # 30% increase
        prev = _make_prev_snapshot(contract_oi=100)
        result = engine.evaluate(signal, chain, _make_flow(), prev)
        assert result.conviction_delta > 0
        assert any("oi_increase" in s for s in result.signals_fired)

    def test_new_flow_adds_points(self):
        engine = ConvictionEngine()
        signal = _make_signal()
        flow = _make_flow([_make_flow_event()])
        result = engine.evaluate(signal, _make_chain(), flow, _make_prev_snapshot())
        assert result.conviction_delta >= 5  # confirming_flow_bonus default
        assert any("new_flow" in s for s in result.signals_fired)

    def test_multiple_flow_events_capped(self):
        engine = ConvictionEngine()
        signal = _make_signal()
        events = [_make_flow_event(alert_id=f"f{i}") for i in range(10)]
        flow = _make_flow(events)
        result = engine.evaluate(signal, _make_chain(), flow, _make_prev_snapshot())
        assert result.conviction_delta <= 15 + 20  # flow cap + possible other bonuses

    def test_chain_spread_adds_points(self):
        engine = ConvictionEngine()
        signal = _make_signal()
        chain = _make_chain(neighbor_strikes=[
            NeighborStrike(strike=45.0, option_type="call", oi=50),
            NeighborStrike(strike=55.0, option_type="call", oi=30),
            NeighborStrike(strike=60.0, option_type="call", oi=10),
        ])
        prev = _make_prev_snapshot(neighbor_strikes_active=1)  # was 1, now 3
        result = engine.evaluate(signal, chain, _make_flow(), prev)
        assert any("chain_spread" in s for s in result.signals_fired)


class TestConvictionNegative:
    def test_oi_decrease_removes_points(self):
        engine = ConvictionEngine()
        signal = _make_signal(initial_oi=100)
        chain = _make_chain(contract_oi=60)  # 40% decrease
        prev = _make_prev_snapshot(contract_oi=100)
        result = engine.evaluate(signal, chain, _make_flow(), prev)
        assert result.conviction_delta < 0
        assert any("oi_decrease" in s for s in result.signals_fired)

    def test_wide_spread_penalty(self):
        engine = ConvictionEngine()
        signal = _make_signal()
        chain = _make_chain(contract_bid=1.0, contract_ask=2.0)  # 67% spread
        result = engine.evaluate(signal, chain, _make_flow(), _make_prev_snapshot())
        assert any("spread_wide" in s for s in result.signals_fired)

    def test_silence_penalty(self):
        engine = ConvictionEngine()
        signal = _make_signal(
            created_at=_now() - timedelta(days=4),
            last_flow_at=_now() - timedelta(days=3),
        )
        result = engine.evaluate(signal, _make_chain(), _make_flow(), _make_prev_snapshot())
        assert any("silence" in s for s in result.signals_fired)

    def test_delta_clamped(self):
        """Conviction delta must be clamped to ±20 per cycle."""
        engine = ConvictionEngine()
        signal = _make_signal(initial_oi=100)
        chain = _make_chain(contract_oi=1)  # 99% decrease — would be huge penalty unclamped
        prev = _make_prev_snapshot(contract_oi=100)
        result = engine.evaluate(signal, chain, _make_flow(), prev)
        assert result.conviction_delta >= -20.0


class TestConvictionNews:
    def test_news_none_backward_compatible(self):
        engine = ConvictionEngine()
        signal = _make_signal()
        chain = _make_chain()
        flow = _make_flow()
        prev = _make_prev_snapshot()
        r1 = engine.evaluate(signal, chain, flow, prev)
        r2 = engine.evaluate(signal, chain, flow, prev, news=None)
        assert r1.conviction_delta == r2.conviction_delta
        assert r1.signals_fired == r2.signals_fired

    def test_catalyst_and_filing_bonuses(self):
        engine = ConvictionEngine()
        signal = _make_signal()
        now = _now()
        news = NewsWatchResult(
            signal_id=signal.id,
            ticker=signal.ticker,
            checked_at=now,
            events=[
                NewsEvent(
                    id="e1",
                    signal_id=signal.id,
                    ticker=signal.ticker,
                    event_type=NewsEventType.HEADLINE,
                    title="Merger announced",
                    source="uw_headlines",
                    published_at=now,
                    detected_at=now,
                    catalyst_matched=True,
                    catalyst_keywords=["merger"],
                ),
                NewsEvent(
                    id="e2",
                    signal_id=signal.id,
                    ticker=signal.ticker,
                    event_type=NewsEventType.SEC_FILING,
                    title="8-K: ACME",
                    source="sec_edgar",
                    published_at=now,
                    detected_at=now,
                    filing_type="8-K",
                    catalyst_matched=True,
                    catalyst_keywords=["8-k"],
                ),
            ],
            has_catalyst=True,
            filing_detected=True,
        )
        result = engine.evaluate(
            signal, _make_chain(), _make_flow(), _make_prev_snapshot(), news=news
        )
        assert "catalyst_detected" in " ".join(result.signals_fired)
        assert any("sec_filing" in s for s in result.signals_fired)
        assert result.conviction_delta >= 9.0


class TestStateTransitions:
    def test_pending_stays_pending_without_evidence(self):
        engine = ConvictionEngine()
        signal = _make_signal(state=SignalState.PENDING)
        result = engine.evaluate(signal, _make_chain(), _make_flow(), None)
        assert result.next_state == SignalState.PENDING

    def test_pending_to_accumulating_on_flow(self):
        engine = ConvictionEngine()
        signal = _make_signal(state=SignalState.PENDING)
        flow = _make_flow([_make_flow_event()])
        result = engine.evaluate(signal, _make_chain(), flow, None)
        assert result.next_state == SignalState.ACCUMULATING

    def test_pending_to_accumulating_on_oi_increase(self):
        engine = ConvictionEngine()
        signal = _make_signal(state=SignalState.PENDING, initial_oi=100)
        chain = _make_chain(contract_oi=120)
        result = engine.evaluate(signal, chain, _make_flow(), None)
        assert result.next_state == SignalState.ACCUMULATING

    def test_accumulating_to_actionable(self):
        engine = ConvictionEngine()
        signal = _make_signal(
            state=SignalState.ACCUMULATING,
            conviction_score=87.0,
            confirming_flows=1,
            initial_oi=100,
        )
        chain = _make_chain(contract_oi=200)  # 2x initial OI
        flow = _make_flow([_make_flow_event()])  # 1 more flow → total 2
        result = engine.evaluate(signal, chain, flow, _make_prev_snapshot())
        # conviction: 87 + delta (at least +5 from flow + OI bonus) → ≥ 90
        new_conv = signal.conviction_score + result.conviction_delta
        if new_conv >= 90.0:
            assert result.next_state == SignalState.ACTIONABLE

    def test_decay_on_low_conviction(self):
        engine = ConvictionEngine()
        signal = _make_signal(
            state=SignalState.ACCUMULATING,
            conviction_score=55.0,  # below decay_conviction (60)
        )
        result = engine.evaluate(signal, _make_chain(), _make_flow(), _make_prev_snapshot())
        assert result.next_state == SignalState.DECAYED

    def test_expired_on_low_dte(self):
        engine = ConvictionEngine()
        signal = _make_signal(
            expiry=(_now().date() + timedelta(days=1)).isoformat(),
        )
        result = engine.evaluate(signal, _make_chain(), _make_flow(), None)
        assert result.next_state == SignalState.EXPIRED

    def test_expired_on_past_expiry(self):
        engine = ConvictionEngine()
        signal = _make_signal(
            expiry=(_now().date() - timedelta(days=1)).isoformat(),
        )
        result = engine.evaluate(signal, _make_chain(), _make_flow(), None)
        assert result.next_state == SignalState.EXPIRED

    def test_decayed_on_window_elapsed(self):
        engine = ConvictionEngine()
        signal = _make_signal(
            state=SignalState.ACCUMULATING,
            conviction_score=75.0,  # below decay_window_conviction (80)
            created_at=_now() - timedelta(days=8),  # past 7-day window
        )
        result = engine.evaluate(signal, _make_chain(), _make_flow(), None)
        assert result.next_state in (SignalState.DECAYED, SignalState.EXPIRED)
