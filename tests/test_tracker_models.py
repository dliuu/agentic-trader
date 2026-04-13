"""Tests for tracker models and config loading."""

from __future__ import annotations

from datetime import datetime, timezone

from tracker.config import TrackerConfig, load_tracker_config
from tracker.models import Signal, SignalState, ACTIVE_STATES, TERMINAL_STATES


class TestSignalState:
    def test_active_states(self):
        assert SignalState.PENDING in ACTIVE_STATES
        assert SignalState.ACCUMULATING in ACTIVE_STATES
        assert SignalState.ACTIONABLE not in ACTIVE_STATES

    def test_terminal_states(self):
        assert SignalState.EXECUTED in TERMINAL_STATES
        assert SignalState.EXPIRED in TERMINAL_STATES
        assert SignalState.DECAYED in TERMINAL_STATES
        assert SignalState.PENDING not in TERMINAL_STATES

    def test_signal_is_active(self):
        s = Signal(
            id="test", ticker="ACME", strike=50.0, expiry="2025-08-15",
            option_type="call", direction="bullish", state=SignalState.PENDING,
            initial_score=82, initial_premium=50000, initial_oi=100,
            initial_volume=500, grade_id="g1", conviction_score=82.0,
            created_at=datetime.now(timezone.utc),
        )
        assert s.is_active is True
        assert s.is_terminal is False

    def test_signal_is_terminal(self):
        s = Signal(
            id="test", ticker="ACME", strike=50.0, expiry="2025-08-15",
            option_type="call", direction="bullish", state=SignalState.EXPIRED,
            initial_score=82, initial_premium=50000, initial_oi=100,
            initial_volume=500, grade_id="g1", conviction_score=82.0,
            created_at=datetime.now(timezone.utc),
        )
        assert s.is_active is False
        assert s.is_terminal is True


class TestLoadTrackerConfig:
    def test_defaults(self):
        cfg = load_tracker_config({})
        assert cfg.monitoring_window_days == 7
        assert cfg.actionable_conviction == 90.0
        assert cfg.scoring.oi_increase_per_10pct == 2

    def test_overrides(self):
        raw = {
            "tracker": {
                "monitoring_window_days": 14,
                "actionable_conviction": 85.0,
                "scoring": {
                    "confirming_flow_bonus": 10,
                },
            }
        }
        cfg = load_tracker_config(raw)
        assert cfg.monitoring_window_days == 14
        assert cfg.actionable_conviction == 85.0
        assert cfg.scoring.confirming_flow_bonus == 10
        # Non-overridden values keep defaults
        assert cfg.scoring.oi_increase_per_10pct == 2

    def test_missing_tracker_section(self):
        cfg = load_tracker_config({"grader": {"enabled": True}})
        assert cfg.enabled is True
        assert isinstance(cfg, TrackerConfig)

    def test_news_section_defaults(self):
        cfg = load_tracker_config({})
        assert cfg.news.headline_interval_seconds == 14400
        assert cfg.news.edgar_interval_seconds == 14400

    def test_news_section_overrides(self):
        cfg = load_tracker_config(
            {
                "tracker": {
                    "news": {
                        "enabled": False,
                        "headline_interval_seconds": 7200,
                        "edgar_user_agent": "custom/1.0 test@example.com",
                    }
                }
            }
        )
        assert cfg.news.enabled is False
        assert cfg.news.headline_interval_seconds == 7200
        assert cfg.news.edgar_user_agent == "custom/1.0 test@example.com"
