import pytest
import httpx
import respx

from scanner.client.rate_limiter import RateLimiter
from scanner.client.uw_client import UWClient
from scanner.models.flow_alert import FlowAlert
from scanner.models.dark_pool import DarkPoolPrint
from scanner.models.market_tide import MarketTide


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


def test_flow_alert_real_api_format():
    """FlowAlert validates against actual UW API response (ticker, put, iv_start, etc)."""
    data = {
        "ticker": "SPXW",
        "option_chain": "SPXW260323P06500000",
        "strike": "6500",
        "total_premium": "177180",
        "total_size": 64,
        "underlying_price": "6490",
        "iv_start": "0.132064420433876",
        "expiry": "2026-03-23",
        "id": "f0c76f8b-9668-4b80-8cb6-1494a1429123",
        "has_sweep": False,
        "has_floor": False,
        "type": "put",
        "open_interest": 2930,
        "created_at": "2026-03-20T20:58:13.941755Z",
    }
    alert = FlowAlert.model_validate(data)
    assert alert.ticker == "SPXW"
    assert alert.type == "Puts"
    assert alert.direction == "bearish"
    assert alert.total_premium == 177180
    assert alert.implied_volatility == 0.132064420433876


def test_dark_pool_real_api_format():
    """DarkPoolPrint validates against actual UW API (ticker, premium, executed_at)."""
    data = {
        "size": 4900,
        "ticker": "SMCI",
        "price": "20.9113",
        "executed_at": "2026-03-20T23:59:59Z",
        "premium": "102465.3700",
    }
    dp = DarkPoolPrint.model_validate(data)
    assert dp.ticker == "SMCI"
    assert dp.notional == 102465.37


def test_market_tide_real_api_format():
    """MarketTide parses net_call_premium and net_put_premium from real API."""
    data = {
        "timestamp": "2026-03-20T09:30:00-04:00",
        "date": "2026-03-20",
        "net_call_premium": "-26877161.0000",
        "net_put_premium": "2143580.0000",
        "net_volume": -73980,
    }
    tide = MarketTide.from_raw(data)
    assert tide.direction == "bearish"


# --- Client integration tests (respx-mocked) ---


@respx.mock
@pytest.mark.asyncio
async def test_client_fetches_flow_alerts(flow_fixture):
    respx.get("https://api.unusualwhales.com/api/option-trades/flow-alerts").mock(
        return_value=httpx.Response(200, json=flow_fixture)
    )
    client = UWClient(api_token="fake", rate_limiter=RateLimiter(calls_per_minute=60))
    alerts = await client.get_flow_alerts()
    await client.close()
    assert len(alerts) > 0
    assert all(isinstance(a, FlowAlert) for a in alerts)
    assert alerts[0].ticker == "ACME"


@respx.mock
@pytest.mark.asyncio
async def test_client_fetches_dark_pool(dark_pool_fixture):
    respx.get("https://api.unusualwhales.com/api/darkpool/recent").mock(
        return_value=httpx.Response(200, json=dark_pool_fixture)
    )
    client = UWClient(api_token="fake", rate_limiter=RateLimiter(calls_per_minute=60))
    prints = await client.get_dark_pool_recent()
    await client.close()
    assert len(prints) > 0
    assert all(isinstance(p, DarkPoolPrint) for p in prints)
    assert prints[0].ticker == "ACME"


@respx.mock
@pytest.mark.asyncio
async def test_client_fetches_market_tide(market_tide_fixture):
    respx.get("https://api.unusualwhales.com/api/market/market-tide").mock(
        return_value=httpx.Response(200, json=market_tide_fixture)
    )
    client = UWClient(api_token="fake", rate_limiter=RateLimiter(calls_per_minute=60))
    tide = await client.get_market_tide()
    await client.close()
    assert isinstance(tide, MarketTide)
    assert tide.direction == "bearish"


@respx.mock
@pytest.mark.asyncio
async def test_client_handles_empty_and_parse_errors_gracefully():
    """Empty data returns empty lists; malformed items are skipped without crashing."""
    # Flow alerts: one valid, one invalid — should return 1 alert
    flow_with_bad_item = {
        "data": [
            {
                "id": "ok-1",
                "ticker": "OK",
                "type": "call",
                "strike": 100,
                "expiry": "2026-04-18",
                "total_premium": 30000,
                "total_size": 50,
                "created_at": "2026-03-20T15:00:00Z",
            },
            {"id": "bad", "ticker": "BAD"},  # missing required fields
        ]
    }
    respx.get("https://api.unusualwhales.com/api/option-trades/flow-alerts").mock(
        return_value=httpx.Response(200, json=flow_with_bad_item)
    )
    client = UWClient(api_token="fake", rate_limiter=RateLimiter(calls_per_minute=60))
    alerts = await client.get_flow_alerts()
    await client.close()
    assert len(alerts) == 1
    assert alerts[0].ticker == "OK"

    # Dark pool: empty data
    respx.get("https://api.unusualwhales.com/api/darkpool/recent").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    client2 = UWClient(api_token="fake", rate_limiter=RateLimiter(calls_per_minute=60))
    prints = await client2.get_dark_pool_recent()
    await client2.close()
    assert prints == []
