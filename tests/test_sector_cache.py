import httpx
import pytest
import respx
from datetime import datetime

from src.grader.context.sector_cache import (
    BENCHMARK_TICKERS,
    MARKET_PROXY_TICKER,
    SectorBenchmark,
    SectorBenchmarkCache,
    TickerVolSnapshot,
    _compute_sector_benchmark,
    _extract_iv_rank,
    get_sector_cache,
    refresh_sector_cache,
)


def make_snapshot(
    ticker: str = "AAPL",
    sector: str = "Technology",
    iv_rank: float = 50.0,
    current_iv: float = 0.30,
    rv_20d: float = 0.25,
    iv_rv_ratio: float = 1.2,
) -> TickerVolSnapshot:
    return TickerVolSnapshot(
        ticker=ticker,
        sector=sector,
        iv_rank=iv_rank,
        current_iv=current_iv,
        realized_vol_20d=rv_20d,
        iv_rv_ratio=iv_rv_ratio,
        fetched_at=datetime.utcnow(),
    )


class TestComputeSectorBenchmark:
    def test_median_with_even_count(self):
        """4 tickers → median is avg of middle two."""
        snapshots = [
            make_snapshot("AAPL", iv_rv_ratio=0.9),
            make_snapshot("MSFT", iv_rv_ratio=1.0),
            make_snapshot("NVDA", iv_rv_ratio=1.2),
            make_snapshot("GOOGL", iv_rv_ratio=1.5),
        ]
        bench = _compute_sector_benchmark("Technology", snapshots)
        assert bench.iv_rv_ratio_median == pytest.approx(1.1, abs=0.01)
        assert bench.iv_rv_ratio_p25 == pytest.approx(0.9, abs=0.1)
        assert bench.iv_rv_ratio_p75 == pytest.approx(1.5, abs=0.1)
        assert bench.ticker_count == 4

    def test_median_with_odd_count(self):
        """3 tickers → median is the middle one."""
        snapshots = [
            make_snapshot("XOM", sector="Energy", iv_rv_ratio=0.8),
            make_snapshot("CVX", sector="Energy", iv_rv_ratio=1.1),
            make_snapshot("COP", sector="Energy", iv_rv_ratio=1.4),
        ]
        bench = _compute_sector_benchmark("Energy", snapshots)
        assert bench.iv_rv_ratio_median == pytest.approx(1.1, abs=0.01)

    def test_avg_iv_rank(self):
        snapshots = [
            make_snapshot(iv_rank=20.0),
            make_snapshot(iv_rank=40.0),
            make_snapshot(iv_rank=60.0),
            make_snapshot(iv_rank=80.0),
        ]
        bench = _compute_sector_benchmark("Technology", snapshots)
        assert bench.avg_iv_rank == pytest.approx(50.0)


class TestSectorBenchmarkCache:
    def test_get_sector_exact(self):
        bench = SectorBenchmark(
            sector="Technology",
            ticker_count=4,
            iv_rv_ratio_median=1.1,
            iv_rv_ratio_p25=0.9,
            iv_rv_ratio_p75=1.3,
            avg_iv_rank=45.0,
            avg_current_iv=0.32,
            computed_at=datetime.utcnow(),
        )
        cache = SectorBenchmarkCache(
            benchmarks={"Technology": bench},
            market_iv_rank=50.0,
            market_iv=0.20,
            market_iv_rv_ratio=1.0,
            refreshed_at=datetime.utcnow(),
            ticker_snapshots=[],
        )
        assert cache.get_sector("Technology") == bench
        assert cache.get_sector("Unknown") is None

    def test_get_sector_fuzzy_case_insensitive(self):
        bench = SectorBenchmark(
            sector="Technology",
            ticker_count=4,
            iv_rv_ratio_median=1.1,
            iv_rv_ratio_p25=0.9,
            iv_rv_ratio_p75=1.3,
            avg_iv_rank=45.0,
            avg_current_iv=0.32,
            computed_at=datetime.utcnow(),
        )
        cache = SectorBenchmarkCache(
            benchmarks={"Technology": bench, "_all_sectors": bench},
            market_iv_rank=50.0,
            market_iv=0.20,
            market_iv_rv_ratio=1.0,
            refreshed_at=datetime.utcnow(),
            ticker_snapshots=[],
        )
        assert cache.get_sector_fuzzy("technology") == bench
        assert cache.get_sector_fuzzy("TECHNOLOGY") == bench

    def test_get_sector_fuzzy_substring(self):
        bench = SectorBenchmark(
            sector="Consumer Discretionary",
            ticker_count=4,
            iv_rv_ratio_median=1.0,
            iv_rv_ratio_p25=0.85,
            iv_rv_ratio_p75=1.2,
            avg_iv_rank=40.0,
            avg_current_iv=0.35,
            computed_at=datetime.utcnow(),
        )
        fallback = SectorBenchmark(
            sector="_all_sectors",
            ticker_count=38,
            iv_rv_ratio_median=1.05,
            iv_rv_ratio_p25=0.90,
            iv_rv_ratio_p75=1.25,
            avg_iv_rank=48.0,
            avg_current_iv=0.30,
            computed_at=datetime.utcnow(),
        )
        cache = SectorBenchmarkCache(
            benchmarks={"Consumer Discretionary": bench, "_all_sectors": fallback},
            market_iv_rank=50.0,
            market_iv=0.20,
            market_iv_rv_ratio=1.0,
            refreshed_at=datetime.utcnow(),
            ticker_snapshots=[],
        )
        assert cache.get_sector_fuzzy("Consumer") == bench
        assert cache.get_sector_fuzzy("Crypto") == fallback

    def test_is_stale(self):
        cache = SectorBenchmarkCache(
            benchmarks={},
            market_iv_rank=50.0,
            market_iv=0.20,
            market_iv_rv_ratio=1.0,
            refreshed_at=datetime(2020, 1, 1),
            ticker_snapshots=[],
        )
        assert cache.is_stale is True


class TestFieldExtraction:
    def test_extract_iv_rank_standard(self):
        assert _extract_iv_rank({"data": {"iv_rank": 42.5}}) == 42.5

    def test_extract_iv_rank_list_response(self):
        assert _extract_iv_rank({"data": [{"iv_rank": 42.5}, {"iv_rank": 30.0}]}) == 42.5

    def test_extract_iv_rank_missing_raises(self):
        with pytest.raises(KeyError):
            _extract_iv_rank({"data": {"unrelated_field": 99}})


def _mock_ok_ticker(ticker: str, iv_rank: float = 40.0, iv: float = 0.30, rv: float = 0.25):
    base = "https://api.unusualwhales.com"
    respx.get(f"{base}/api/stock/{ticker}/iv-rank").mock(
        return_value=httpx.Response(200, json={"data": {"iv_rank": iv_rank, "iv": iv}})
    )
    respx.get(f"{base}/api/stock/{ticker}/volatility/stats").mock(
        return_value=httpx.Response(200, json={"data": {"realized_volatility_20d": rv, "implied_volatility": iv}})
    )


def _mock_fail_ticker(ticker: str, status_code: int = 500):
    base = "https://api.unusualwhales.com"
    respx.get(f"{base}/api/stock/{ticker}/iv-rank").mock(
        return_value=httpx.Response(status_code, json={"error": "boom"})
    )
    respx.get(f"{base}/api/stock/{ticker}/volatility/stats").mock(
        return_value=httpx.Response(status_code, json={"error": "boom"})
    )


@pytest.mark.asyncio
@respx.mock
async def test_partial_failure_still_builds_cache():
    # Restrict to a tiny benchmark universe for the test.
    from src.grader.context import sector_cache as sc

    sc.BENCHMARK_TICKERS = {"Technology": ["AAPL", "MSFT", "NVDA"]}  # type: ignore[assignment]
    sc.MARKET_PROXY_TICKER = "SPY"  # type: ignore[assignment]

    _mock_ok_ticker("AAPL")
    _mock_ok_ticker("MSFT")
    _mock_fail_ticker("NVDA")
    _mock_ok_ticker("SPY", iv_rank=55.0, iv=0.22, rv=0.20)

    async with httpx.AsyncClient() as client:
        cache = await refresh_sector_cache(client, api_token="fake")

    # Technology has 2 successful tickers → benchmark present
    assert cache.get_sector("Technology") is not None
    # _all_sectors should always exist when any sector_results exist
    assert cache.get_sector("_all_sectors") is not None


@pytest.mark.asyncio
@respx.mock
async def test_total_spy_failure_uses_defaults():
    from src.grader.context import sector_cache as sc

    sc.BENCHMARK_TICKERS = {"Energy": ["XOM", "CVX"]}  # type: ignore[assignment]
    sc.MARKET_PROXY_TICKER = "SPY"  # type: ignore[assignment]

    _mock_ok_ticker("XOM")
    _mock_ok_ticker("CVX")
    _mock_fail_ticker("SPY", status_code=503)

    async with httpx.AsyncClient() as client:
        cache = await refresh_sector_cache(client, api_token="fake")

    assert cache.market_iv_rank == pytest.approx(50.0)
    assert cache.market_iv == pytest.approx(0.20)
    assert cache.market_iv_rv_ratio == pytest.approx(1.0)
    assert cache.get_sector("_all_sectors") is not None


@pytest.mark.asyncio
@respx.mock
async def test_all_sectors_populated_when_api_succeeds():
    from src.grader.context import sector_cache as sc

    sc.BENCHMARK_TICKERS = {"Tech": ["AAPL", "MSFT"], "Energy": ["XOM", "CVX"]}  # type: ignore[assignment]
    sc.MARKET_PROXY_TICKER = "SPY"  # type: ignore[assignment]

    for t in ["AAPL", "MSFT", "XOM", "CVX"]:
        _mock_ok_ticker(t)
    _mock_ok_ticker("SPY", iv_rank=60.0, iv=0.25, rv=0.20)

    async with httpx.AsyncClient() as client:
        cache = await refresh_sector_cache(client, api_token="fake")

    assert cache.get_sector("Tech") is not None
    assert cache.get_sector("Energy") is not None
    assert cache.get_sector("_all_sectors") is not None


@pytest.mark.asyncio
@respx.mock
async def test_get_sector_cache_caches_and_force_refresh():
    # Use a tiny universe and verify repeated calls return same object unless forced.
    from src.grader.context import sector_cache as sc

    sc.BENCHMARK_TICKERS = {"Tech": ["AAPL", "MSFT"]}  # type: ignore[assignment]
    sc.MARKET_PROXY_TICKER = "SPY"  # type: ignore[assignment]
    sc._cache = None  # type: ignore[attr-defined]

    for t in ["AAPL", "MSFT", "SPY"]:
        _mock_ok_ticker(t)

    async with httpx.AsyncClient() as client:
        c1 = await get_sector_cache(client, api_token="fake")
        c2 = await get_sector_cache(client, api_token="fake")
        assert c1 is c2

        # Force refresh should replace cache instance
        c3 = await get_sector_cache(client, api_token="fake", force_refresh=True)
        assert c3 is not c2

