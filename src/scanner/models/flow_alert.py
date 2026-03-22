from __future__ import annotations

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator
from datetime import datetime


class FlowAlert(BaseModel):
    """Raw flow alert from /api/option-trades/flow-alerts.

    Field names/aliases match the UW API response. API returns ticker (not
    ticker_symbol), type as "put"/"call", numeric strings, iv_start/iv_end,
    has_sweep/has_floor instead of execution_type.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str
    ticker: str = Field(validation_alias=AliasChoices("ticker", "ticker_symbol"))
    type: str  # API: "put"/"call"; normalized to "Calls"/"Puts" for direction
    strike: float  # API may send string, coerced
    expiry: str  # "YYYY-MM-DD"
    total_premium: float  # API may send string, coerced
    total_size: int
    open_interest: int | None = None
    implied_volatility: float | None = Field(
        default=None,
        validation_alias=AliasChoices("iv_start", "implied_volatility"),
    )
    underlying_price: float | None = None  # API may send string, coerced
    execution_type: str | None = None  # "Sweep", "Block"; derived from has_sweep/has_floor
    is_otm: bool = False
    has_sweep: bool = False
    has_floor: bool = False
    created_at: datetime

    @field_validator("type", mode="before")
    @classmethod
    def _normalize_type(cls, v: str) -> str:
        if not v:
            return "Puts"
        v = str(v).lower()
        return "Calls" if v in ("call", "calls") else "Puts"

    @model_validator(mode="after")
    def _derive_execution_type(self) -> "FlowAlert":
        if self.execution_type is None:
            if self.has_sweep:
                object.__setattr__(self, "execution_type", "Sweep")
            elif self.has_floor:
                object.__setattr__(self, "execution_type", "Block")
        return self

    @property
    def direction(self) -> str:
        return "bullish" if self.type == "Calls" else "bearish"

    @property
    def dte(self) -> int:
        from datetime import date

        try:
            exp = date.fromisoformat(self.expiry)
            return (exp - date.today()).days
        except (ValueError, TypeError):
            return -1

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
