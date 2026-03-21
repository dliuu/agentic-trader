"""Shared pytest fixtures."""
import pytest


@pytest.fixture
def sample_config():
    """Minimal config for unit tests."""
    return {
        "filters": {
            "otm": {"enabled": True, "min_otm_percentage": 5, "max_otm_percentage": 50},
            "premium": {"enabled": True, "min_premium_usd": 25000},
            "volume": {
                "enabled": True,
                "size_greater_oi": True,
                "min_volume_oi_ratio": 2.0,
            },
            "expiry": {"enabled": True, "min_dte": 1, "max_dte": 14},
            "execution": {"enabled": True, "require_sweep_or_block": True},
            "dark_pool": {"enabled": True, "min_notional_usd": 500000, "lookback_minutes": 30},
            "market_regime": {"enabled": True, "respect_tide_direction": True},
        },
        "confluence": {
            "min_signals_required": 2,
            "weights": {
                "otm": 1.0,
                "premium": 1.5,
                "volume": 1.0,
                "expiry": 0.5,
                "execution": 1.0,
                "dark_pool": 2.0,
                "market_regime": 0.5,
            },
        },
    }
