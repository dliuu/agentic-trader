import pytest
from datetime import datetime

from grader.agents.volatility_analyst import (
    _score_absolute_value,
    _score_from_context,
    _score_market_context,
    _score_relative_to_history,
)
from grader.context.sector_cache import SectorBenchmark, SectorBenchmarkCache
from grader.context.vol_ctx import VolContext
from shared.filters import VolScoringConfig


def make_vol_context(**overrides) -> VolContext:
    """Create a VolContext with sensible defaults. Override any field."""
    defaults = dict(
        ticker="AAPL",
        iv_rank=50.0,
        iv_percentile=50.0,
        current_iv=0.30,
        realized_vol_20d=0.28,
        realized_vol_60d=0.28,
        iv_rv_spread=0.02,
        iv_rv_ratio=1.07,
        rv_regime_ratio=1.0,
        near_term_iv=0.30,
        far_term_iv=0.30,
        term_structure_slope=0.0,
        candidate_expiry_iv=0.30,
        contract_delta=0.35,
        contract_gamma=0.05,
        contract_theta=-0.15,
        contract_vega=0.08,
        contract_mid_price=3.50,
        contract_volume=500,
        contract_oi=2000,
        theta_pct_of_premium=0.043,
        moneyness=0.95,
        candidate_dte=21,
        candidate_is_call=True,
        sector="Technology",
        fetched_at=datetime.utcnow(),
    )
    defaults.update(overrides)
    return VolContext(**defaults)


def make_sector_cache(**overrides) -> SectorBenchmarkCache:
    """Create a SectorBenchmarkCache with sensible defaults."""
    tech_bench = SectorBenchmark(
        sector="Technology",
        ticker_count=4,
        iv_rv_ratio_median=1.1,
        iv_rv_ratio_p25=0.9,
        iv_rv_ratio_p75=1.3,
        avg_iv_rank=45.0,
        avg_current_iv=0.32,
        computed_at=datetime.utcnow(),
    )
    all_bench = SectorBenchmark(
        sector="_all_sectors",
        ticker_count=38,
        iv_rv_ratio_median=1.05,
        iv_rv_ratio_p25=0.90,
        iv_rv_ratio_p75=1.25,
        avg_iv_rank=48.0,
        avg_current_iv=0.30,
        computed_at=datetime.utcnow(),
    )
    defaults = dict(
        benchmarks={"Technology": tech_bench, "_all_sectors": all_bench},
        market_iv_rank=50.0,
        market_iv=0.20,
        market_iv_rv_ratio=1.0,
        refreshed_at=datetime.utcnow(),
        ticker_snapshots=[],
    )
    defaults.update(overrides)
    return SectorBenchmarkCache(**defaults)


class TestAbsoluteValue:
    def test_cheap_vol_scores_high(self):
        """Low IV rank + IV below RV + low theta → high absolute score."""
        ctx = make_vol_context(iv_rank=18.0, iv_rv_ratio=0.85, theta_pct_of_premium=0.015)
        score, signals, _ = _score_absolute_value(ctx, VolScoringConfig())
        assert score > 70
        assert "iv_rank_low" in signals
        assert "iv_below_rv" in signals
        assert "theta_low" in signals

    def test_expensive_vol_scores_low(self):
        """High IV rank + IV premium over RV + high theta → low absolute score."""
        ctx = make_vol_context(iv_rank=82.0, iv_rv_ratio=1.55, theta_pct_of_premium=0.065)
        score, signals, _ = _score_absolute_value(ctx, VolScoringConfig())
        assert score < 30
        assert "iv_rank_high" in signals
        assert "iv_premium" in signals or "iv_extreme_premium" in signals
        assert "theta_high" in signals or "theta_extreme" in signals

    def test_neutral_vol_near_baseline(self):
        """All metrics in middle range → score near 50."""
        ctx = make_vol_context(iv_rank=50.0, iv_rv_ratio=1.05, theta_pct_of_premium=0.03)
        score, signals, _ = _score_absolute_value(ctx, VolScoringConfig())
        assert 40 <= score <= 60
        assert len(signals) == 0

    def test_vega_synergy(self):
        """High vega + low IV rank → synergy bonus."""
        ctx = make_vol_context(iv_rank=20.0, contract_vega=0.50, contract_mid_price=3.50)
        score, signals, _ = _score_absolute_value(ctx, VolScoringConfig())
        assert "vega_iv_synergy" in signals

    def test_vega_conflict(self):
        """High vega + high IV rank → conflict penalty."""
        ctx = make_vol_context(iv_rank=80.0, contract_vega=0.50, contract_mid_price=3.50)
        score, signals, _ = _score_absolute_value(ctx, VolScoringConfig())
        assert "vega_iv_conflict" in signals


class TestRelativeToHistory:
    def test_inverted_term_near_expiry(self):
        """Inverted term structure + near-term expiry → bonus."""
        ctx = make_vol_context(term_structure_slope=-0.12, candidate_dte=14)
        score, signals, _ = _score_relative_to_history(ctx, VolScoringConfig())
        assert "term_inverted_near" in signals
        assert score > 55

    def test_steep_contango_near_expiry(self):
        """Steep contango + near-term expiry → penalty."""
        ctx = make_vol_context(term_structure_slope=0.20, candidate_dte=14)
        score, signals, _ = _score_relative_to_history(ctx, VolScoringConfig())
        assert "term_contango_near" in signals
        assert score < 45

    def test_rv_expanding(self):
        """20d RV significantly above 60d RV → expansion bonus."""
        ctx = make_vol_context(rv_regime_ratio=1.35)
        score, signals, _ = _score_relative_to_history(ctx, VolScoringConfig())
        assert "rv_expanding" in signals

    def test_rv_compressing(self):
        """20d RV significantly below 60d RV → compression penalty."""
        ctx = make_vol_context(rv_regime_ratio=0.65)
        score, signals, _ = _score_relative_to_history(ctx, VolScoringConfig())
        assert "rv_compressing" in signals

    def test_percentile_below_rank(self):
        """Percentile well below rank → cheap vs distribution."""
        ctx = make_vol_context(iv_rank=50.0, iv_percentile=30.0)
        score, signals, _ = _score_relative_to_history(ctx, VolScoringConfig())
        assert "pctl_below_rank" in signals


class TestMarketContext:
    def test_ticker_cheap_market_expensive(self):
        """Ticker IV rank low, market rank high → relative bargain."""
        ctx = make_vol_context(iv_rank=20.0)
        cache = make_sector_cache(market_iv_rank=65.0)
        score, signals, _ = _score_market_context(ctx, cache, VolScoringConfig())
        assert "ticker_cheap_vs_market" in signals
        assert score > 55

    def test_ticker_expensive_market_calm(self):
        """Ticker IV rank high, market rank low → paying premium."""
        ctx = make_vol_context(iv_rank=75.0)
        cache = make_sector_cache(market_iv_rank=30.0)
        score, signals, _ = _score_market_context(ctx, cache, VolScoringConfig())
        assert "ticker_expensive_vs_market" in signals
        assert score < 45

    def test_sector_cheap(self):
        """Ticker IV/RV below sector median → cheaper than peers."""
        ctx = make_vol_context(iv_rv_ratio=0.95)
        cache = make_sector_cache()
        score, signals, _ = _score_market_context(ctx, cache, VolScoringConfig())
        assert "sector_cheap" in signals

    def test_sector_expensive(self):
        """Ticker IV/RV above sector 75th percentile → pricier than peers."""
        ctx = make_vol_context(iv_rv_ratio=1.45)
        cache = make_sector_cache()
        score, signals, _ = _score_market_context(ctx, cache, VolScoringConfig())
        assert "sector_expensive" in signals

    def test_delta_sweet_spot(self):
        ctx = make_vol_context(contract_delta=0.35)
        cache = make_sector_cache()
        score, signals, _ = _score_market_context(ctx, cache, VolScoringConfig())
        assert "delta_sweet_spot" in signals

    def test_delta_lottery_ticket(self):
        ctx = make_vol_context(contract_delta=0.05)
        cache = make_sector_cache()
        score, signals, _ = _score_market_context(ctx, cache, VolScoringConfig())
        assert "delta_lottery" in signals

    def test_unknown_sector_no_crash(self):
        """Unknown sector should not crash — just skip peer comparison."""
        ctx = make_vol_context(sector="Alien Technology")
        cache = make_sector_cache()
        score, signals, _ = _score_market_context(ctx, cache, VolScoringConfig())
        assert isinstance(score, float)


class TestFullScoring:
    def test_dream_candidate_scores_high(self):
        """Everything aligned for the buyer → high score."""
        ctx = make_vol_context(
            iv_rank=15.0,
            iv_percentile=12.0,
            iv_rv_ratio=0.78,
            theta_pct_of_premium=0.015,
            contract_vega=0.50,
            contract_mid_price=3.50,
            term_structure_slope=-0.10,
            candidate_dte=14,
            rv_regime_ratio=1.30,
            contract_delta=0.35,
        )
        cache = make_sector_cache(market_iv_rank=60.0)
        result = _score_from_context(ctx, cache, VolScoringConfig())
        assert result.score >= 75
        assert not result.skipped

    def test_nightmare_candidate_scores_low(self):
        """Everything against the buyer → low score."""
        ctx = make_vol_context(
            iv_rank=88.0,
            iv_percentile=92.0,
            iv_rv_ratio=1.65,
            theta_pct_of_premium=0.09,
            contract_vega=0.50,
            contract_mid_price=3.50,
            term_structure_slope=0.25,
            candidate_dte=5,
            rv_regime_ratio=0.60,
            contract_delta=0.05,
        )
        cache = make_sector_cache(market_iv_rank=25.0)
        result = _score_from_context(ctx, cache, VolScoringConfig())
        assert result.score <= 25
        assert not result.skipped

    def test_score_clamped_to_range(self):
        """Score never goes below min_score or above max_score."""
        config = VolScoringConfig(min_score=10, max_score=90)
        ctx = make_vol_context(iv_rank=5.0, iv_rv_ratio=0.5)
        cache = make_sector_cache()
        result = _score_from_context(ctx, cache, config)
        assert config.min_score <= result.score <= config.max_score

    def test_rationale_is_human_readable(self):
        """Rationale string should contain actual numbers and be readable."""
        ctx = make_vol_context(iv_rank=22.0, iv_rv_ratio=0.88)
        cache = make_sector_cache()
        result = _score_from_context(ctx, cache, VolScoringConfig())
        assert "22" in result.rationale
        assert "0.88" in result.rationale
        assert len(result.rationale) > 50

    def test_signals_list_is_populated(self):
        """Non-neutral inputs should produce signals."""
        ctx = make_vol_context(iv_rank=20.0, iv_rv_ratio=0.85)
        cache = make_sector_cache()
        result = _score_from_context(ctx, cache, VolScoringConfig())
        assert len(result.signals) >= 2

    def test_config_weights_sum_to_one(self):
        """Sanity check that default weights sum to 1.0."""
        config = VolScoringConfig()
        total = config.absolute_weight + config.historical_weight + config.market_context_weight
        assert abs(total - 1.0) < 0.001

