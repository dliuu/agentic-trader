"""Individual filter functions."""
from __future__ import annotations

# Each filter takes a FlowAlert and the config, returns a SignalMatch if the rule
# triggers or None if it doesn't. Filters are pure functions — no side effects.
from scanner.models.flow_alert import FlowAlert
from scanner.models.candidate import SignalMatch


def check_otm(alert: FlowAlert, cfg: dict) -> SignalMatch | None:
    """Flag deep out-of-the-money options."""
    pct = alert.otm_percentage
    if pct is None:
        return None
    min_pct = cfg["min_otm_percentage"]
    max_pct = cfg["max_otm_percentage"]
    if min_pct <= pct <= max_pct:
        return SignalMatch(
            rule_name="otm",
            weight=1.0,
            detail=f"OTM {pct:.1f}% (strike {alert.strike}, "
            f"spot {alert.underlying_price})",
        )
    return None


def check_premium(alert: FlowAlert, cfg: dict) -> SignalMatch | None:
    """Flag large premium trades."""
    if alert.total_premium >= cfg["min_premium_usd"]:
        return SignalMatch(
            rule_name="premium",
            weight=1.5,
            detail=f"Premium ${alert.total_premium:,.0f} "
            f"(min ${cfg['min_premium_usd']:,.0f})",
        )
    return None


def check_volume_oi(alert: FlowAlert, cfg: dict) -> SignalMatch | None:
    """Flag trades where volume dwarfs open interest."""
    ratio = alert.volume_oi_ratio
    if ratio is None:
        return None
    if cfg.get("size_greater_oi") and alert.total_size > (alert.open_interest or 0):
        return SignalMatch(
            rule_name="volume",
            weight=1.0,
            detail=f"Size {alert.total_size} > OI {alert.open_interest} "
            f"(ratio {ratio:.1f}x)",
        )
    if ratio >= cfg.get("min_volume_oi_ratio", 2.0):
        return SignalMatch(
            rule_name="volume",
            weight=1.0,
            detail=f"Vol/OI ratio {ratio:.1f}x " f"(min {cfg['min_volume_oi_ratio']}x)",
        )
    return None


def check_expiry(alert: FlowAlert, cfg: dict) -> SignalMatch | None:
    """Flag near-term expiry (directional bets, not hedges)."""
    dte = alert.dte
    if cfg["min_dte"] <= dte <= cfg["max_dte"]:
        return SignalMatch(
            rule_name="expiry",
            weight=0.5,
            detail=f"{dte} DTE (window {cfg['min_dte']}-{cfg['max_dte']})",
        )
    return None


def check_execution_type(alert: FlowAlert, cfg: dict) -> SignalMatch | None:
    """Flag sweeps and blocks — urgency/size signals."""
    if not cfg.get("require_sweep_or_block"):
        return None
    if alert.execution_type in ("Sweep", "Block"):
        return SignalMatch(
            rule_name="execution",
            weight=1.0,
            detail=f"Execution type: {alert.execution_type}",
        )
    return None
