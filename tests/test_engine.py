import datetime

import pytest

from scanner.models.flow_alert import FlowAlert
from scanner.rules.engine import RuleEngine


@pytest.fixture
def multi_signal_alert():
    """Alert that triggers OTM + premium + execution filters."""
    return FlowAlert(
        id="eng-1",
        ticker="MULTI",
        type="Calls",
        strike=200.0,
        expiry="2026-04-03",
        total_premium=50000.0,
        total_size=600,
        open_interest=200,
        underlying_price=150.0,
        execution_type="Sweep",
        is_otm=True,
        created_at="2026-03-20T14:30:00Z",
    )


def test_engine_flags_candidate(sample_config, multi_signal_alert):
    engine = RuleEngine(sample_config)
    result = engine.evaluate(multi_signal_alert)
    assert result is not None
    assert result.ticker == "MULTI"
    assert result.confluence_score > 0
    assert len(result.signals) >= 2


def test_engine_rejects_below_confluence(sample_config, monkeypatch):
    """Single-signal alert should not pass min_signals_required=2."""
    monkeypatch.setattr(
        "scanner.models.flow_alert._today_for_dte",
        lambda: datetime.date(2026, 3, 21),
    )
    # OTM passes (5.3%), but expiry fails (28 DTE > max 14), execution fails (Split)
    alert = FlowAlert(
        id="eng-2",
        ticker="WEAK",
        type="Calls",
        strike=100.0,
        expiry="2026-04-18",  # 28 DTE, outside 1-14
        total_premium=10000.0,
        total_size=50,
        open_interest=100,
        underlying_price=95.0,
        execution_type="Split",
        is_otm=True,
        created_at="2026-03-20T14:30:00Z",
    )
    engine = RuleEngine(sample_config)
    result = engine.evaluate(alert)
    assert result is None


def test_evaluate_batch(sample_config, multi_signal_alert):
    engine = RuleEngine(sample_config)
    candidates = engine.evaluate_batch([multi_signal_alert])
    assert len(candidates) >= 1


def test_engine_produces_reasonable_candidates_from_fixture(
    sample_config, flow_fixture
):
    """Engine run on fixture produces a reasonable number of candidates (not 0, not all)."""
    data = flow_fixture.get("data", flow_fixture)
    if isinstance(data, dict):
        data = data.get("data", [])
    if not isinstance(data, list):
        data = []
    alerts = [FlowAlert.model_validate(item) for item in data]
    engine = RuleEngine(sample_config)
    candidates = engine.evaluate_batch(alerts)
    assert len(candidates) > 0, "Should produce at least one candidate"
    assert len(candidates) <= len(alerts), "Should not exceed number of alerts"
