from pydantic import BaseModel


class MarketTide(BaseModel):
    """Market sentiment from /api/market/market-tide.

    API returns net_call_premium and net_put_premium. Positive net call flow
    = bullish, negative = bearish.
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

        net_call = float(item.get("net_call_premium", 0) or 0)
        net_put = float(item.get("net_put_premium", 0) or 0)
        net_premium = item.get("net_premium") or item.get("net_call_put_premium")
        if net_premium is None:
            net_premium = net_call - net_put
        else:
            net_premium = float(net_premium)

        threshold = float(item.get("tide_threshold_ratio", 1_000_000))
        if net_premium > threshold:
            return cls(direction="bullish")
        if net_premium < -threshold:
            return cls(direction="bearish")
        return cls(direction="neutral")
