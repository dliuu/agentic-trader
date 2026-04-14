"""Re-grader milestone detection, guards, and blend math."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from tracker.enrichment_config import RegraderConfig, load_enrichment_config
from tracker.models import (
    ChainPollResult,
    FlowWatchResult,
    NewsWatchResult,
    Signal,
    SignalState,
)
from tracker.regrader import check_milestone_triggers


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr("shared.db.DB_PATH", tmp_path / "regrader_test.db")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _base_signal(**kwargs) -> Signal:
    d = dict(
        id="s1",
        ticker="ACME",
        strike=50.0,
        expiry=(_now().date() + timedelta(days=60)).isoformat(),
        option_type="call",
        direction="bullish",
        state=SignalState.ACCUMULATING,
        initial_score=80,
        initial_premium=100_000.0,
        initial_oi=100,
        initial_volume=500,
        initial_contract_adv=0,
        grade_id="g1",
        conviction_score=75.0,
        cumulative_premium=150_000.0,
        confirming_flows=2,
        created_at=_now() - timedelta(days=3),
        milestones_fired=[],
    )
    d.update(kwargs)
    return Signal(**d)


def _chain(oi: int | None = 100) -> ChainPollResult:
    return ChainPollResult(ticker="ACME", polled_at=_now(), contract_oi=oi)


class TestMilestones:
    def test_premium_2x(self):
        cfg = RegraderConfig()
        s = _base_signal(
            cumulative_premium=250_000.0,
            initial_premium=100_000.0,
            milestones_fired=[],
        )
        assert check_milestone_triggers(s, _chain(), None, cfg) == "premium_2x"

    def test_premium_dedup(self):
        cfg = RegraderConfig()
        s = _base_signal(
            cumulative_premium=250_000.0,
            milestones_fired=["premium_2x"],
        )
        assert check_milestone_triggers(s, _chain(500), None, cfg) != "premium_2x"

    def test_oi_3x(self):
        cfg = RegraderConfig()
        s = _base_signal(
            cumulative_premium=50_000.0,
            initial_premium=100_000.0,
            milestones_fired=["premium_2x"],
        )
        assert check_milestone_triggers(s, _chain(oi=350), None, cfg) == "oi_3x"

    def test_confirming_flows(self):
        cfg = RegraderConfig()
        s = _base_signal(
            cumulative_premium=50_000.0,
            initial_premium=100_000.0,
            confirming_flows=4,
            milestones_fired=["premium_2x", "oi_3x"],
        )
        assert check_milestone_triggers(s, _chain(oi=200), None, cfg) == "confirming_flows_3"

    def test_catalyst_headline(self):
        cfg = RegraderConfig()
        s = _base_signal(
            confirming_flows=0,
            cumulative_premium=50_000.0,
            milestones_fired=["premium_2x", "oi_3x", "confirming_flows_3"],
        )
        news = NewsWatchResult(
            signal_id=s.id,
            ticker=s.ticker,
            checked_at=_now(),
            regrade_recommended=True,
        )
        assert check_milestone_triggers(s, _chain(), news, cfg) == "catalyst_headline"

    def test_sec_filing(self):
        cfg = RegraderConfig()
        s = _base_signal(
            milestones_fired=[
                "premium_2x",
                "oi_3x",
                "confirming_flows_3",
                "catalyst_headline",
            ],
        )
        news = NewsWatchResult(
            signal_id=s.id,
            ticker=s.ticker,
            checked_at=_now(),
            filing_detected=True,
        )
        assert check_milestone_triggers(s, _chain(), news, cfg) == "sec_filing"


class TestBlend:
    def test_blend_formula(self):
        cfg = RegraderConfig(
            score_blend_deterministic_pct=55.0,
            score_blend_llm_pct=45.0,
        )
        det = 85.0
        llm = 60
        det_pct = cfg.score_blend_deterministic_pct / 100.0
        llm_pct = cfg.score_blend_llm_pct / 100.0
        blended = det_pct * det + llm_pct * llm
        assert abs(blended - 73.75) < 0.01


class TestLoadEnrichmentConfig:
    def test_defaults(self):
        cfg = load_enrichment_config({})
        assert cfg is None

    def test_yaml_section(self):
        raw = {
            "enrichment": {
                "regrader": {
                    "enabled": False,
                    "max_regrades_per_signal": 3,
                }
            }
        }
        cfg = load_enrichment_config(raw)
        assert cfg is not None
        assert cfg.regrader.enabled is False
        assert cfg.regrader.max_regrades_per_signal == 3


@pytest.mark.asyncio
class TestRegraderGuards:
    async def test_budget_exhausted_short_circuit(self):
        from grader.llm_client import LLMResponse
        from tracker.regrader import Regrader
        from tracker.signal_store import SignalStore

        class DummyLLM:
            async def complete(self, system, user, max_tokens=None):
                return LLMResponse(
                    text='{"score":50,"verdict":"pass","rationale":"x","key_development":"y","thesis_status":"unchanged"}',
                    input_tokens=1,
                    output_tokens=1,
                    latency_ms=1,
                    model="stub",
                )

        store = SignalStore()
        r = Regrader(
            DummyLLM(),  # type: ignore[arg-type]
            MagicMock(),
            "tok",
            "",
            store,
            config=RegraderConfig(),
            news_watcher=None,
        )
        s = _base_signal(regrade_count=5)
        res = await r.maybe_regrade(
            s,
            _chain(),
            FlowWatchResult(ticker="ACME", checked_at=_now(), events=[]),
            None,
            None,
            80.0,
        )
        assert not res.triggered
        assert res.skipped_reason == "regrade_budget_exhausted"
