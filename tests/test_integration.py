"""Integration tests: client fetches all endpoints and parses into models."""
import httpx
import pytest
import respx

from scanner.client.rate_limiter import RateLimiter
from scanner.client.uw_client import UWClient
from scanner.models.flow_alert import FlowAlert
from scanner.models.dark_pool import DarkPoolPrint
from scanner.models.market_tide import MarketTide


@respx.mock
@pytest.mark.asyncio
async def test_client_fetches_all_endpoints(flow_fixture, dark_pool_fixture, market_tide_fixture):
    """Client fetches all three endpoints and parses into models."""
    respx.get("https://api.unusualwhales.com/api/option-trades/flow-alerts").mock(
        return_value=httpx.Response(200, json=flow_fixture)
    )
    respx.get("https://api.unusualwhales.com/api/darkpool/recent").mock(
        return_value=httpx.Response(200, json=dark_pool_fixture)
    )
    respx.get("https://api.unusualwhales.com/api/market/market-tide").mock(
        return_value=httpx.Response(200, json=market_tide_fixture)
    )
    limiter = RateLimiter(calls_per_minute=60)
    client = UWClient(api_token="test-token", rate_limiter=limiter)

    alerts = await client.get_flow_alerts()
    prints = await client.get_dark_pool_recent()
    tide = await client.get_market_tide()
    await client.close()

    assert len(alerts) >= 1
    assert all(isinstance(a, FlowAlert) for a in alerts)
    assert len(prints) >= 1
    assert all(isinstance(p, DarkPoolPrint) for p in prints)
    assert isinstance(tide, MarketTide)
