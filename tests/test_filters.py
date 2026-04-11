import datetime

import pytest

from scanner.models.flow_alert import FlowAlert
from scanner.rules.filters import (
    check_execution_type,
    check_expiry,
    check_otm,
    check_premium,
    check_volume_oi,
)


@pytest.fixture
def deep_otm_alert():
    return FlowAlert(
        id="test-1",
        ticker="ACME",
        type="Calls",
        strike=180.0,
        expiry="2026-04-03",
        total_premium=75000.0,
        total_size=500,
        open_interest=100,
        underlying_price=140.0,
        execution_type="Sweep",
        is_otm=True,
        created_at="2026-03-20T14:30:00Z",
    )


def test_otm_filter_triggers(deep_otm_alert):
    cfg = {"min_otm_percentage": 5, "max_otm_percentage": 50}
    result = check_otm(deep_otm_alert, cfg)
    assert result is not None
    assert result.rule_name == "otm"
    assert "28.6%" in result.detail


def test_otm_filter_returns_none_when_underlying_price_missing():
    """Alert with underlying_price=None returns None (cannot compute OTM %)."""
    alert = FlowAlert(
        id="test-otm-none",
        ticker="NOSPOT",
        type="Calls",
        strike=180.0,
        expiry="2026-04-03",
        total_premium=75000.0,
        total_size=500,
        open_interest=100,
        underlying_price=None,
        created_at="2026-03-20T14:30:00Z",
    )
    cfg = {"min_otm_percentage": 5, "max_otm_percentage": 50}
    result = check_otm(alert, cfg)
    assert result is None


def test_otm_filter_skips_near_money():
    alert = FlowAlert(
        id="test-2",
        ticker="XYZ",
        type="Calls",
        strike=142.0,
        expiry="2026-04-03",
        total_premium=50000.0,
        total_size=200,
        open_interest=300,
        underlying_price=140.0,
        execution_type="Split",
        is_otm=True,
        created_at="2026-03-20T14:30:00Z",
    )
    cfg = {"min_otm_percentage": 5, "max_otm_percentage": 50}
    result = check_otm(alert, cfg)
    assert result is None


def test_premium_filter_rejects_below_threshold():
    """Premium below min_premium_usd returns None."""
    alert = FlowAlert(
        id="test-prem-low",
        ticker="SMALL",
        type="Calls",
        strike=100.0,
        expiry="2026-04-03",
        total_premium=5000.0,
        total_size=50,
        open_interest=100,
        underlying_price=95.0,
        created_at="2026-03-20T14:30:00Z",
    )
    cfg = {"min_premium_usd": 25000}
    result = check_premium(alert, cfg)
    assert result is None


def test_premium_filter():
    alert = FlowAlert(
        id="test-3",
        ticker="BIG",
        type="Puts",
        strike=100.0,
        expiry="2026-04-03",
        total_premium=120000.0,
        total_size=1000,
        underlying_price=130.0,
        execution_type="Block",
        is_otm=True,
        created_at="2026-03-20T14:30:00Z",
    )
    cfg = {"min_premium_usd": 25000}
    result = check_premium(alert, cfg)
    assert result is not None
    assert "120,000" in result.detail


def test_volume_oi_returns_none_when_open_interest_missing():
    """Alert with open_interest=None returns None (cannot compute ratio)."""
    alert = FlowAlert(
        id="test-vol-none",
        ticker="NOOI",
        type="Calls",
        strike=100.0,
        expiry="2026-04-03",
        total_premium=50000.0,
        total_size=500,
        open_interest=None,
        underlying_price=95.0,
        created_at="2026-03-20T14:30:00Z",
    )
    cfg = {"size_greater_oi": True, "min_volume_oi_ratio": 2.0}
    result = check_volume_oi(alert, cfg)
    assert result is None


def test_volume_oi_size_greater_oi_with_none_open_interest():
    """open_interest=None does not crash; returns None (ratio is None)."""
    alert = FlowAlert(
        id="test-vol-none2",
        ticker="NOOI2",
        type="Calls",
        strike=100.0,
        expiry="2026-04-03",
        total_premium=50000.0,
        total_size=100,
        open_interest=None,
        underlying_price=95.0,
        created_at="2026-03-20T14:30:00Z",
    )
    cfg = {"size_greater_oi": True, "min_volume_oi_ratio": 2.0}
    result = check_volume_oi(alert, cfg)
    assert result is None


def test_volume_oi_ratio():
    alert = FlowAlert(
        id="test-4",
        ticker="TINY",
        type="Calls",
        strike=50.0,
        expiry="2026-04-03",
        total_premium=30000.0,
        total_size=800,
        open_interest=150,
        underlying_price=42.0,
        execution_type="Sweep",
        is_otm=True,
        created_at="2026-03-20T14:30:00Z",
    )
    cfg = {"size_greater_oi": True, "min_volume_oi_ratio": 2.0}
    result = check_volume_oi(alert, cfg)
    assert result is not None
    assert "5.3x" in result.detail


# --- check_expiry ---


def test_expiry_filter_triggers_in_range(monkeypatch):
    """DTE within [min_dte, max_dte] returns SignalMatch."""
    monkeypatch.setattr(
        "scanner.models.flow_alert._today_for_dte",
        lambda: datetime.date(2026, 3, 21),
    )
    alert = FlowAlert(
        id="exp-1",
        ticker="EXP",
        type="Calls",
        strike=100.0,
        expiry="2026-04-03",
        total_premium=50000.0,
        total_size=100,
        open_interest=50,
        underlying_price=95.0,
        created_at="2026-03-20T14:30:00Z",
    )
    cfg = {"min_dte": 1, "max_dte": 14}
    result = check_expiry(alert, cfg)
    assert result is not None
    assert result.rule_name == "expiry"
    assert "DTE" in result.detail


def test_expiry_filter_rejects_outside_range():
    """DTE outside [min_dte, max_dte] returns None."""
    alert = FlowAlert(
        id="exp-2",
        ticker="FAR",
        type="Calls",
        strike=100.0,
        expiry="2026-05-18",  # ~58 DTE, outside 1-14
        total_premium=50000.0,
        total_size=100,
        open_interest=50,
        underlying_price=95.0,
        created_at="2026-03-20T14:30:00Z",
    )
    cfg = {"min_dte": 1, "max_dte": 14}
    result = check_expiry(alert, cfg)
    assert result is None


def test_expiry_filter_handles_invalid_expiry():
    """Invalid expiry (malformed date) returns None without crashing."""
    alert = FlowAlert(
        id="exp-3",
        ticker="BAD",
        type="Calls",
        strike=100.0,
        expiry="not-a-date",
        total_premium=50000.0,
        total_size=100,
        open_interest=50,
        underlying_price=95.0,
        created_at="2026-03-20T14:30:00Z",
    )
    cfg = {"min_dte": 1, "max_dte": 14}
    result = check_expiry(alert, cfg)
    assert result is None


# --- check_execution_type ---


def test_execution_filter_triggers_sweep():
    """execution_type=Sweep with require_sweep_or_block returns SignalMatch."""
    alert = FlowAlert(
        id="exec-1",
        ticker="SWP",
        type="Calls",
        strike=100.0,
        expiry="2026-04-03",
        total_premium=50000.0,
        total_size=100,
        open_interest=50,
        underlying_price=95.0,
        execution_type="Sweep",
        created_at="2026-03-20T14:30:00Z",
    )
    cfg = {"require_sweep_or_block": True}
    result = check_execution_type(alert, cfg)
    assert result is not None
    assert result.rule_name == "execution"
    assert "Sweep" in result.detail


def test_execution_filter_triggers_block():
    """execution_type=Block with require_sweep_or_block returns SignalMatch."""
    alert = FlowAlert(
        id="exec-2",
        ticker="BLK",
        type="Calls",
        strike=100.0,
        expiry="2026-04-03",
        total_premium=50000.0,
        total_size=100,
        open_interest=50,
        underlying_price=95.0,
        execution_type="Block",
        created_at="2026-03-20T14:30:00Z",
    )
    cfg = {"require_sweep_or_block": True}
    result = check_execution_type(alert, cfg)
    assert result is not None
    assert "Block" in result.detail


def test_execution_filter_returns_none_when_none_or_split():
    """execution_type=None or Split returns None."""
    base = {
        "id": "exec-3",
        "ticker": "NON",
        "type": "Calls",
        "strike": 100.0,
        "expiry": "2026-04-03",
        "total_premium": 50000.0,
        "total_size": 100,
        "open_interest": 50,
        "underlying_price": 95.0,
        "created_at": "2026-03-20T14:30:00Z",
    }
    cfg = {"require_sweep_or_block": True}
    alert_none = FlowAlert(**{**base, "execution_type": None})
    alert_split = FlowAlert(**{**base, "execution_type": "Split"})
    assert check_execution_type(alert_none, cfg) is None
    assert check_execution_type(alert_split, cfg) is None


def test_execution_filter_disabled():
    """require_sweep_or_block=False returns None regardless of execution_type."""
    alert = FlowAlert(
        id="exec-4",
        ticker="SWP",
        type="Calls",
        strike=100.0,
        expiry="2026-04-03",
        total_premium=50000.0,
        total_size=100,
        open_interest=50,
        underlying_price=95.0,
        execution_type="Sweep",
        created_at="2026-03-20T14:30:00Z",
    )
    cfg = {"require_sweep_or_block": False}
    result = check_execution_type(alert, cfg)
    assert result is None
