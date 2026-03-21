from __future__ import annotations

from pydantic import BaseModel, Field
from datetime import datetime


class SignalMatch(BaseModel):
    """One matched rule with its details."""

    rule_name: str
    weight: float
    detail: str  # Human-readable: "OTM 23.5% (strike 180, spot 145.80)"


class Candidate(BaseModel):
    """Output of Agent A. This is what Agent B receives.

    Contains the raw alert data plus the scanner's analysis:
    which rules matched, the confluence score, and metadata.
    """

    model_config = {"extra": "forbid"}

    id: str
    source: str  # "flow_alert" or "dark_pool"
    ticker: str
    direction: str  # "bullish" or "bearish"
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
