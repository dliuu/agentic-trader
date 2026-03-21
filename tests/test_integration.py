import json
from pathlib import Path

import httpx
import pytest
import respx


@pytest.fixture
def flow_fixture():
    fixture_path = Path(__file__).parent / "fixtures" / "flow_alerts_sample.json"
    return json.loads(fixture_path.read_text())


@pytest.fixture
def dark_pool_fixture():
    fixture_path = Path(__file__).parent / "fixtures" / "dark_pool_sample.json"
    return json.loads(fixture_path.read_text())


@respx.mock
@pytest.mark.asyncio
async def test_mocked_flow_alerts(flow_fixture):
    """API response shape matches FlowAlert model."""
    respx.get("https://api.unusualwhales.com/api/option-trades/flow-alerts").mock(
        return_value=httpx.Response(200, json=flow_fixture)
    )
    from scanner.client.uw_client import UWClient
    from scanner.client.rate_limiter import RateLimiter

    limiter = RateLimiter(calls_per_minute=60)
    client = UWClient(api_token="test-token", rate_limiter=limiter)
    alerts = await client.get_flow_alerts()
    await client.close()
    assert len(alerts) >= 1
    assert alerts[0].ticker == "ACME"


@respx.mock
@pytest.mark.asyncio
async def test_mocked_dark_pool(dark_pool_fixture):
    respx.get("https://api.unusualwhales.com/api/darkpool/recent").mock(
        return_value=httpx.Response(200, json=dark_pool_fixture)
    )
    from scanner.client.uw_client import UWClient
    from scanner.client.rate_limiter import RateLimiter

    limiter = RateLimiter(calls_per_minute=60)
    client = UWClient(api_token="test-token", rate_limiter=limiter)
    prints = await client.get_dark_pool_recent()
    await client.close()
    assert len(prints) >= 1
