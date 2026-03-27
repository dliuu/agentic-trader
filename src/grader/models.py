"""Grader data models — context sent to the LLM, validated response, and scored trade output."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

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

    @field_validator("verdict", mode="before")
    @classmethod
    def verdict_valid(cls, v: str) -> str:
        # Safety net: normalize any free-form Claude verdicts.
        from grader.parser import normalize_verdict

        v = normalize_verdict(v)
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


class NewsHeadline(BaseModel):
    """Single news headline from UW."""

    title: str
    source: str
    published_at: datetime
    tickers: list[str] = Field(default_factory=list)


class NewsBuzz(BaseModel):
    """Finnhub buzz metrics — measures attention level."""

    articles_last_week: int = 0
    weekly_average: float = 0.0
    buzz_ratio: float = 0.0
    bullish_pct: float = 0.0
    bearish_pct: float = 0.0
    news_score: float = 0.0


class RedditPresence(BaseModel):
    """Ticker mention presence across a single subreddit."""

    subreddit: str
    post_count: int = 0
    top_post_title: str | None = None
    top_post_score: int = 0
    searched_at: datetime = Field(default_factory=datetime.utcnow)


class RedditSummary(BaseModel):
    """Aggregated Reddit presence across all trading subreddits."""

    total_subreddits_with_mentions: int = 0
    total_post_count: int = 0
    subreddits: list[RedditPresence] = Field(default_factory=list)
    is_meme_candidate: bool = False
    is_crowded: bool = False


class SentimentContext(BaseModel):
    """Complete sentiment context passed to the sentiment analyst LLM."""

    ticker: str
    option_type: str
    trade_direction: str

    headline_count_48h: int = 0
    headlines: list[NewsHeadline] = Field(default_factory=list)
    buzz: NewsBuzz = Field(default_factory=NewsBuzz)
    reddit: RedditSummary = Field(default_factory=RedditSummary)

    has_catalyst: bool = False
    is_quiet: bool = False
    news_aligns_with_direction: bool | None = None


class SentimentGrade(BaseModel):
    """Structured response model for the sentiment analyst LLM."""

    score: int = Field(ge=1, le=100, description="1-100 sentiment score")
    verdict: Literal["pass", "fail"]
    rationale: str = Field(max_length=500)
    signals_confirmed: list[str] = Field(default_factory=list)
    risk_factors: list[str] = Field(default_factory=list)
    crowd_exposure: Literal["none", "low", "moderate", "high"] = "none"
