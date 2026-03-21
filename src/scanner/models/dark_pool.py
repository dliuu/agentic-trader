from __future__ import annotations

from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from datetime import datetime


class DarkPoolPrint(BaseModel):
    """Dark pool print from /api/darkpool/recent or /api/darkpool/{ticker}.

    API returns: ticker, premium (dollar value), executed_at.
    Aliases support ticker_symbol, cost/notional/premium, execution_time.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    ticker: str = Field(
        default="",
        validation_alias=AliasChoices("ticker", "ticker_symbol"),
    )
    notional: float = Field(
        default=0,
        validation_alias=AliasChoices("premium", "cost", "notional"),
    )
    executed_at: datetime = Field(
        default_factory=datetime.utcnow,
        validation_alias=AliasChoices("executed_at", "execution_time", "timestamp"),
    )
