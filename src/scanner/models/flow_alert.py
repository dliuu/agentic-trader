from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from datetime import datetime


class FlowAlert(BaseModel):
    """Raw flow alert from /api/option-trades/flow-alerts.

    Field names match the UW API response. We keep the raw
    representation and derive computed fields as properties.
    """

    model_config = ConfigDict(populate_by_name=True)

    id: str
    ticker: str = Field(alias="ticker_symbol")
    type: str  # "Calls" or "Puts"
    strike: float
    expiry: str  # "YYYY-MM-DD"
    total_premium: float
    total_size: int
    open_interest: int | None = None
    implied_volatility: float | None = None
    underlying_price: float | None = None
    execution_type: str | None = None  # "Sweep", "Block", "Split"
    is_otm: bool = False
    created_at: datetime

    @property
    def direction(self) -> str:
        return "bullish" if self.type == "Calls" else "bearish"

    @property
    def dte(self) -> int:
        from datetime import date

        exp = date.fromisoformat(self.expiry)
        return (exp - date.today()).days

    @property
    def otm_percentage(self) -> float | None:
        if self.underlying_price and self.underlying_price > 0:
            return abs(self.strike - self.underlying_price) / self.underlying_price * 100
        return None

    @property
    def volume_oi_ratio(self) -> float | None:
        if self.open_interest and self.open_interest > 0:
            return self.total_size / self.open_interest
        return None
