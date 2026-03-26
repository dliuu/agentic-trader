"""
Sector Benchmark Cache — daily-refresh volatility benchmarks for market context scoring.

Usage:
    cache = await refresh_sector_cache(http_client, api_token)
    benchmark = cache.get_sector_fuzzy("Technology")
    market_rank = cache.market_iv_rank
"""

from __future__ import annotations

import asyncio
import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
import structlog

logger = structlog.get_logger()


# Tickers used to build sector benchmarks.
# Criteria: high options volume, liquid chains, representative of sector.
# This is intentionally a small set — we need ~3-4 per sector for a median,
# not a comprehensive universe.
BENCHMARK_TICKERS: dict[str, list[str]] = {
    "Technology": ["AAPL", "MSFT", "NVDA", "GOOGL"],
    "Healthcare": ["JNJ", "UNH", "PFE", "ABBV"],
    "Financials": ["JPM", "BAC", "GS", "V"],
    "Consumer Discretionary": ["AMZN", "TSLA", "HD", "NKE"],
    "Consumer Staples": ["PG", "KO", "COST", "WMT"],
    "Energy": ["XOM", "CVX", "COP"],
    "Industrials": ["CAT", "BA", "UNP", "HON"],
    "Communication Services": ["META", "GOOG", "NFLX", "DIS"],
    "Materials": ["LIN", "APD", "FCX"],
    "Utilities": ["NEE", "DUK", "SO"],
    "Real Estate": ["AMT", "PLD", "SPG"],
}

# Market-wide proxy
MARKET_PROXY_TICKER = "SPY"


@dataclass(frozen=True)
class TickerVolSnapshot:
    """Raw vol data for a single benchmark ticker."""

    ticker: str
    sector: str
    iv_rank: float  # 0-100
    current_iv: float  # annualized, e.g. 0.35
    realized_vol_20d: float  # annualized
    iv_rv_ratio: float  # current_iv / realized_vol_20d
    fetched_at: datetime


@dataclass(frozen=True)
class SectorBenchmark:
    """Aggregated vol statistics for one sector."""

    sector: str
    ticker_count: int
    iv_rv_ratio_median: float
    iv_rv_ratio_p25: float
    iv_rv_ratio_p75: float
    avg_iv_rank: float
    avg_current_iv: float
    computed_at: datetime


@dataclass
class SectorBenchmarkCache:
    """The full cache — holds all sector benchmarks + market-wide data."""

    benchmarks: dict[str, SectorBenchmark]  # keyed by sector name
    market_iv_rank: float  # SPY IV rank as broad market proxy
    market_iv: float  # SPY current IV
    market_iv_rv_ratio: float  # SPY IV/RV ratio
    refreshed_at: datetime
    ticker_snapshots: list[TickerVolSnapshot]  # raw data, useful for debugging

    @property
    def is_stale(self) -> bool:
        """Cache is stale if older than 8 hours."""
        age = datetime.utcnow() - self.refreshed_at
        return age.total_seconds() > 8 * 3600

    def get_sector(self, sector: str) -> SectorBenchmark | None:
        """Lookup by sector name. Returns None if sector not in cache."""
        return self.benchmarks.get(sector)

    def get_sector_fuzzy(self, sector: str) -> SectorBenchmark | None:
        """Fuzzy lookup — tries exact match, then case-insensitive, then substring.

        UW API sector names may not exactly match what we store.
        Falls back to the 'all_sectors' aggregate if no match found.
        """
        if sector in self.benchmarks:
            return self.benchmarks[sector]

        sector_lower = sector.lower()
        for key, bench in self.benchmarks.items():
            if key.lower() == sector_lower:
                return bench

        for key, bench in self.benchmarks.items():
            if sector_lower in key.lower() or key.lower() in sector_lower:
                return bench

        return self.benchmarks.get("_all_sectors")


def _unwrap_data(response: dict[str, Any]) -> dict[str, Any]:
    """UW API responses wrap data in a 'data' key, sometimes as a list."""
    data: Any = response.get("data", response)
    if isinstance(data, list):
        return data[0] if data else {}
    if isinstance(data, dict):
        return data
    return {}


def _extract_iv_rank(iv_response: dict[str, Any]) -> float:
    """Extract IV rank (0-100) from /iv-rank response."""
    data = _unwrap_data(iv_response)
    for field in ["iv_rank", "ivRank", "rank", "iv_percentile_rank"]:
        val = data.get(field)
        if val is not None:
            if field != "iv_rank":
                logger.warning("sector_cache.field_fallback", metric="iv_rank", field=field)
            return float(val)
    raise KeyError(f"No IV rank field found. Available keys: {list(data.keys())}")


def _extract_current_iv(iv_response: dict[str, Any], vol_response: dict[str, Any]) -> float:
    """Extract current implied volatility. Tries iv-rank response first, then vol stats."""
    for resp, source in [(iv_response, "iv-rank"), (vol_response, "vol-stats")]:
        data = _unwrap_data(resp)
        for field in ["iv", "implied_volatility", "current_iv", "iv30", "impliedVolatility"]:
            val = data.get(field)
            if val is not None:
                if not (source == "iv-rank" and field == "iv"):
                    logger.warning(
                        "sector_cache.field_fallback",
                        metric="current_iv",
                        field=field,
                        source=source,
                    )
                return float(val)
    raise KeyError("No current IV field found in either response.")


def _extract_realized_vol(vol_response: dict[str, Any]) -> float:
    """Extract 20-day realized volatility from /volatility/stats response."""
    data = _unwrap_data(vol_response)
    for field in [
        "realized_volatility_20d",
        "rv20",
        "realized_vol_20",
        "hv20",
        "historical_volatility_20d",
        "realizedVolatility20",
    ]:
        val = data.get(field)
        if val is not None:
            if field != "realized_volatility_20d":
                logger.warning("sector_cache.field_fallback", metric="realized_vol_20d", field=field)
            return float(val)

    for key, val in data.items():
        if ("realized" in key.lower()) or ("hv" in key.lower()):
            if val is not None:
                logger.warning(
                    "sector_cache.field_fallback",
                    metric="realized_vol_20d",
                    field=key,
                    source="heuristic",
                )
                return float(val)

    raise KeyError(f"No realized vol field found. Available keys: {list(data.keys())}")


async def _fetch_ticker_vol(
    client: httpx.AsyncClient,
    ticker: str,
    sector: str,
    api_token: str,
) -> TickerVolSnapshot | None:
    """Fetch IV rank + vol stats for one ticker. Returns None on failure."""
    headers = {
        "Authorization": f"Bearer {api_token}",
        "UW-CLIENT-API-ID": "100001",
    }
    base = "https://api.unusualwhales.com"

    try:
        iv_resp, vol_resp = await asyncio.gather(
            client.get(f"{base}/api/stock/{ticker}/iv-rank", headers=headers),
            client.get(f"{base}/api/stock/{ticker}/volatility/stats", headers=headers),
        )
        iv_resp.raise_for_status()
        vol_resp.raise_for_status()

        iv_data = iv_resp.json()
        vol_data = vol_resp.json()

        iv_rank = _extract_iv_rank(iv_data)
        current_iv = _extract_current_iv(iv_data, vol_data)
        realized_vol_20d = _extract_realized_vol(vol_data)

        iv_rv_ratio = current_iv / realized_vol_20d if realized_vol_20d > 0.001 else 1.0

        return TickerVolSnapshot(
            ticker=ticker,
            sector=sector,
            iv_rank=iv_rank,
            current_iv=current_iv,
            realized_vol_20d=realized_vol_20d,
            iv_rv_ratio=iv_rv_ratio,
            fetched_at=datetime.utcnow(),
        )
    except Exception as e:
        logger.warning("sector_cache.ticker_fetch_failed", ticker=ticker, sector=sector, error=str(e))
        return None


def _compute_sector_benchmark(sector: str, snapshots: list[TickerVolSnapshot]) -> SectorBenchmark:
    """Compute percentile stats from a list of ticker snapshots."""
    ratios = sorted(s.iv_rv_ratio for s in snapshots)
    ranks = [s.iv_rank for s in snapshots]
    ivs = [s.current_iv for s in snapshots]

    n = len(ratios)
    p25_idx = max(0, int(n * 0.25))
    p75_idx = min(n - 1, int(n * 0.75))

    return SectorBenchmark(
        sector=sector,
        ticker_count=n,
        iv_rv_ratio_median=statistics.median(ratios),
        iv_rv_ratio_p25=ratios[p25_idx],
        iv_rv_ratio_p75=ratios[p75_idx],
        avg_iv_rank=statistics.mean(ranks),
        avg_current_iv=statistics.mean(ivs),
        computed_at=datetime.utcnow(),
    )


async def refresh_sector_cache(client: httpx.AsyncClient, api_token: str) -> SectorBenchmarkCache:
    """Fetch all benchmark tickers and build the cache."""
    ticker_total = sum(len(v) for v in BENCHMARK_TICKERS.values()) + 1
    logger.info("sector_cache.refresh_started", ticker_count=ticker_total)

    tasks: list[asyncio.Future[TickerVolSnapshot | None]] = []
    for sector, tickers in BENCHMARK_TICKERS.items():
        for ticker in tickers:
            tasks.append(_fetch_ticker_vol(client, ticker, sector, api_token))

    spy_task: asyncio.Future[TickerVolSnapshot | None] = _fetch_ticker_vol(
        client, MARKET_PROXY_TICKER, "_market", api_token
    )

    semaphore = asyncio.Semaphore(10)

    async def _throttled(coro: asyncio.Future[TickerVolSnapshot | None]) -> TickerVolSnapshot | None:
        async with semaphore:
            return await coro

    results = await asyncio.gather(
        *[_throttled(t) for t in tasks],
        _throttled(spy_task),
        return_exceptions=True,
    )

    spy_snapshot = results[-1] if isinstance(results[-1], TickerVolSnapshot) else None
    sector_results: list[TickerVolSnapshot] = [r for r in results[:-1] if isinstance(r, TickerVolSnapshot)]

    by_sector: dict[str, list[TickerVolSnapshot]] = {}
    for snapshot in sector_results:
        by_sector.setdefault(snapshot.sector, []).append(snapshot)

    benchmarks: dict[str, SectorBenchmark] = {}
    for sector, snapshots in by_sector.items():
        if len(snapshots) >= 2:
            benchmarks[sector] = _compute_sector_benchmark(sector, snapshots)
        else:
            logger.warning("sector_cache.insufficient_tickers", sector=sector, count=len(snapshots))

    if sector_results:
        benchmarks["_all_sectors"] = _compute_sector_benchmark("_all_sectors", sector_results)

    market_iv_rank = spy_snapshot.iv_rank if spy_snapshot else 50.0
    market_iv = spy_snapshot.current_iv if spy_snapshot else 0.20
    market_iv_rv_ratio = spy_snapshot.iv_rv_ratio if spy_snapshot else 1.0

    cache = SectorBenchmarkCache(
        benchmarks=benchmarks,
        market_iv_rank=market_iv_rank,
        market_iv=market_iv,
        market_iv_rv_ratio=market_iv_rv_ratio,
        refreshed_at=datetime.utcnow(),
        ticker_snapshots=sector_results,
    )

    logger.info(
        "sector_cache.refresh_complete",
        sectors=len(benchmarks),
        tickers_fetched=len(sector_results),
        tickers_failed=len(tasks) - len(sector_results),
        market_iv_rank=market_iv_rank,
    )
    return cache


_cache: SectorBenchmarkCache | None = None
_cache_lock = asyncio.Lock()


async def get_sector_cache(
    client: httpx.AsyncClient,
    api_token: str,
    force_refresh: bool = False,
) -> SectorBenchmarkCache:
    """Get or refresh the sector cache. Thread-safe via asyncio lock.

    Auto-refreshes if cache is None, stale, or force_refresh=True.
    """
    global _cache
    async with _cache_lock:
        if _cache is None or _cache.is_stale or force_refresh:
            _cache = await refresh_sector_cache(client, api_token)
        return _cache


def get_cached_benchmarks() -> SectorBenchmarkCache | None:
    """Synchronous accessor — returns current cache or None if not yet populated."""
    return _cache

