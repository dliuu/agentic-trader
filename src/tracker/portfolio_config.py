"""Portfolio guardrails configuration.

All hard limits on capital exposure, position sizing, and concentration.
Loaded from the ``portfolio`` section of ``rules.yaml``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PortfolioConfig:
    """Portfolio-level risk limits.

    These are HARD LIMITS — a signal that violates any of these
    is blocked from the executor queue even if it reaches ACTIONABLE.
    """

    max_total_capital_usd: float = 50_000.0
    max_single_position_pct: float = 5.0
    max_single_position_usd: float = 2_500.0
    max_total_exposure_pct: float = 25.0
    max_total_exposure_usd: float = 12_500.0

    max_signals_per_sector: int = 2
    max_signals_per_ticker: int = 1
    max_concurrent_positions: int = 5

    max_single_loss_pct: float = 2.0
    daily_loss_limit_pct: float = 5.0

    min_option_volume: int = 50
    max_bid_ask_spread_pct: float = 15.0

    @property
    def max_single_position_effective(self) -> float:
        pct_cap = self.max_total_capital_usd * self.max_single_position_pct / 100.0
        return min(self.max_single_position_usd, pct_cap)

    @property
    def max_total_exposure_effective(self) -> float:
        pct_cap = self.max_total_capital_usd * self.max_total_exposure_pct / 100.0
        return min(self.max_total_exposure_usd, pct_cap)


def load_portfolio_config(raw_config: dict) -> PortfolioConfig | None:
    """Parse ``portfolio`` from rules.yaml. Returns ``None`` if the key is absent."""
    if "portfolio" not in raw_config:
        return None
    section = raw_config.get("portfolio") or {}
    return PortfolioConfig(
        max_total_capital_usd=float(section.get("max_total_capital_usd", 50_000)),
        max_single_position_pct=float(section.get("max_single_position_pct", 5.0)),
        max_single_position_usd=float(section.get("max_single_position_usd", 2_500)),
        max_total_exposure_pct=float(section.get("max_total_exposure_pct", 25.0)),
        max_total_exposure_usd=float(section.get("max_total_exposure_usd", 12_500)),
        max_signals_per_sector=int(section.get("max_signals_per_sector", 2)),
        max_signals_per_ticker=int(section.get("max_signals_per_ticker", 1)),
        max_concurrent_positions=int(section.get("max_concurrent_positions", 5)),
        max_single_loss_pct=float(section.get("max_single_loss_pct", 2.0)),
        daily_loss_limit_pct=float(section.get("daily_loss_limit_pct", 5.0)),
        min_option_volume=int(section.get("min_option_volume", 50)),
        max_bid_ask_spread_pct=float(section.get("max_bid_ask_spread_pct", 15.0)),
    )
