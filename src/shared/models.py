from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, field_validator


class OptionType(str, Enum):
    CALL = "call"
    PUT = "put"


class FillType(str, Enum):
    SWEEP = "sweep"
    BLOCK = "block"
    SPLIT = "split"


class SignalMatch(BaseModel):
    """One matched rule with its details."""

    rule_name: str
    weight: float
    detail: str


class Candidate(BaseModel):
    """Output of Agent A. This is what Agent B receives."""

    model_config = {"extra": "forbid"}

    id: str
    source: str
    ticker: str
    direction: str
    strike: float
    expiry: str
    premium_usd: float
    underlying_price: float | None
    implied_volatility: float | None
    execution_type: str | None
    dte: int

    signals: list[SignalMatch]
    confluence_score: float
    dark_pool_confirmation: bool = False
    market_tide_aligned: bool = False

    # Optional flow-analyst inputs (populated from FlowAlert when available)
    volume: int = 0
    open_interest: int = 0
    oi_change: float | None = None

    scanned_at: datetime = Field(default_factory=datetime.utcnow)
    raw_alert_id: str


class FlowCandidate(BaseModel):
    """Normalized option flow row for deterministic Gate 1 scoring."""

    model_config = {"extra": "forbid"}

    id: str
    ticker: str
    strike: float
    expiry: datetime
    option_type: OptionType
    fill_type: FillType
    premium: float
    spot_price: float
    volume: int
    open_interest: int
    oi_change: float | None = None
    confluence_score: int
    signals: list[str]
    scanned_at: datetime
    raw_data: dict = Field(default_factory=dict)


class SubScore(BaseModel):
    """Score output from any single agent."""

    model_config = {"extra": "forbid"}

    agent: str
    score: int
    rationale: str
    signals: list[str]
    skipped: bool = False
    skip_reason: str | None = None

    @field_validator("score")
    @classmethod
    def clamp_score(cls, v: int) -> int:
        return max(0, min(100, v))
