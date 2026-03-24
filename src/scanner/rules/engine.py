"""Core rule engine.

Takes raw alerts, runs every enabled filter, computes
confluence score, and decides which alerts become candidates.
"""
from __future__ import annotations
import uuid
from datetime import datetime

import structlog

from scanner.models.flow_alert import FlowAlert
from shared.models import Candidate, SignalMatch
from scanner.rules import filters

logger = structlog.get_logger()

FILTER_REGISTRY = {
    "otm": filters.check_otm,
    "premium": filters.check_premium,
    "volume": filters.check_volume_oi,
    "expiry": filters.check_expiry,
    "execution": filters.check_execution_type,
}


class RuleEngine:
    def __init__(self, config: dict):
        self._filters_cfg = config["filters"]
        self._confluence_cfg = config["confluence"]
        self._weights = self._confluence_cfg["weights"]
        self._min_signals = self._confluence_cfg["min_signals_required"]

    def evaluate(self, alert: FlowAlert) -> Candidate | None:
        """Run all enabled filters against a single alert."""
        matched_signals: list[SignalMatch] = []

        for rule_name, filter_fn in FILTER_REGISTRY.items():
            rule_cfg = self._filters_cfg.get(rule_name, {})
            if not rule_cfg.get("enabled", True):
                continue

            result = filter_fn(alert, rule_cfg)
            if result is not None:
                result.weight = self._weights.get(rule_name, result.weight)
                matched_signals.append(result)

        if len(matched_signals) < self._min_signals:
            logger.debug(
                "below_confluence",
                ticker=alert.ticker,
                matched=len(matched_signals),
                required=self._min_signals,
            )
            return None

        confluence_score = sum(s.weight for s in matched_signals)

        candidate = Candidate(
            id=str(uuid.uuid4()),
            source="flow_alert",
            ticker=alert.ticker,
            direction=alert.direction,
            strike=alert.strike,
            expiry=alert.expiry,
            premium_usd=alert.total_premium,
            underlying_price=alert.underlying_price,
            implied_volatility=alert.implied_volatility,
            execution_type=alert.execution_type,
            dte=alert.dte,
            signals=matched_signals,
            confluence_score=confluence_score,
            dark_pool_confirmation=False,
            market_tide_aligned=False,
            raw_alert_id=alert.id,
        )

        logger.info(
            "candidate_flagged",
            ticker=alert.ticker,
            direction=alert.direction,
            score=confluence_score,
            signals=[s.rule_name for s in matched_signals],
        )
        return candidate

    def evaluate_batch(self, alerts: list[FlowAlert]) -> list[Candidate]:
        """Run the engine over a batch of alerts."""
        candidates = []
        for alert in alerts:
            result = self.evaluate(alert)
            if result is not None:
                candidates.append(result)
        return candidates
