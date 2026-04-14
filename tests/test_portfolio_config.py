"""Portfolio config loader."""

from tracker.portfolio_config import PortfolioConfig, load_portfolio_config


def test_load_portfolio_config_defaults():
    raw = {"portfolio": {}}
    cfg = load_portfolio_config(raw)
    assert cfg is not None
    assert isinstance(cfg, PortfolioConfig)
    assert cfg.max_total_capital_usd == 50_000.0
    assert cfg.max_single_position_effective <= cfg.max_single_position_usd


def test_load_portfolio_config_missing_section():
    assert load_portfolio_config({}) is None


def test_effective_caps():
    cfg = PortfolioConfig(
        max_total_capital_usd=100_000,
        max_single_position_pct=4.0,
        max_single_position_usd=10_000,
        max_total_exposure_pct=20.0,
        max_total_exposure_usd=50_000,
    )
    assert cfg.max_single_position_effective == 4_000.0
    assert cfg.max_total_exposure_effective == 20_000.0
