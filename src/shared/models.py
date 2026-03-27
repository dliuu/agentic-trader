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


class RiskConvictionScore(SubScore):
    """Extended SubScore with conviction breakdown and execution params.

    The risk analyst scores how much structural risk the original buyer
    accepted. Higher risk -> higher conviction signal.
    """

    # --- Factor breakdowns (for transparency / debugging) ---
    premium_commitment_points: int = 0
    time_pressure_points: int = 0
    spread_cost_points: int = 0
    fill_aggression_points: int = 0
    strike_distance_points: int = 0
    move_ratio_points: int = 0
    liquidity_cost_points: int = 0
    earnings_modifier: int = 0

    # --- Computed intermediate values ---
    spread_pct: float | None = None
    otm_pct: float | None = None
    move_ratio: float | None = None
    theta_daily_pct: float | None = None
    days_to_expiry: int | None = None

    # --- Human-readable conviction signals ---
    conviction_signals: list[str] = Field(default_factory=list)

    # --- Execution parameters for Agent C ---
    recommended_position_size: float = 0.0
    recommended_stop_loss_pct: float = 0.0
    max_entry_spread_pct: float = 0.0

    # --- Flags ---
    untradeable: bool = False
    data_gaps: list[str] = Field(default_factory=list)
