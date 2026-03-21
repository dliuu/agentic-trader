import pytest

from scanner.client.rate_limiter import RateLimiter
from scanner.models.flow_alert import FlowAlert


def test_rate_limiter_acquires():
    limiter = RateLimiter(calls_per_minute=60)
    # Should not block when well under limit
    import asyncio
    async def run():
        await limiter.acquire()
        await limiter.acquire()
    asyncio.run(run())


def test_flow_alert_from_api_format():
    """FlowAlert validates against API response shape."""
    data = {
        "id": "api-1",
        "ticker_symbol": "TEST",
        "type": "Calls",
        "strike": 100.0,
        "expiry": "2026-04-18",
        "total_premium": 30000.0,
        "total_size": 100,
        "open_interest": 50,
        "underlying_price": 95.0,
        "execution_type": "Sweep",
        "is_otm": True,
        "created_at": "2026-03-20T15:00:00Z",
    }
    alert = FlowAlert.model_validate(data)
    assert alert.ticker == "TEST"
    assert alert.direction == "bullish"
