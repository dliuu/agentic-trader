import pytest

from scanner.models.flow_alert import FlowAlert
from scanner.rules.engine import RuleEngine


@pytest.fixture
def multi_signal_alert():
    """Alert that triggers OTM + premium + execution filters."""
    return FlowAlert(
        id="eng-1",
        ticker_symbol="MULTI",
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


def test_engine_rejects_below_confluence(sample_config):
    """Single-signal alert should not pass min_signals_required=2."""
    alert = FlowAlert(
        id="eng-2",
        ticker_symbol="WEAK",
        type="Calls",
        strike=100.0,
        expiry="2026-04-03",
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
