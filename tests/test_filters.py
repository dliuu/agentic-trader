import pytest

from scanner.models.flow_alert import FlowAlert
from scanner.rules.filters import check_otm, check_premium, check_volume_oi


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
