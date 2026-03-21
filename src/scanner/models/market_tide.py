from pydantic import BaseModel


class MarketTide(BaseModel):
    """Market sentiment from /api/market/market-tide.

    Net call vs put premium indicates bullish/bearish/neutral.
    """

    direction: str  # "bullish", "bearish", "neutral"

    @classmethod
    def from_raw(cls, data: list | dict) -> "MarketTide":
        """Parse UW API market-tide response into direction."""
        if isinstance(data, dict):
            data = data.get("data", data)
        if isinstance(data, list) and data:
            item = data[0]
        elif isinstance(data, dict):
            item = data
        else:
            return cls(direction="neutral")

        net_premium = item.get("net_premium") or item.get("net_call_put_premium") or 0
        threshold = item.get("tide_threshold_ratio", 1.5)
        if net_premium > threshold:
            return cls(direction="bullish")
        if net_premium < -threshold:
            return cls(direction="bearish")
        return cls(direction="neutral")
