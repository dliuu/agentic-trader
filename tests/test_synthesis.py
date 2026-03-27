"""Unit and integration tests for aggregator, synthesis prompts, and synthesis agent (design Step 6)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from grader.aggregator import Aggregator
from grader.gate3 import run_gate3
from grader.llm_client import LLMResponse
from grader.synthesis import (
    SynthesisAgent,
    SynthesisParseError,
    apply_synthesis_constraints,
)
from grader.synthesis_prompt import (
    build_synthesis_system_prompt,
    build_synthesis_user_message,
    estimate_synthesis_token_count,
)
from shared.filters import AgentWeights
from shared.models import Candidate, RiskConvictionScore, SignalMatch, SubScore


def _sub(
    agent: str,
    score: int,
    *,
    skipped: bool = False,
    rationale: str = "rationale text here",
    signals: list[str] | None = None,
    skip_reason: str | None = None,
) -> SubScore:
    return SubScore(
        agent=agent,
        score=score,
        rationale=rationale,
        signals=signals or ["s1", "s2"],
        skipped=skipped,
        skip_reason=skip_reason,
    )


def _full_board(**overrides: SubScore) -> dict[str, SubScore]:
    base: dict[str, SubScore] = {
        "flow_analyst": _sub("flow_analyst", 60),
        "volatility_analyst": _sub("volatility_analyst", 60),
        "risk_analyst": _sub("risk_analyst", 60),
        "sentiment_analyst": _sub("sentiment_analyst", 60),
        "insider_tracker": _sub("insider_tracker", 60),
        "sector_analyst": _sub("sector_analyst", 60),
    }
    base.update(overrides)
    return base


@pytest.fixture
def sample_candidate() -> Candidate:
    return Candidate(
        id="syn-1",
        source="flow_alert",
        ticker="ACME",
        direction="bullish",
        strike=180.0,
        expiry="2026-04-03",
        premium_usd=75_000.0,
        underlying_price=140.0,
        implied_volatility=None,
        execution_type="Sweep",
        dte=14,
        signals=[SignalMatch(rule_name="otm", weight=1.0, detail="OTM")],
        confluence_score=2.0,
        dark_pool_confirmation=False,
        market_tide_aligned=False,
        raw_alert_id="raw-1",
        scanned_at=datetime.now(timezone.utc),
    )


# --- Aggregator: weighted average (2) ---


def test_aggregator_weighted_average_all_active_equal_scores():
    agg = Aggregator()
    board = _full_board()
    r = agg.aggregate(board)
    assert r.weighted_average == 60.0
    assert "flow_analyst" not in r.skipped_agents


def test_aggregator_renormalizes_weights_when_agent_skipped():
    """Skipped agent excluded; remaining weights renormalized to sum to 1."""
    agg = Aggregator()
    board = _full_board(
        sentiment_analyst=_sub("sentiment_analyst", 100, skipped=True, skip_reason="down"),
        flow_analyst=_sub("flow_analyst", 0),
        volatility_analyst=_sub("volatility_analyst", 0),
        risk_analyst=_sub("risk_analyst", 0),
        insider_tracker=_sub("insider_tracker", 0),
        sector_analyst=_sub("sector_analyst", 0),
    )
    r = agg.aggregate(board)
    # Only five agents active, all score 0 except weights apply uniformly -> 0
    assert r.weighted_average == 0.0
    assert "sentiment_analyst" in r.skipped_agents


# --- Aggregator: conflicts (6) ---


def test_conflict_high_conviction_high_risk():
    board = _full_board(
        flow_analyst=_sub("flow_analyst", 80),
        risk_analyst=_sub("risk_analyst", 30),
    )
    names = {c.name for c in Aggregator().aggregate(board).conflict_flags}
    assert "high_conviction_high_risk" in names


def test_conflict_sentiment_contradicts_flow():
    board = _full_board(
        flow_analyst=_sub("flow_analyst", 75),
        sentiment_analyst=_sub("sentiment_analyst", 30),
    )
    names = {c.name for c in Aggregator().aggregate(board).conflict_flags}
    assert "sentiment_contradicts_flow" in names


def test_conflict_insider_selling_despite_flow():
    board = _full_board(
        flow_analyst=_sub("flow_analyst", 75),
        insider_tracker=_sub("insider_tracker", 20),
    )
    names = {c.name for c in Aggregator().aggregate(board).conflict_flags}
    assert "insider_selling_despite_flow" in names


def test_conflict_sector_headwind():
    board = _full_board(
        flow_analyst=_sub("flow_analyst", 70),
        sector_analyst=_sub("sector_analyst", 30),
    )
    names = {c.name for c in Aggregator().aggregate(board).conflict_flags}
    assert "sector_headwind" in names


def test_conflict_vol_and_risk_both_low():
    board = _full_board(
        volatility_analyst=_sub("volatility_analyst", 30),
        risk_analyst=_sub("risk_analyst", 30),
    )
    names = {c.name for c in Aggregator().aggregate(board).conflict_flags}
    assert "vol_and_risk_both_low" in names


def test_conflict_unanimous_conviction_warning():
    board = _full_board(
        flow_analyst=_sub("flow_analyst", 70),
        volatility_analyst=_sub("volatility_analyst", 70),
        risk_analyst=_sub("risk_analyst", 70),
        sentiment_analyst=_sub("sentiment_analyst", 70),
        insider_tracker=_sub("insider_tracker", 70),
        sector_analyst=_sub("sector_analyst", 70),
    )
    flags = Aggregator().aggregate(board).conflict_flags
    assert any(c.name == "unanimous_conviction" for c in flags)


# --- Aggregator: agreement (2) + risk (1) ---


def test_agent_agreement_strong_low_stdev():
    board = _full_board(
        flow_analyst=_sub("flow_analyst", 50),
        volatility_analyst=_sub("volatility_analyst", 52),
        risk_analyst=_sub("risk_analyst", 51),
        sentiment_analyst=_sub("sentiment_analyst", 50),
        insider_tracker=_sub("insider_tracker", 51),
        sector_analyst=_sub("sector_analyst", 50),
    )
    r = Aggregator().aggregate(board)
    assert r.agent_agreement == "strong"
    assert r.score_stdev < 10


def test_agent_agreement_weak_high_stdev():
    board = _full_board(
        flow_analyst=_sub("flow_analyst", 100),
        volatility_analyst=_sub("volatility_analyst", 0),
        risk_analyst=_sub("risk_analyst", 100),
        sentiment_analyst=_sub("sentiment_analyst", 0),
        insider_tracker=_sub("insider_tracker", 100),
        sector_analyst=_sub("sector_analyst", 0),
    )
    r = Aggregator().aggregate(board)
    assert r.agent_agreement == "weak"
    assert r.score_stdev > 20


def test_aggregator_extracts_risk_conviction_score():
    risk = RiskConvictionScore(
        agent="risk_analyst",
        score=55,
        rationale="ok",
        signals=[],
        recommended_position_size=0.4,
        recommended_stop_loss_pct=0.3,
        max_entry_spread_pct=0.02,
    )
    board = _full_board(risk_analyst=risk)
    r = Aggregator().aggregate(board)
    assert r.risk_score is risk


# --- Prompt builder: content (7) + budget (1) ---


def test_user_message_contains_ticker(sample_candidate: Candidate):
    agg = Aggregator().aggregate(_full_board())
    u = build_synthesis_user_message(sample_candidate, _full_board(), agg)
    assert "ACME" in u


def test_user_message_lists_all_sub_score_agents(sample_candidate: Candidate):
    agg = Aggregator().aggregate(_full_board())
    u = build_synthesis_user_message(sample_candidate, _full_board(), agg)
    for name in (
        "flow_analyst",
        "volatility_analyst",
        "risk_analyst",
        "sentiment_analyst",
        "insider_tracker",
        "sector_analyst",
    ):
        assert name in u


def test_user_message_contains_aggregation_block(sample_candidate: Candidate):
    agg = Aggregator().aggregate(_full_board())
    u = build_synthesis_user_message(sample_candidate, _full_board(), agg)
    assert "AGGREGATION:" in u
    assert "weighted_average:" in u
    assert "score_stdev:" in u
    assert "agent_agreement:" in u


def test_user_message_lists_conflicts_when_present(sample_candidate: Candidate):
    board = _full_board(
        flow_analyst=_sub("flow_analyst", 80),
        risk_analyst=_sub("risk_analyst", 30),
    )
    agg = Aggregator().aggregate(board)
    u = build_synthesis_user_message(sample_candidate, board, agg)
    assert "high_conviction_high_risk" in u
    assert "severity=critical" in u


def test_user_message_skipped_agents_section(sample_candidate: Candidate):
    board = _full_board(
        sector_analyst=_sub("sector_analyst", 50, skipped=True, skip_reason="no data"),
    )
    agg = Aggregator().aggregate(board)
    u = build_synthesis_user_message(sample_candidate, board, agg)
    assert "SKIPPED AGENTS:" in u
    assert "sector_analyst" in u
    assert "no data" in u


def test_user_message_risk_params_from_risk_conviction(sample_candidate: Candidate):
    risk = RiskConvictionScore(
        agent="risk_analyst",
        score=60,
        rationale="r",
        signals=[],
        recommended_position_size=0.33,
        recommended_stop_loss_pct=0.25,
        max_entry_spread_pct=0.04,
    )
    board = _full_board(risk_analyst=risk)
    agg = Aggregator().aggregate(board)
    u = build_synthesis_user_message(sample_candidate, board, agg)
    assert "0.3300" in u or "0.33" in u
    assert "recommended_stop_loss_pct" in u


def test_user_message_truncates_long_rationale(sample_candidate: Candidate):
    long_r = "x" * 500
    board = _full_board(flow_analyst=_sub("flow_analyst", 50, rationale=long_r))
    agg = Aggregator().aggregate(board)
    u = build_synthesis_user_message(sample_candidate, board, agg)
    assert "..." in u
    assert len(u) < len(long_r) + 2000


def test_token_estimate_budget_positive(sample_candidate: Candidate):
    agg = Aggregator().aggregate(_full_board())
    u = build_synthesis_user_message(sample_candidate, _full_board(), agg)
    est = estimate_synthesis_token_count(u)
    assert est >= len(u) // 4


# --- System prompt structure (3) ---


def test_synthesis_system_prompt_has_score_bands():
    s = build_synthesis_system_prompt()
    assert "80" in s and "100" in s
    assert "70" in s and "79" in s


def test_synthesis_system_prompt_requires_json_object():
    s = build_synthesis_system_prompt()
    assert "JSON" in s
    assert "position_size_modifier" in s


def test_synthesis_system_prompt_mentions_conflicts_and_caps():
    s = build_synthesis_system_prompt()
    assert "conflict" in s.lower()
    assert "cap" in s.lower() or "ceiling" in s.lower()


# --- Response parsing (via SynthesisAgent + mocks) (7) ---


def _valid_synthesis_json(score: int = 80, verdict: str = "pass") -> str:
    return json.dumps(
        {
            "score": score,
            "verdict": verdict,
            "confidence": "high",
            "rationale": "Strong alignment across agents.",
            "conflict_resolution": "Accepted aggregate.",
            "key_signal": "sweep size",
            "position_size_modifier": 0.9,
        }
    )


@pytest.mark.asyncio
async def test_synthesis_parses_valid_json(sample_candidate: Candidate):
    llm = AsyncMock()
    llm.complete = AsyncMock(
        return_value=LLMResponse(
            text=_valid_synthesis_json(),
            input_tokens=10,
            output_tokens=20,
            latency_ms=5,
            model="test-model",
        )
    )
    agent = SynthesisAgent(llm, max_retries=0)
    board = _full_board()
    agg = Aggregator().aggregate(board)
    grade, resp, risk = await agent.synthesize(sample_candidate, board, agg)
    assert grade.score == 80
    assert grade.verdict == "pass"
    assert resp.model == "test-model"
    assert risk.recommended_position_size <= 0.9


@pytest.mark.asyncio
async def test_synthesis_parses_fenced_json(sample_candidate: Candidate):
    raw = f"Here you go:\n```json\n{_valid_synthesis_json()}\n```"
    llm = AsyncMock()
    llm.complete = AsyncMock(
        return_value=LLMResponse(
            text=raw,
            input_tokens=1,
            output_tokens=1,
            latency_ms=1,
            model="m",
        )
    )
    agent = SynthesisAgent(llm, max_retries=0)
    board = _full_board()
    grade, _, _ = await agent.synthesize(sample_candidate, board, Aggregator().aggregate(board))
    assert grade.score == 80


@pytest.mark.asyncio
async def test_synthesis_verdict_overridden_by_final_score(sample_candidate: Candidate):
    """LLM says pass but deterministic score < 70 forces fail."""
    llm = AsyncMock()
    llm.complete = AsyncMock(
        return_value=LLMResponse(
            text=_valid_synthesis_json(score=55, verdict="pass"),
            input_tokens=1,
            output_tokens=1,
            latency_ms=1,
            model="m",
        )
    )
    agent = SynthesisAgent(llm, max_retries=0)
    board = _full_board()
    grade, _, _ = await agent.synthesize(sample_candidate, board, Aggregator().aggregate(board))
    assert grade.score == 55
    assert grade.verdict == "fail"


@pytest.mark.asyncio
async def test_synthesis_invalid_json_retries_then_raises(sample_candidate: Candidate):
    llm = AsyncMock()
    llm.complete = AsyncMock(
        return_value=LLMResponse(
            text="not json at all {{{",
            input_tokens=1,
            output_tokens=1,
            latency_ms=1,
            model="m",
        )
    )
    agent = SynthesisAgent(llm, max_retries=1)
    board = _full_board()
    with pytest.raises(SynthesisParseError):
        await agent.synthesize(sample_candidate, board, Aggregator().aggregate(board))
    assert llm.complete.await_count == 2


@pytest.mark.asyncio
async def test_synthesis_missing_required_field_retries(sample_candidate: Candidate):
    bad = json.dumps({"verdict": "pass"})
    good = _valid_synthesis_json()
    llm = AsyncMock()
    llm.complete = AsyncMock(
        side_effect=[
            LLMResponse(text=bad, input_tokens=1, output_tokens=1, latency_ms=1, model="m"),
            LLMResponse(text=good, input_tokens=1, output_tokens=1, latency_ms=1, model="m"),
        ]
    )
    agent = SynthesisAgent(llm, max_retries=2)
    board = _full_board()
    grade, _, _ = await agent.synthesize(sample_candidate, board, Aggregator().aggregate(board))
    assert grade.score == 80


@pytest.mark.asyncio
async def test_synthesis_clamps_extreme_score_in_payload(sample_candidate: Candidate):
    payload = json.dumps(
        {
            "score": 500,
            "verdict": "pass",
            "rationale": "x",
            "position_size_modifier": 1.0,
        }
    )
    llm = AsyncMock()
    llm.complete = AsyncMock(
        return_value=LLMResponse(
            text=payload,
            input_tokens=1,
            output_tokens=1,
            latency_ms=1,
            model="m",
        )
    )
    agent = SynthesisAgent(llm, max_retries=0)
    board = _full_board()
    grade, _, _ = await agent.synthesize(sample_candidate, board, Aggregator().aggregate(board))
    assert grade.score <= 100


@pytest.mark.asyncio
async def test_synthesis_clamps_position_modifier_above_one(sample_candidate: Candidate):
    payload = json.dumps(
        {
            "score": 80,
            "verdict": "pass",
            "rationale": "x",
            "position_size_modifier": 5.0,
        }
    )
    llm = AsyncMock()
    llm.complete = AsyncMock(
        return_value=LLMResponse(
            text=payload,
            input_tokens=1,
            output_tokens=1,
            latency_ms=1,
            model="m",
        )
    )
    agent = SynthesisAgent(llm, max_retries=0)
    risk = RiskConvictionScore(
        agent="risk_analyst",
        score=60,
        rationale="r",
        signals=[],
        recommended_position_size=0.5,
        recommended_stop_loss_pct=0.2,
        max_entry_spread_pct=0.01,
    )
    board = _full_board(risk_analyst=risk)
    _, _, rp = await agent.synthesize(sample_candidate, board, Aggregator().aggregate(board))
    assert rp.recommended_position_size == 0.5


# --- Score constraints (5) ---


def test_constraint_critical_flow_risk_caps_at_65():
    board = _full_board(
        flow_analyst=_sub("flow_analyst", 80),
        risk_analyst=_sub("risk_analyst", 30),
    )
    assert apply_synthesis_constraints(90, board) == 65


def test_constraint_vol_and_risk_low_caps_at_65():
    # Use 35–39 so vol/risk are both <40 but not counted toward the "2+ below 35" cap.
    board = _full_board(
        volatility_analyst=_sub("volatility_analyst", 39),
        risk_analyst=_sub("risk_analyst", 38),
    )
    assert apply_synthesis_constraints(88, board) == 65


def test_constraint_two_agents_below_35_caps_at_55():
    board = _full_board(
        sentiment_analyst=_sub("sentiment_analyst", 30),
        sector_analyst=_sub("sector_analyst", 30),
    )
    assert apply_synthesis_constraints(90, board) == 55


def test_constraint_passthrough_without_triggers():
    board = _full_board()
    assert apply_synthesis_constraints(72, board) == 72


def test_constraint_combined_caps_use_minimum():
    board = _full_board(
        flow_analyst=_sub("flow_analyst", 80),
        risk_analyst=_sub("risk_analyst", 30),
        sentiment_analyst=_sub("sentiment_analyst", 30),
        sector_analyst=_sub("sector_analyst", 30),
    )
    # critical cap 65 and multi-low 55
    assert apply_synthesis_constraints(90, board) == 55


# --- Integration async (8) ---


def _mock_gate3_agents() -> tuple[AsyncMock, AsyncMock, AsyncMock]:
    sentiment = AsyncMock()
    sentiment.score = AsyncMock(return_value=_sub("sentiment_analyst", 70))
    insider = AsyncMock()
    insider.score = AsyncMock(return_value=_sub("insider_tracker", 70))
    sector = AsyncMock()
    sector.score = AsyncMock(return_value=_sub("sector_analyst", 70))
    return sentiment, insider, sector


@pytest.mark.asyncio
async def test_gate3_full_flow_passes_threshold(
    sample_candidate: Candidate, tmp_path, monkeypatch
):
    monkeypatch.setattr("shared.db.DB_PATH", tmp_path / "g3.db")
    llm = AsyncMock()
    llm.complete = AsyncMock(
        return_value=LLMResponse(
            text=_valid_synthesis_json(score=85),
            input_tokens=10,
            output_tokens=10,
            latency_ms=12,
            model="claude-test",
        )
    )
    syn = SynthesisAgent(llm, max_retries=0)
    flow = _sub("flow_analyst", 70)
    vol = _sub("volatility_analyst", 70)
    risk = RiskConvictionScore(
        agent="risk_analyst",
        score=70,
        rationale="r",
        signals=[],
        recommended_position_size=0.5,
        recommended_stop_loss_pct=0.2,
        max_entry_spread_pct=0.02,
    )
    sentiment, insider, sector = _mock_gate3_agents()

    st = await run_gate3(
        sample_candidate,
        flow,
        vol,
        risk,
        sentiment=sentiment,
        insider=insider,
        sector=sector,
        synthesis_agent=syn,
        aggregator=Aggregator(),
        final_threshold=70,
    )
    assert st is not None
    assert st.grade is not None
    assert st.grade.score >= 70
    assert st.risk is not None
    assert st.risk.recommended_position_size <= 0.5


@pytest.mark.asyncio
async def test_gate3_fails_below_threshold_returns_none(
    sample_candidate: Candidate, tmp_path, monkeypatch
):
    monkeypatch.setattr("shared.db.DB_PATH", tmp_path / "g3b.db")
    llm = AsyncMock()
    llm.complete = AsyncMock(
        return_value=LLMResponse(
            text=_valid_synthesis_json(score=55, verdict="fail"),
            input_tokens=1,
            output_tokens=1,
            latency_ms=1,
            model="m",
        )
    )
    syn = SynthesisAgent(llm, max_retries=0)
    sentiment, insider, sector = _mock_gate3_agents()

    st = await run_gate3(
        sample_candidate,
        _sub("flow_analyst", 70),
        _sub("volatility_analyst", 70),
        RiskConvictionScore(
            agent="risk_analyst",
            score=70,
            rationale="r",
            signals=[],
            recommended_position_size=0.5,
            recommended_stop_loss_pct=0.2,
            max_entry_spread_pct=0.02,
        ),
        sentiment=sentiment,
        insider=insider,
        sector=sector,
        synthesis_agent=syn,
        aggregator=Aggregator(),
        final_threshold=70,
    )
    assert st is None


@pytest.mark.asyncio
async def test_gate3_synthesis_parse_failure_returns_none(
    sample_candidate: Candidate, tmp_path, monkeypatch
):
    monkeypatch.setattr("shared.db.DB_PATH", tmp_path / "g3c.db")
    llm = AsyncMock()
    llm.complete = AsyncMock(
        return_value=LLMResponse(
            text="not valid json {{{",
            input_tokens=1,
            output_tokens=1,
            latency_ms=1,
            model="m",
        )
    )
    syn = SynthesisAgent(llm, max_retries=0)
    sentiment, insider, sector = _mock_gate3_agents()

    st = await run_gate3(
        sample_candidate,
        _sub("flow_analyst", 70),
        _sub("volatility_analyst", 70),
        RiskConvictionScore(
            agent="risk_analyst",
            score=70,
            rationale="r",
            signals=[],
            recommended_position_size=0.5,
            recommended_stop_loss_pct=0.2,
            max_entry_spread_pct=0.02,
        ),
        sentiment=sentiment,
        insider=insider,
        sector=sector,
        synthesis_agent=syn,
        aggregator=Aggregator(),
        final_threshold=70,
    )
    assert st is None


@pytest.mark.asyncio
async def test_gate3_synthesis_retry_succeeds_on_second_llm_call(
    sample_candidate: Candidate, tmp_path, monkeypatch
):
    monkeypatch.setattr("shared.db.DB_PATH", tmp_path / "g3d.db")
    llm = AsyncMock()
    llm.complete = AsyncMock(
        side_effect=[
            LLMResponse(text="garbage", input_tokens=1, output_tokens=1, latency_ms=1, model="m"),
            LLMResponse(
                text=_valid_synthesis_json(score=88),
                input_tokens=1,
                output_tokens=1,
                latency_ms=1,
                model="m",
            ),
        ]
    )
    syn = SynthesisAgent(llm, max_retries=2)
    sentiment, insider, sector = _mock_gate3_agents()

    st = await run_gate3(
        sample_candidate,
        _sub("flow_analyst", 70),
        _sub("volatility_analyst", 70),
        RiskConvictionScore(
            agent="risk_analyst",
            score=70,
            rationale="r",
            signals=[],
            recommended_position_size=0.5,
            recommended_stop_loss_pct=0.2,
            max_entry_spread_pct=0.02,
        ),
        sentiment=sentiment,
        insider=insider,
        sector=sector,
        synthesis_agent=syn,
        aggregator=Aggregator(),
        final_threshold=70,
    )
    assert st is not None
    assert st.grade.score == 88
    assert llm.complete.await_count == 2


@pytest.mark.asyncio
async def test_gate3_constraint_caps_score_below_pass_threshold(
    sample_candidate: Candidate, tmp_path, monkeypatch
):
    """LLM wants 90 but flow/risk critical cap forces 65 → filtered at threshold 70."""
    monkeypatch.setattr("shared.db.DB_PATH", tmp_path / "g3e.db")
    llm = AsyncMock()
    llm.complete = AsyncMock(
        return_value=LLMResponse(
            text=_valid_synthesis_json(score=90, verdict="pass"),
            input_tokens=1,
            output_tokens=1,
            latency_ms=1,
            model="m",
        )
    )
    syn = SynthesisAgent(llm, max_retries=0)
    sentiment, insider, sector = _mock_gate3_agents()

    st = await run_gate3(
        sample_candidate,
        _sub("flow_analyst", 80),
        _sub("volatility_analyst", 70),
        RiskConvictionScore(
            agent="risk_analyst",
            score=30,
            rationale="r",
            signals=[],
            recommended_position_size=0.5,
            recommended_stop_loss_pct=0.2,
            max_entry_spread_pct=0.02,
        ),
        sentiment=sentiment,
        insider=insider,
        sector=sector,
        synthesis_agent=syn,
        aggregator=Aggregator(),
        final_threshold=70,
    )
    assert st is None


@pytest.mark.asyncio
async def test_synthesis_integration_applies_multi_low_cap(
    sample_candidate: Candidate,
):
    llm = AsyncMock()
    llm.complete = AsyncMock(
        return_value=LLMResponse(
            text=_valid_synthesis_json(score=90, verdict="pass"),
            input_tokens=1,
            output_tokens=1,
            latency_ms=1,
            model="m",
        )
    )
    agent = SynthesisAgent(llm, max_retries=0)
    board = _full_board(
        sentiment_analyst=_sub("sentiment_analyst", 30),
        sector_analyst=_sub("sector_analyst", 30),
    )
    grade, _, _ = await agent.synthesize(
        sample_candidate, board, Aggregator().aggregate(board)
    )
    assert grade.score == 55
    assert grade.verdict == "fail"


def test_aggregator_custom_weights_change_weighted_average():
    """Custom weights (equal) change aggregate vs default weighting."""
    w = AgentWeights(
        flow_analyst=1 / 6,
        volatility_analyst=1 / 6,
        risk_analyst=1 / 6,
        sentiment_analyst=1 / 6,
        insider_tracker=1 / 6,
        sector_analyst=1 / 6,
    )
    board = _full_board(
        flow_analyst=_sub("flow_analyst", 100),
        volatility_analyst=_sub("volatility_analyst", 0),
        risk_analyst=_sub("risk_analyst", 0),
        sentiment_analyst=_sub("sentiment_analyst", 0),
        insider_tracker=_sub("insider_tracker", 0),
        sector_analyst=_sub("sector_analyst", 0),
    )
    r_default = Aggregator().aggregate(board)
    r_equal = Aggregator(weights=w).aggregate(board)
    assert r_default.weighted_average != r_equal.weighted_average
    assert r_equal.weighted_average == 100 / 6


@pytest.mark.asyncio
async def test_gate3_llm_specialist_failure_yields_skipped_neutral(
    sample_candidate: Candidate, tmp_path, monkeypatch
):
    """One specialist raises; gate3 substitutes skipped score=50 and still completes."""
    monkeypatch.setattr("shared.db.DB_PATH", tmp_path / "g3f.db")
    llm = AsyncMock()
    llm.complete = AsyncMock(
        return_value=LLMResponse(
            text=_valid_synthesis_json(score=85),
            input_tokens=1,
            output_tokens=1,
            latency_ms=1,
            model="m",
        )
    )
    syn = SynthesisAgent(llm, max_retries=0)

    sentiment = AsyncMock()
    sentiment.score = AsyncMock(side_effect=RuntimeError("upstream"))
    insider = AsyncMock()
    insider.score = AsyncMock(return_value=_sub("insider_tracker", 70))
    sector = AsyncMock()
    sector.score = AsyncMock(return_value=_sub("sector_analyst", 70))

    st = await run_gate3(
        sample_candidate,
        _sub("flow_analyst", 70),
        _sub("volatility_analyst", 70),
        RiskConvictionScore(
            agent="risk_analyst",
            score=70,
            rationale="r",
            signals=[],
            recommended_position_size=0.5,
            recommended_stop_loss_pct=0.2,
            max_entry_spread_pct=0.02,
        ),
        sentiment=sentiment,
        insider=insider,
        sector=sector,
        synthesis_agent=syn,
        aggregator=Aggregator(),
        final_threshold=70,
    )
    assert st is not None
    assert sentiment.score.await_count == 1
