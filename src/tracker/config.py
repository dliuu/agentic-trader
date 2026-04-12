"""Tracker configuration loaded from rules.yaml."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ConvictionScoringConfig:
    """Point deltas applied each poll cycle based on observed evidence."""

    # Positive signals
    oi_increase_per_10pct: int = 2
    oi_increase_cap: int = 8
    confirming_flow_bonus: int = 5
    confirming_flow_cap: int = 15
    ghost_strike_bonus: int = 3
    put_call_shift_bonus: int = 3
    premium_accumulation_bonus: int = 2

    # Negative signals
    oi_decrease_per_10pct: int = -3
    spread_widened_penalty: int = -5
    silence_penalty_per_day: int = -4
    spot_moved_away_per_5pct: int = -2
    dte_pressure_below_14: int = -3


@dataclass(frozen=True)
class TrackerConfig:
    """All tracker thresholds. Loaded from rules.yaml 'tracker' section.

    To tune the system, edit config/rules.yaml and restart.
    """

    enabled: bool = True

    # Polling cadence
    poll_interval_market_seconds: int = 300
    poll_interval_off_hours_seconds: int = 1800
    morning_reconciliation: bool = True

    # Monitoring window
    monitoring_window_days: int = 7
    min_dte_for_monitoring: int = 3

    # Capacity
    max_active_signals: int = 10
    max_snapshots_per_signal: int = 500

    # Conviction thresholds
    actionable_conviction: float = 90.0
    actionable_min_confirming_flows: int = 2
    actionable_min_oi_ratio: float = 1.5

    # Decay thresholds
    decay_conviction: float = 60.0
    decay_window_conviction: float = 80.0
    silence_decay_days: int = 2

    # Neighbor analysis
    neighbor_strike_radius: int = 5
    neighbor_expiry_radius: int = 1

    # Scoring
    scoring: ConvictionScoringConfig = ConvictionScoringConfig()


def load_tracker_config(raw_config: dict) -> TrackerConfig:
    """Build TrackerConfig from the parsed rules.yaml dict.

    Args:
        raw_config: The full config dict from load_config(). Reads the
                    'tracker' key. Missing keys use dataclass defaults.
    """
    section = raw_config.get("tracker") or {}
    scoring_raw = section.get("scoring") or {}

    scoring = ConvictionScoringConfig(
        oi_increase_per_10pct=int(scoring_raw.get("oi_increase_per_10pct", 2)),
        oi_increase_cap=int(scoring_raw.get("oi_increase_cap", 8)),
        confirming_flow_bonus=int(scoring_raw.get("confirming_flow_bonus", 5)),
        confirming_flow_cap=int(scoring_raw.get("confirming_flow_cap", 15)),
        ghost_strike_bonus=int(scoring_raw.get("ghost_strike_bonus", 3)),
        put_call_shift_bonus=int(scoring_raw.get("put_call_shift_bonus", 3)),
        premium_accumulation_bonus=int(scoring_raw.get("premium_accumulation_bonus", 2)),
        oi_decrease_per_10pct=int(scoring_raw.get("oi_decrease_per_10pct", -3)),
        spread_widened_penalty=int(scoring_raw.get("spread_widened_penalty", -5)),
        silence_penalty_per_day=int(scoring_raw.get("silence_penalty_per_day", -4)),
        spot_moved_away_per_5pct=int(scoring_raw.get("spot_moved_away_per_5pct", -2)),
        dte_pressure_below_14=int(scoring_raw.get("dte_pressure_below_14", -3)),
    )

    return TrackerConfig(
        enabled=bool(section.get("enabled", True)),
        poll_interval_market_seconds=int(section.get("poll_interval_market_seconds", 300)),
        poll_interval_off_hours_seconds=int(section.get("poll_interval_off_hours_seconds", 1800)),
        morning_reconciliation=bool(section.get("morning_reconciliation", True)),
        monitoring_window_days=int(section.get("monitoring_window_days", 7)),
        min_dte_for_monitoring=int(section.get("min_dte_for_monitoring", 3)),
        max_active_signals=int(section.get("max_active_signals", 10)),
        max_snapshots_per_signal=int(section.get("max_snapshots_per_signal", 500)),
        actionable_conviction=float(section.get("actionable_conviction", 90.0)),
        actionable_min_confirming_flows=int(section.get("actionable_min_confirming_flows", 2)),
        actionable_min_oi_ratio=float(section.get("actionable_min_oi_ratio", 1.5)),
        decay_conviction=float(section.get("decay_conviction", 60.0)),
        decay_window_conviction=float(section.get("decay_window_conviction", 80.0)),
        silence_decay_days=int(section.get("silence_decay_days", 2)),
        neighbor_strike_radius=int(section.get("neighbor_strike_radius", 5)),
        neighbor_expiry_radius=int(section.get("neighbor_expiry_radius", 1)),
        scoring=scoring,
    )
