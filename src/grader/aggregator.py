"""Deterministic aggregation of six specialist sub-scores for synthesis."""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Literal

from shared.filters import AGENT_WEIGHTS, AgentWeights
from shared.models import RiskConvictionScore, SubScore

AgentAgreement = Literal["strong", "moderate", "weak"]
ConflictSeverity = Literal["critical", "warning"]


@dataclass(frozen=True)
class ConflictRecord:
    """One detected cross-agent conflict pattern."""

    name: str
    severity: ConflictSeverity


@dataclass
class AggregatedResult:
    """Output of the deterministic aggregator before the synthesis LLM."""

    weighted_average: float
    score_stdev: float
    agent_agreement: AgentAgreement
    conflict_flags: list[ConflictRecord] = field(default_factory=list)
    risk_score: RiskConvictionScore | None = None
    skipped_agents: list[str] = field(default_factory=list)


def _weight_map(weights: AgentWeights) -> dict[str, float]:
    return {
        "flow_analyst": weights.flow_analyst,
        "volatility_analyst": weights.volatility_analyst,
        "risk_analyst": weights.risk_analyst,
        "sentiment_analyst": weights.sentiment_analyst,
        "insider_tracker": weights.insider_tracker,
        "sector_analyst": weights.sector_analyst,
    }


def _active_scores(scores: dict[str, SubScore]) -> dict[str, SubScore]:
    return {k: v for k, v in scores.items() if not v.skipped}


def _weighted_average(active: dict[str, SubScore], weights: dict[str, float]) -> float:
    wsum = sum(weights[k] for k in active if k in weights)
    if wsum <= 0:
        return 0.0
    return sum(active[k].score * weights[k] for k in active if k in weights) / wsum


def _population_stdev(values: list[int]) -> float:
    if len(values) <= 1:
        return 0.0
    return float(statistics.pstdev(values))


def _agreement_level(stdev: float) -> AgentAgreement:
    if stdev < 10:
        return "strong"
    if stdev <= 20:
        return "moderate"
    return "weak"


def _detect_conflicts(scores: dict[str, SubScore]) -> list[ConflictRecord]:
    flags: list[ConflictRecord] = []

    def g(name: str) -> SubScore | None:
        return scores.get(name)

    flow = g("flow_analyst")
    risk = g("risk_analyst")
    vol = g("volatility_analyst")
    sentiment = g("sentiment_analyst")
    insider = g("insider_tracker")
    sector = g("sector_analyst")

    if (
        flow
        and not flow.skipped
        and risk
        and not risk.skipped
        and flow.score >= 75
        and risk.score < 40
    ):
        flags.append(ConflictRecord("high_conviction_high_risk", "critical"))

    if (
        flow
        and not flow.skipped
        and sentiment
        and not sentiment.skipped
        and flow.score >= 70
        and sentiment.score < 35
    ):
        flags.append(ConflictRecord("sentiment_contradicts_flow", "warning"))

    if (
        flow
        and not flow.skipped
        and insider
        and not insider.skipped
        and flow.score >= 70
        and insider.score < 30
    ):
        flags.append(ConflictRecord("insider_selling_despite_flow", "warning"))

    if (
        flow
        and not flow.skipped
        and sector
        and not sector.skipped
        and flow.score >= 65
        and sector.score < 35
    ):
        flags.append(ConflictRecord("sector_headwind", "warning"))

    if (
        vol
        and not vol.skipped
        and risk
        and not risk.skipped
        and vol.score < 40
        and risk.score < 40
    ):
        flags.append(ConflictRecord("vol_and_risk_both_low", "critical"))

    active = _active_scores(scores)
    if len(active) >= 5 and all(s.score >= 65 for s in active.values()):
        flags.append(ConflictRecord("unanimous_conviction", "warning"))

    return flags


class Aggregator:
    """Weighted aggregation, spread, and conflict detection across six agents."""

    def __init__(self, weights: AgentWeights | None = None) -> None:
        self._weights = weights or AGENT_WEIGHTS
        self._wmap = _weight_map(self._weights)

    def aggregate(self, scores: dict[str, SubScore]) -> AggregatedResult:
        skipped_agents = [k for k, v in scores.items() if v.skipped]
        active = _active_scores(scores)

        weighted_average = _weighted_average(active, self._wmap)
        values = [s.score for s in active.values()]
        score_stdev = _population_stdev(values)
        agent_agreement = _agreement_level(score_stdev)
        conflict_flags = _detect_conflicts(scores)

        risk_raw = scores.get("risk_analyst")
        risk_score: RiskConvictionScore | None = None
        if isinstance(risk_raw, RiskConvictionScore):
            risk_score = risk_raw

        return AggregatedResult(
            weighted_average=weighted_average,
            score_stdev=score_stdev,
            agent_agreement=agent_agreement,
            conflict_flags=conflict_flags,
            risk_score=risk_score,
            skipped_agents=skipped_agents,
        )
