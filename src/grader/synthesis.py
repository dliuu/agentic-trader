"""Final synthesis LLM step: aggregate sub-scores into one GradeResponse + risk params."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import structlog
from pydantic import BaseModel, field_validator

from shared.models import Candidate, SubScore

from grader.aggregator import AggregatedResult
from grader.llm_client import LLMClient, LLMResponse
from grader.models import GradeResponse, TradeRiskParams
from grader.parser import normalize_verdict
from grader.synthesis_prompt import build_synthesis_system_prompt, build_synthesis_user_message

log = structlog.get_logger()


class SynthesisParseError(Exception):
    """Raised when the synthesis LLM output cannot be parsed after all retries."""


class SynthesisLLMOutput(BaseModel):
    """JSON schema returned by the synthesis model (before deterministic caps)."""

    model_config = {"extra": "ignore"}

    score: int
    verdict: str
    confidence: str = "medium"
    rationale: str = ""
    conflict_resolution: str = ""
    key_signal: str = ""
    position_size_modifier: float = 1.0

    @field_validator("score")
    @classmethod
    def clamp_score(cls, v: int) -> int:
        return max(1, min(100, int(v)))

    @field_validator("position_size_modifier", mode="before")
    @classmethod
    def clamp_modifier(cls, v: object) -> float:
        try:
            x = float(v)
        except (TypeError, ValueError):
            return 1.0
        return max(0.0, min(1.0, x))


def apply_synthesis_constraints(score: int, sub_scores: dict[str, SubScore]) -> int:
    """Deterministic caps per synthesis design (after LLM suggestion)."""
    s = score
    flow = sub_scores.get("flow_analyst")
    risk = sub_scores.get("risk_analyst")
    vol = sub_scores.get("volatility_analyst")

    if flow and not flow.skipped and risk and not risk.skipped:
        if flow.score >= 75 and risk.score < 40:
            s = min(s, 65)
    if vol and not vol.skipped and risk and not risk.skipped:
        if vol.score < 40 and risk.score < 40:
            s = min(s, 65)

    low = sum(1 for sc in sub_scores.values() if not sc.skipped and sc.score < 35)
    if low >= 2:
        s = min(s, 55)

    return max(1, min(100, s))


def _parse_synthesis_json(text: str) -> SynthesisLLMOutput:
    from grader.parser import _extract_json

    cleaned = _extract_json(text)
    data = json.loads(cleaned)
    if not isinstance(data, dict):
        raise ValueError("expected JSON object")
    if "verdict" in data:
        data["verdict"] = normalize_verdict(str(data["verdict"]))
    return SynthesisLLMOutput.model_validate(data)


class SynthesisAgent:
    """Single Claude call to produce the final trade score."""

    def __init__(
        self,
        llm_client: LLMClient,
        max_retries: int = 2,
        max_tokens: int | None = None,
    ) -> None:
        self._llm = llm_client
        self._max_retries = max_retries
        self._max_tokens = max_tokens

    async def synthesize(
        self,
        candidate: Candidate,
        sub_scores: dict[str, SubScore],
        aggregated: AggregatedResult,
    ) -> tuple[GradeResponse, LLMResponse, TradeRiskParams]:
        system = build_synthesis_system_prompt()
        user = build_synthesis_user_message(candidate, sub_scores, aggregated)

        last_err: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                llm_resp = await self._llm.complete(
                    system,
                    user,
                    max_tokens=self._max_tokens,
                )
                raw = _parse_synthesis_json(llm_resp.text)
                constrained = apply_synthesis_constraints(raw.score, sub_scores)
                if constrained != raw.score:
                    log.warning(
                        "synthesis.score_capped",
                        ticker=candidate.ticker,
                        raw_score=raw.score,
                        capped_to=constrained,
                    )
                verdict = "pass" if constrained >= 70 else "fail"

                risk_src = aggregated.risk_score
                mod = float(raw.position_size_modifier)
                mod = max(0.0, min(1.0, mod))
                if risk_src is not None:
                    pos = min(mod, float(risk_src.recommended_position_size))
                    risk_params = TradeRiskParams(
                        recommended_position_size=pos,
                        recommended_stop_loss_pct=float(risk_src.recommended_stop_loss_pct),
                        max_entry_spread_pct=float(risk_src.max_entry_spread_pct),
                    )
                else:
                    risk_params = TradeRiskParams(
                        recommended_position_size=mod,
                        recommended_stop_loss_pct=0.0,
                        max_entry_spread_pct=0.0,
                    )

                grade = GradeResponse(
                    score=constrained,
                    verdict=verdict,
                    rationale=raw.rationale or "(no rationale)",
                    signals_confirmed=[raw.key_signal] if raw.key_signal else [],
                    risk_factors=[],
                    likely_directional=True,
                    confidence=raw.confidence,
                    conflict_resolution=raw.conflict_resolution or None,
                    key_signal=raw.key_signal or None,
                    position_size_modifier=raw.position_size_modifier,
                )
                log.info(
                    "synthesis.complete",
                    ticker=candidate.ticker,
                    score=grade.score,
                    verdict=grade.verdict,
                    confidence=grade.confidence,
                    key_signal=grade.key_signal,
                    tokens=llm_resp.input_tokens + llm_resp.output_tokens,
                    latency_ms=llm_resp.latency_ms,
                )
                return grade, llm_resp, risk_params
            except Exception as e:
                last_err = e
                log.warning(
                    "synthesis.parse_retry",
                    attempt=attempt + 1,
                    error=str(e),
                    ticker=candidate.ticker,
                )

        raise SynthesisParseError(f"Synthesis parsing failed after retries: {last_err}") from last_err


async def log_synthesis_grade(
    candidate: Candidate,
    grade: GradeResponse,
    llm_resp: LLMResponse,
) -> None:
    """Persist synthesis outcome to the grades table (same shape as legacy grader)."""
    import json as json_lib

    from shared.db import get_db

    db = await get_db()
    try:
        await db.execute(
            """INSERT INTO grades
               (id, candidate_id, score, verdict, rationale,
                signals_confirmed, model, input_tokens,
                output_tokens, latency_ms, graded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(uuid.uuid4()),
                candidate.id,
                grade.score,
                grade.verdict,
                grade.rationale,
                json_lib.dumps(grade.signals_confirmed),
                llm_resp.model,
                llm_resp.input_tokens,
                llm_resp.output_tokens,
                llm_resp.latency_ms,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await db.commit()
    finally:
        await db.close()
