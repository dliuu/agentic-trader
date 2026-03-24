from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


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

    scanned_at: datetime = Field(default_factory=datetime.utcnow)
    raw_alert_id: str
