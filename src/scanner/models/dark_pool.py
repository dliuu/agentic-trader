from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from datetime import datetime


class DarkPoolPrint(BaseModel):
    """Dark pool print from /api/darkpool/recent or /api/darkpool/{ticker}.

    Field names match the UW API response. Supports ticker_symbol/ticker,
    cost/notional, execution_time/executed_at.
    """

    model_config = ConfigDict(populate_by_name=True)

    ticker: str = Field(default="", alias="ticker_symbol")
    notional: float = Field(default=0, alias="cost")
    executed_at: datetime = Field(default_factory=datetime.utcnow, alias="execution_time")
