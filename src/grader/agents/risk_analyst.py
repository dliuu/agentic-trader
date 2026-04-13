"""
Risk Analyst (Agent 3) - Conviction scorer based on structural risk taken.

Measures how much structural cost the original option buyer voluntarily
accepted and interprets that as a conviction signal. A buyer who absorbs
wide spreads, short DTE, deep OTM strikes, and illiquidity is signaling
high conviction - they believe their informational edge outweighs the
structural disadvantage.

This agent is fully deterministic. It makes 3 UW API calls:
  1. /api/stock/{ticker}/option-chains   -> bid, ask, volume, OI, greeks
  2. /api/stock/{ticker}/volatility/stats -> realized vol for move ratio
  3. /api/earnings/{ticker}              -> next earnings date

All scoring thresholds live in shared.filters.RiskConvictionConfig.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from shared.filters import RiskConvictionConfig
from shared.models import FillType, FlowCandidate, OptionType, RiskConvictionScore


def _tier_lookup(
    value: float | int,
    tiers: tuple,
    points: tuple,
    ascending: bool = True,
) -> int:
    """Map a value to a point score using threshold tiers."""
    if ascending:
        for i, threshold in enumerate(tiers):
            if value <= threshold:
                return points[i]
        return points[-1]
    for i, threshold in enumerate(tiers):
        if value <= threshold:
            return points[i]
    return points[-1]


def _score_premium_commitment(
    premium: float,
    cfg: RiskConvictionConfig,
) -> tuple[int, str | None]:
    pts = _tier_lookup(premium, cfg.premium_tiers, cfg.premium_points)
    signal = None
    if premium >= cfg.premium_tiers[-1]:
        signal = f"${premium:,.0f} premium - extreme capital commitment"
    elif premium >= cfg.premium_tiers[-2]:
        signal = f"${premium:,.0f} premium - serious capital at risk"
    return pts, signal


def _score_time_pressure(
    dte: int,
    cfg: RiskConvictionConfig,
) -> tuple[int, str | None]:
    pts = _tier_lookup(dte, cfg.dte_tiers, cfg.dte_points)
    signal = None
    if dte <= cfg.dte_tiers[0]:
        signal = f"{dte}-DTE - maximum time pressure, expects move within days"
    elif dte <= cfg.dte_tiers[1]:
        signal = f"{dte}-DTE - high theta burn accepted, near-term catalyst expected"
    return pts, signal


def _score_spread_cost(
    spread_pct: float,
    cfg: RiskConvictionConfig,
) -> tuple[int, str | None]:
    pts = _tier_lookup(spread_pct, cfg.spread_tiers, cfg.spread_points)
    signal = None
    if spread_pct > cfg.spread_tiers[-1]:
        signal = f"{spread_pct:.1f}% spread absorbed - extreme urgency to enter"
    elif spread_pct > cfg.spread_tiers[-2]:
        signal = f"{spread_pct:.1f}% spread absorbed - paying heavy entry tax"
    return pts, signal


def _fill_aggression_bonus(
    fill_type: str,
    cfg: RiskConvictionConfig,
) -> tuple[int, str | None]:
    fill_lower = fill_type.lower() if fill_type else ""
    if "sweep" in fill_lower:
        return cfg.sweep_bonus, "swept across exchanges - immediate execution needed"
    if "block" in fill_lower:
        return cfg.block_bonus, None
    return cfg.split_bonus, None


def _score_strike_distance(
    strike: float,
    spot_price: float,
    option_type: str,
    cfg: RiskConvictionConfig,
) -> tuple[int, float, str | None]:
    if spot_price <= 0:
        return 0, 0.0, None

    if option_type.lower() in ("call", "c"):
        otm_pct = max(0.0, (strike - spot_price) / spot_price * 100)
        is_itm = strike < spot_price
    else:
        otm_pct = max(0.0, (spot_price - strike) / spot_price * 100)
        is_itm = strike > spot_price

    if is_itm:
        return cfg.itm_points, -otm_pct, "ITM - conservative positioning"

    pts = _tier_lookup(otm_pct, cfg.otm_tiers, cfg.otm_points)
    signal = None
    if otm_pct > cfg.otm_tiers[-1]:
        signal = f"{otm_pct:.1f}% OTM - deep out-of-money, highly specific target"
    elif otm_pct > cfg.otm_tiers[-2]:
        signal = f"{otm_pct:.1f}% OTM - significant move needed"
    return pts, otm_pct, signal


def _score_move_ratio(
    strike: float,
    spot_price: float,
    option_type: str,
    dte: int,
    annualized_realized_vol: float | None,
    cfg: RiskConvictionConfig,
) -> tuple[int, float | None, str | None]:
    if spot_price <= 0 or dte <= 0 or annualized_realized_vol is None:
        return 0, None, None

    if option_type.lower() in ("call", "c"):
        required_move_pct = max(0.0, (strike - spot_price) / spot_price * 100)
    else:
        required_move_pct = max(0.0, (spot_price - strike) / spot_price * 100)

    if required_move_pct == 0.0:
        return cfg.move_ratio_points[0], 0.0, None

    implied_daily_move = required_move_pct / math.sqrt(dte)
    realized_daily_vol = annualized_realized_vol / math.sqrt(252)

    if realized_daily_vol <= 0:
        return 0, None, None

    move_ratio = implied_daily_move / realized_daily_vol
    pts = _tier_lookup(move_ratio, cfg.move_ratio_tiers, cfg.move_ratio_points)
    signal = None
    if move_ratio > cfg.move_ratio_tiers[-1]:
        signal = f"needs {move_ratio:.1f}sigma daily move - tail event conviction"
    elif move_ratio > cfg.move_ratio_tiers[-2]:
        signal = f"needs {move_ratio:.1f}sigma daily move - above-average move required"
    return pts, move_ratio, signal


def _score_liquidity_cost(
    contract_volume: int,
    cfg: RiskConvictionConfig,
) -> tuple[int, str | None]:
    pts = _tier_lookup(contract_volume, cfg.volume_tiers, cfg.volume_points)
    signal = None
    if contract_volume < cfg.volume_tiers[0]:
        signal = f"only {contract_volume} contracts traded - extreme precision in strike/expiry"
    elif contract_volume < cfg.volume_tiers[1]:
        signal = f"{contract_volume} daily volume - accepted thin liquidity"
    return pts, signal


def _score_earnings_modifier(
    dte: int,
    days_to_earnings: int | None,
    cfg: RiskConvictionConfig,
) -> tuple[int, str | None]:
    if days_to_earnings is None or days_to_earnings < 0:
        return 0, None
    if days_to_earnings > dte:
        return 0, None

    if dte <= cfg.earnings_tight_bracket_days or (
        dte - days_to_earnings <= cfg.earnings_tight_bracket_days
    ):
        return cfg.earnings_tight_modifier, (
            f"earnings in {days_to_earnings}d with {dte}-DTE - tightly targeting earnings event"
        )

    if dte <= cfg.earnings_event_window_days:
        return cfg.earnings_window_modifier, (
            f"earnings in {days_to_earnings}d before {dte}-DTE expiry - catalyst conviction"
        )

    return 0, None


def _derive_position_size(score: int, cfg: RiskConvictionConfig) -> float:
    return _tier_lookup_float(score, cfg.size_tiers, cfg.size_multipliers)


def _derive_stop_loss(score: int, cfg: RiskConvictionConfig) -> float:
    return _tier_lookup_float(score, cfg.stop_tiers, cfg.stop_pcts)


def _derive_max_entry_spread(
    spread_pct: float | None,
    cfg: RiskConvictionConfig,
) -> float:
    if spread_pct is None or spread_pct <= 0:
        return cfg.max_entry_spread_cap
    observed = spread_pct / 100.0
    return min(observed * cfg.entry_spread_discount, cfg.max_entry_spread_cap)


def _tier_lookup_float(
    value: float | int,
    tiers: tuple,
    results: tuple,
) -> float:
    for i, threshold in enumerate(tiers):
        if value < threshold:
            return results[i]
    return results[-1]


def extract_option_chain_data(
    option_chains_response: dict | None,
    candidate: FlowCandidate,
) -> dict[str, Any]:
    result = {
        "bid": None,
        "ask": None,
        "mid": None,
        "spread_pct": None,
        "contract_volume": None,
        "open_interest": None,
        "delta": None,
        "theta": None,
        "gamma": None,
        "vega": None,
        "iv": None,
    }

    if not option_chains_response:
        return result

    chains = option_chains_response.get("data", [])
    if not chains:
        chains = option_chains_response if isinstance(option_chains_response, list) else []

    target_expiry = candidate.expiry.strftime("%Y-%m-%d")
    target_type = (
        candidate.option_type.value
        if isinstance(candidate.option_type, OptionType)
        else str(candidate.option_type).lower()
    )
    target_type = "call" if target_type in ("c", "call") else "put"

    for contract in chains:
        contract_strike = float(contract.get("strike", 0))
        contract_expiry = str(contract.get("expiry", ""))[:10]
        contract_type = str(
            contract.get("option_type", contract.get("type", ""))
        ).lower()
        if (
            abs(contract_strike - candidate.strike) < 0.01
            and contract_expiry == target_expiry
            and contract_type == target_type
        ):
            bid = _safe_float(contract.get("bid"))
            ask = _safe_float(contract.get("ask"))
            mid = None
            spread_pct = None
            if bid is not None and ask is not None:
                mid = (bid + ask) / 2.0
                if mid > 0:
                    spread_pct = (ask - bid) / mid * 100.0

            result["bid"] = bid
            result["ask"] = ask
            result["mid"] = mid
            result["spread_pct"] = spread_pct
            result["contract_volume"] = _safe_int(contract.get("volume"))
            result["open_interest"] = _safe_int(contract.get("open_interest"))
            result["delta"] = _safe_float(contract.get("delta"))
            result["theta"] = _safe_float(contract.get("theta"))
            result["gamma"] = _safe_float(contract.get("gamma"))
            result["vega"] = _safe_float(contract.get("vega"))
            result["iv"] = _safe_float(
                contract.get("implied_volatility", contract.get("iv"))
            )
            break

    return result


def extract_realized_vol(vol_stats_response: dict | None) -> float | None:
    if not vol_stats_response:
        return None
    data = vol_stats_response.get("data", vol_stats_response)
    if isinstance(data, list) and len(data) > 0:
        data = data[0]
    if not isinstance(data, dict):
        return None
    for key in (
        "realized_volatility",
        "rv",
        "realized_vol",
        "hv_20",
        "hv20",
        "historical_volatility",
    ):
        val = _safe_float(data.get(key))
        if val is not None and val > 0:
            return val
    return None


def _next_upcoming_earnings(
    earnings_response: dict | None,
    as_of: datetime | None = None,
) -> tuple[int | None, datetime | None]:
    """First upcoming earnings in API list order (same semantics as historical extract_days_to_earnings)."""
    if not earnings_response:
        return None, None
    now = as_of or datetime.now(timezone.utc)
    data = earnings_response.get("data", earnings_response)
    entries = data if isinstance(data, list) else [data]
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        date_str = entry.get("date") or entry.get("earnings_date")
        if not date_str:
            continue
        try:
            earnings_date = datetime.fromisoformat(str(date_str).replace("Z", "+00:00"))
            if earnings_date.tzinfo is None:
                earnings_date = earnings_date.replace(tzinfo=timezone.utc)
            delta = (earnings_date - now).days
            if delta >= 0:
                return delta, earnings_date
        except (ValueError, TypeError):
            continue
    return None, None


def extract_days_to_earnings(
    earnings_response: dict | None,
    as_of: datetime | None = None,
) -> int | None:
    d, _ = _next_upcoming_earnings(earnings_response, as_of)
    return d


def extract_next_earnings_datetime(
    earnings_response: dict | None,
    as_of: datetime | None = None,
) -> datetime | None:
    """UTC datetime of the same next earnings used by extract_days_to_earnings (for Gate 1.5 expiry math)."""
    _, dt = _next_upcoming_earnings(earnings_response, as_of)
    return dt


def _safe_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        f = float(val)
        return f if math.isfinite(f) else None
    except (ValueError, TypeError):
        return None


def _safe_int(val: Any) -> int | None:
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def score_risk_conviction(
    candidate: FlowCandidate,
    option_chain_data: dict[str, Any],
    annualized_realized_vol: float | None,
    days_to_earnings: int | None,
    cfg: RiskConvictionConfig | None = None,
) -> RiskConvictionScore:
    if cfg is None:
        cfg = RiskConvictionConfig()

    signals: list[str] = []
    data_gaps: list[str] = []

    now = datetime.now(timezone.utc)
    expiry = candidate.expiry
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    dte = max(0, (expiry - now).days)

    premium_pts, premium_signal = _score_premium_commitment(candidate.premium, cfg)
    if premium_signal:
        signals.append(premium_signal)

    time_pts, time_signal = _score_time_pressure(dte, cfg)
    if time_signal:
        signals.append(time_signal)

    spread_pct = option_chain_data.get("spread_pct")
    if spread_pct is not None:
        spread_pts, spread_signal = _score_spread_cost(spread_pct, cfg)
        if spread_signal:
            signals.append(spread_signal)
    else:
        spread_pts = 0
        data_gaps.append("option_chain_spread")

    fill_type_str = (
        candidate.fill_type.value
        if isinstance(candidate.fill_type, FillType)
        else str(candidate.fill_type)
    )
    aggression_pts, aggression_signal = _fill_aggression_bonus(fill_type_str, cfg)
    if aggression_signal:
        signals.append(aggression_signal)

    option_type_str = (
        candidate.option_type.value
        if isinstance(candidate.option_type, OptionType)
        else str(candidate.option_type)
    )
    strike_pts, otm_pct, strike_signal = _score_strike_distance(
        candidate.strike, candidate.spot_price, option_type_str, cfg
    )
    if strike_signal:
        signals.append(strike_signal)

    move_pts, move_ratio, move_signal = _score_move_ratio(
        candidate.strike,
        candidate.spot_price,
        option_type_str,
        dte,
        annualized_realized_vol,
        cfg,
    )
    if move_signal:
        signals.append(move_signal)
    if annualized_realized_vol is None:
        data_gaps.append("realized_volatility")

    contract_volume = option_chain_data.get("contract_volume")
    if contract_volume is not None:
        liquidity_pts, liquidity_signal = _score_liquidity_cost(contract_volume, cfg)
        if liquidity_signal:
            signals.append(liquidity_signal)
    elif candidate.volume > 0:
        liquidity_pts, liquidity_signal = _score_liquidity_cost(candidate.volume, cfg)
        if liquidity_signal:
            signals.append(liquidity_signal)
    else:
        liquidity_pts = 0
        data_gaps.append("contract_volume")

    earnings_pts, earnings_signal = _score_earnings_modifier(dte, days_to_earnings, cfg)
    if earnings_signal:
        signals.append(earnings_signal)
    if days_to_earnings is None:
        data_gaps.append("earnings_date")

    raw_score = (
        cfg.baseline_score
        + premium_pts
        + time_pts
        + spread_pts
        + aggression_pts
        + strike_pts
        + move_pts
        + liquidity_pts
        + earnings_pts
    )
    final_score = max(1, min(100, raw_score))

    theta_daily_pct = None
    theta = option_chain_data.get("theta")
    mid = option_chain_data.get("mid")
    if theta is not None and mid is not None and mid > 0:
        theta_daily_pct = abs(theta) / mid * 100.0

    position_size = _derive_position_size(final_score, cfg)
    stop_loss = _derive_stop_loss(final_score, cfg)
    max_entry_spread = _derive_max_entry_spread(spread_pct, cfg)

    untradeable = (
        spread_pct is None
        and contract_volume is None
        and annualized_realized_vol is None
    )

    rationale_parts = [
        f"Conviction score {final_score}/100 (baseline {cfg.baseline_score}).",
        f"Premium: {premium_pts:+d} (${candidate.premium:,.0f})",
        f"Time pressure: {time_pts:+d} ({dte} DTE)",
        f"Spread cost: {spread_pts:+d}"
        + (f" ({spread_pct:.1f}%)" if spread_pct is not None else " (no data)"),
        f"Fill aggression: {aggression_pts:+d} ({fill_type_str})",
        f"Strike distance: {strike_pts:+d}"
        + (f" ({otm_pct:+.1f}% OTM)" if otm_pct is not None else ""),
        f"Move ratio: {move_pts:+d}"
        + (f" ({move_ratio:.2f}sigma)" if move_ratio is not None else " (no data)"),
        f"Liquidity: {liquidity_pts:+d}"
        + (f" ({contract_volume} vol)" if contract_volume is not None else ""),
        f"Earnings: {earnings_pts:+d}",
    ]
    rationale = " | ".join(rationale_parts)

    return RiskConvictionScore(
        agent="risk_analyst",
        score=final_score,
        rationale=rationale,
        signals=signals,
        skipped=False,
        skip_reason=None,
        premium_commitment_points=premium_pts,
        time_pressure_points=time_pts,
        spread_cost_points=spread_pts,
        fill_aggression_points=aggression_pts,
        strike_distance_points=strike_pts,
        move_ratio_points=move_pts,
        liquidity_cost_points=liquidity_pts,
        earnings_modifier=earnings_pts,
        spread_pct=spread_pct,
        otm_pct=otm_pct,
        move_ratio=move_ratio,
        theta_daily_pct=theta_daily_pct,
        days_to_expiry=dte,
        conviction_signals=signals,
        recommended_position_size=position_size,
        recommended_stop_loss_pct=stop_loss,
        max_entry_spread_pct=max_entry_spread,
        untradeable=untradeable,
        data_gaps=data_gaps,
    )

