"""Cross-signal confluence enrichment.

After the rule engine flags candidates from options flow,
this module checks for confirming signals from other sources:
dark pool prints and market tide direction.
"""
from __future__ import annotations
from datetime import datetime, timedelta

import structlog

from scanner.models.candidate import Candidate, SignalMatch
from scanner.models.dark_pool import DarkPoolPrint
from scanner.models.market_tide import MarketTide

logger = structlog.get_logger()


class ConfluenceEnricher:
    def __init__(self, config: dict):
        self._dp_cfg = config["filters"]["dark_pool"]
        self._tide_cfg = config["filters"]["market_regime"]
        self._weights = config["confluence"]["weights"]

    def enrich(
        self,
        candidate: Candidate,
        dark_pool_prints: list[DarkPoolPrint],
        market_tide: MarketTide,
    ) -> Candidate:
        """Add dark pool and market tide signals to candidate."""
        if self._dp_cfg.get("enabled", True):
            lookback = timedelta(minutes=self._dp_cfg["lookback_minutes"])
            cutoff = datetime.utcnow() - lookback
            matching_prints = [
                dp
                for dp in dark_pool_prints
                if dp.ticker == candidate.ticker
                and dp.notional >= self._dp_cfg["min_notional_usd"]
                and dp.executed_at >= cutoff
            ]
            if matching_prints:
                candidate.dark_pool_confirmation = True
                dp_signal = SignalMatch(
                    rule_name="dark_pool",
                    weight=self._weights.get("dark_pool", 2.0),
                    detail=f"{len(matching_prints)} dark pool prints "
                    f">= ${self._dp_cfg['min_notional_usd']:,.0f} "
                    f"in last {self._dp_cfg['lookback_minutes']}min",
                )
                candidate.signals.append(dp_signal)
                candidate.confluence_score += dp_signal.weight

        if self._tide_cfg.get("enabled", True) and self._tide_cfg.get(
            "respect_tide_direction"
        ):
            tide_direction = market_tide.direction
            aligned = tide_direction == "neutral" or tide_direction == candidate.direction
            candidate.market_tide_aligned = aligned
            if aligned:
                tide_signal = SignalMatch(
                    rule_name="market_regime",
                    weight=self._weights.get("market_regime", 0.5),
                    detail=f"Market tide {tide_direction} aligns "
                    f"with {candidate.direction} signal",
                )
                candidate.signals.append(tide_signal)
                candidate.confluence_score += tide_signal.weight

        logger.info(
            "confluence_enriched",
            ticker=candidate.ticker,
            dark_pool=candidate.dark_pool_confirmation,
            tide_aligned=candidate.market_tide_aligned,
            final_score=candidate.confluence_score,
        )
        return candidate
