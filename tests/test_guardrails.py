"""Tests for portfolio guardrail checker."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tracker.guardrails import check_guardrails, compute_position_size
from tracker.models import ChainPollResult, Signal, SignalState
from tracker.portfolio_config import PortfolioConfig


def test_position_size_respects_dollar_cap():
    cfg = PortfolioConfig(
        max_total_capital_usd=100_000,
        max_single_position_usd=2_500,
        max_single_position_pct=10.0,
    )
    signal = _make_signal(
        risk_params_json='{"recommended_position_size": 0.5, "recommended_stop_loss_pct": 30.0, "max_entry_spread_pct": 10.0}'
    )
    chain = _make_chain(contract_ask=5.0)
    result = compute_position_size(signal, chain, cfg)
    assert result.dollar_size == 2_500


def test_position_size_respects_pct_cap():
    cfg = PortfolioConfig(
        max_total_capital_usd=50_000,
        max_single_position_usd=10_000,
        max_single_position_pct=5.0,
    )
    signal = _make_signal(
        risk_params_json='{"recommended_position_size": 0.5, "recommended_stop_loss_pct": 30.0, "max_entry_spread_pct": 10.0}'
    )
    chain = _make_chain(contract_ask=5.0)
    result = compute_position_size(signal, chain, cfg)
    assert result.dollar_size == 2_500


def test_position_size_no_risk_params():
    cfg = PortfolioConfig(max_total_capital_usd=50_000)
    signal = _make_signal(risk_params_json=None)
    chain = _make_chain(contract_ask=2.0)
    result = compute_position_size(signal, chain, cfg)
    assert result.raw_size_multiplier == 0.5


@pytest.mark.asyncio
async def test_spread_too_wide():
    cfg = PortfolioConfig(max_bid_ask_spread_pct=15.0)
    signal = _make_signal()
    chain = _make_chain(contract_bid=1.0, contract_ask=2.0)
    store = MockSignalStore([])

    violation = await check_guardrails(signal, chain, cfg, store)
    assert violation is not None
    assert violation.rule == "max_bid_ask_spread"


@pytest.mark.asyncio
async def test_volume_too_low():
    cfg = PortfolioConfig(min_option_volume=50)
    signal = _make_signal()
    chain = _make_chain(contract_volume=10)
    store = MockSignalStore([])

    violation = await check_guardrails(signal, chain, cfg, store)
    assert violation is not None
    assert violation.rule == "min_option_volume"


@pytest.mark.asyncio
async def test_max_concurrent_positions():
    cfg = PortfolioConfig(max_concurrent_positions=2)
    signal = _make_signal(id="new-sig")
    chain = _make_chain()
    existing = [
        _make_signal(id="a", ticker="AAA"),
        _make_signal(id="b", ticker="BBB"),
        _make_signal(id="c", ticker="CCC"),
    ]
    store = MockSignalStore(existing)

    violation = await check_guardrails(signal, chain, cfg, store)
    assert violation is not None
    assert violation.rule == "max_concurrent_positions"


@pytest.mark.asyncio
async def test_all_guardrails_pass():
    cfg = PortfolioConfig(
        max_total_capital_usd=100_000,
        max_single_position_usd=5_000,
        max_concurrent_positions=10,
        min_option_volume=10,
        max_bid_ask_spread_pct=20.0,
    )
    signal = _make_signal()
    chain = _make_chain(
        contract_bid=2.0,
        contract_ask=2.20,
        contract_volume=100,
    )
    store = MockSignalStore([])

    violation = await check_guardrails(signal, chain, cfg, store)
    assert violation is None


def _make_signal(
    ticker: str = "TEST",
    id: str = "test-signal-1",
    risk_params_json: str | None = (
        '{"recommended_position_size": 0.3, "recommended_stop_loss_pct": 30.0, '
        '"max_entry_spread_pct": 10.0}'
    ),
    **kwargs,
) -> Signal:
    defaults = dict(
        id=id,
        ticker=ticker,
        strike=50.0,
        expiry="2030-06-20",
        option_type="call",
        direction="bullish",
        state=SignalState.ACTIONABLE,
        initial_score=85,
        initial_premium=50_000.0,
        initial_oi=500,
        initial_volume=200,
        grade_id="test-grade-1",
        conviction_score=92.0,
        cumulative_premium=75_000.0,
        created_at=datetime.now(timezone.utc),
        risk_params_json=risk_params_json,
    )
    defaults.update(kwargs)
    return Signal(**defaults)


def _make_chain(
    contract_bid: float | None = 2.0,
    contract_ask: float | None = 2.20,
    contract_volume: int | None = 200,
    contract_oi: int | None = 500,
    spot_price: float | None = 48.0,
) -> ChainPollResult:
    return ChainPollResult(
        ticker="TEST",
        polled_at=datetime.now(timezone.utc),
        contract_oi=contract_oi,
        contract_volume=contract_volume,
        contract_bid=contract_bid,
        contract_ask=contract_ask,
        spot_price=spot_price,
        neighbor_strikes=[],
        adjacent_expiry_oi=[],
    )


class MockSignalStore:
    def __init__(self, actionable_signals: list[Signal]):
        self._actionable = actionable_signals

    async def get_signals_by_state(self, state):
        if state == SignalState.ACTIONABLE:
            return self._actionable
        return []
