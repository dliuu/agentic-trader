"""Portfolio guardrail checker — validates ACTIONABLE signals before executor queue."""

from __future__ import annotations

from dataclasses import dataclass

from grader.models import TradeRiskParams
from tracker.models import ChainPollResult, Signal, SignalState
from tracker.portfolio_config import PortfolioConfig
from tracker.signal_store import SignalStore


@dataclass
class GuardrailViolation:
    rule: str
    limit: float | int
    actual: float | int
    message: str


@dataclass
class PositionSizing:
    raw_size_multiplier: float
    dollar_size: float
    max_loss_usd: float
    contracts: int


async def check_guardrails(
    signal: Signal,
    chain: ChainPollResult,
    portfolio_config: PortfolioConfig,
    store: SignalStore,
) -> GuardrailViolation | None:
    cfg = portfolio_config

    if chain.contract_bid is not None and chain.contract_ask is not None:
        mid = (chain.contract_bid + chain.contract_ask) / 2
        if mid > 0:
            spread_pct = (chain.contract_ask - chain.contract_bid) / mid * 100
            if spread_pct > cfg.max_bid_ask_spread_pct:
                return GuardrailViolation(
                    rule="max_bid_ask_spread",
                    limit=cfg.max_bid_ask_spread_pct,
                    actual=round(spread_pct, 1),
                    message=(
                        f"Spread {spread_pct:.1f}% exceeds limit {cfg.max_bid_ask_spread_pct}%"
                    ),
                )

    if chain.contract_volume is not None and chain.contract_volume < cfg.min_option_volume:
        return GuardrailViolation(
            rule="min_option_volume",
            limit=cfg.min_option_volume,
            actual=chain.contract_volume,
            message=(
                f"Contract volume {chain.contract_volume} below minimum {cfg.min_option_volume}"
            ),
        )

    actionable_signals = await store.get_signals_by_state(SignalState.ACTIONABLE)
    actionable_count = len(actionable_signals)
    if actionable_count > cfg.max_concurrent_positions:
        return GuardrailViolation(
            rule="max_concurrent_positions",
            limit=cfg.max_concurrent_positions,
            actual=actionable_count,
            message=(
                f"{actionable_count} concurrent actionable positions, "
                f"limit is {cfg.max_concurrent_positions}"
            ),
        )

    same_ticker_peers = sum(
        1
        for s in actionable_signals
        if s.ticker.upper() == signal.ticker.upper() and s.id != signal.id
    )
    if same_ticker_peers >= cfg.max_signals_per_ticker:
        return GuardrailViolation(
            rule="max_signals_per_ticker",
            limit=cfg.max_signals_per_ticker,
            actual=same_ticker_peers + 1,
            message=(
                f"Ticker {signal.ticker} already has {same_ticker_peers} other actionable signal(s)"
            ),
        )

    # Sector concentration: requires sector on Signal or live lookup — not in pilot scope.

    position = compute_position_size(signal, chain, cfg)

    others = [s for s in actionable_signals if s.id != signal.id]
    total_exposure_proxy = sum(s.cumulative_premium for s in others)
    new_total = total_exposure_proxy + position.dollar_size
    if new_total > cfg.max_total_exposure_effective:
        return GuardrailViolation(
            rule="max_total_exposure",
            limit=round(cfg.max_total_exposure_effective, 2),
            actual=round(new_total, 2),
            message=(
                f"Total exposure ${new_total:,.0f} would exceed limit "
                f"${cfg.max_total_exposure_effective:,.0f}"
            ),
        )

    if position.dollar_size > cfg.max_single_position_effective:
        return GuardrailViolation(
            rule="max_single_position",
            limit=round(cfg.max_single_position_effective, 2),
            actual=round(position.dollar_size, 2),
            message=(
                f"Position size ${position.dollar_size:,.0f} exceeds limit "
                f"${cfg.max_single_position_effective:,.0f}"
            ),
        )

    max_loss = cfg.max_total_capital_usd * cfg.max_single_loss_pct / 100.0
    if position.max_loss_usd > max_loss:
        return GuardrailViolation(
            rule="max_single_loss",
            limit=round(max_loss, 2),
            actual=round(position.max_loss_usd, 2),
            message=(
                f"Max loss ${position.max_loss_usd:,.0f} exceeds limit ${max_loss:,.0f}"
            ),
        )

    # daily_loss_limit_pct: requires realized P&L / broker fills — not enforced in pilot.
    return None


def compute_position_size(
    signal: Signal,
    chain: ChainPollResult,
    cfg: PortfolioConfig,
) -> PositionSizing:
    raw_size = 0.5
    stop_loss_pct = 50.0

    if signal.risk_params_json:
        try:
            risk = TradeRiskParams.model_validate_json(signal.risk_params_json)
            raw_size = risk.recommended_position_size
            stop_loss_pct = risk.recommended_stop_loss_pct or 50.0
        except Exception:
            pass

    dollar_size = raw_size * cfg.max_total_capital_usd
    dollar_size = min(dollar_size, cfg.max_single_position_usd)
    dollar_size = min(
        dollar_size,
        cfg.max_total_capital_usd * cfg.max_single_position_pct / 100.0,
    )

    max_loss = dollar_size * stop_loss_pct / 100.0

    contracts = 0
    if chain.contract_ask and chain.contract_ask > 0:
        contract_cost = chain.contract_ask * 100
        if contract_cost > 0:
            contracts = int(dollar_size / contract_cost)

    return PositionSizing(
        raw_size_multiplier=raw_size,
        dollar_size=dollar_size,
        max_loss_usd=max_loss,
        contracts=max(contracts, 0),
    )
