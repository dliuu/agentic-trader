"""Grader data models — context sent to the LLM, validated response, and scored trade output."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, field_validator

from shared.models import Candidate


class Greeks(BaseModel):
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    iv: float | None = None


class NewsItem(BaseModel):
    headline: str
    source: str
    published_at: datetime


class InsiderTrade(BaseModel):
    name: str
    title: str | None = None
    trade_type: str  # "buy" or "sell"
    shares: int
    value: float
    filed_at: datetime


class GradingContext(BaseModel):
    """Everything the LLM needs to grade a candidate."""

    candidate: Candidate
    current_spot: float
    daily_volume: int
    avg_daily_volume: int | None = None
    greeks: Greeks | None = None
    recent_news: list[NewsItem] = []
    insider_trades: list[InsiderTrade] = []
    congressional_trades: list[InsiderTrade] = []
    sector: str | None = None
    market_cap: float | None = None


class GradeResponse(BaseModel):
    """Validated LLM output. This is the JSON schema included in the prompt."""

    score: int  # 1–100
    verdict: str  # "pass" or "fail"
    rationale: str  # 2–4 sentence explanation
    signals_confirmed: list[str]  # Which of the scanner's signals the LLM agrees with
    risk_factors: list[str] = []  # Concerns the LLM flagged
    likely_directional: bool  # LLM's judgment: directional bet vs hedge

    @field_validator("score")
    @classmethod
    def score_in_range(cls, v: int) -> int:
        if not 1 <= v <= 100:
            raise ValueError(f"Score must be 1–100, got {v}")
        return v

    @field_validator("verdict")
    @classmethod
    def verdict_valid(cls, v: str) -> str:
        if v not in ("pass", "fail"):
            raise ValueError(f"Verdict must be 'pass' or 'fail', got {v}")
        return v


class ScoredTrade(BaseModel):
    """A candidate that passed grading. Sent to the executor queue."""

    candidate: Candidate
    grade: GradeResponse | None = None  # None in pass-through mode when grading disabled
    graded_at: datetime
    model_used: str
    latency_ms: int
    input_tokens: int
    output_tokens: int
