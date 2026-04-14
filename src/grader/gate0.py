"""
Gate 0: Ticker Universe Filter.

Hard pre-filter before Gate 1. Rejects candidates outside the target universe
(small/mid-cap common stocks) using:
  1. Static block lists (EXCLUDED_TICKERS, BLOCKED_MEGA_CAPS, BLOCKED_MEME_TICKERS,
     BLOCKED_CHINA_ADRS) — zero API calls.
  2. Dynamic market cap + issue_type check via /api/stock/{ticker}/info — one API
     call per new ticker, cached for 24 hours.

Design decisions:
  - Fail-open on API errors: if /stock/{ticker}/info returns an error or times out,
    the candidate proceeds. The static lists already caught the worst offenders, and
    Gate 1/2/3 provide further filtering.
  - ALLOW_LIST: when non-empty, a ticker must be listed to reach the dynamic
    (market cap / issue type) checks; EXCLUDED_TICKERS (ETFs, index products, etc.)
    still apply and are not bypassed. Use for backtests or a fixed watchlist.
  - Caching: uses the shared UW JSON cache (uw_get_json) with a 24h TTL. Market cap
    and issue_type don't change intraday, so this avoids redundant calls when the
    same ticker appears multiple times.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from shared.filters import (
    UNIVERSE_CONFIG,
    TickerExclusionReason,
    UniverseConfig,
    is_universe_blocked,
)
from shared.models import Candidate
from shared.uw_http import uw_get_json
from shared.uw_validation import uw_auth_headers

log = structlog.get_logger()

UW_BASE = "https://api.unusualwhales.com"


class Gate0Result:
    """Result of Gate 0 universe check."""

    __slots__ = ("passed", "reason", "market_cap", "issue_type", "sector")

    def __init__(
        self,
        passed: bool,
        reason: TickerExclusionReason | None = None,
        market_cap: float | None = None,
        issue_type: str | None = None,
        sector: str | None = None,
    ):
        self.passed = passed
        self.reason = reason
        self.market_cap = market_cap
        self.issue_type = issue_type
        self.sector = sector


def _gate0_result_from_info_payload(
    ticker: str,
    info: dict[str, Any],
    cfg: UniverseConfig,
) -> Gate0Result:
    """Apply universe rules to a normalized /stock/{{ticker}}/info ``data`` dict."""
    issue_type = (
        info.get("issue_type")
        or info.get("issueType")
        or info.get("type")
        or ""
    )
    market_cap_raw = (
        info.get("marketCap")
        or info.get("market_cap")
        or info.get("mktCap")
        or 0
    )
    sector = info.get("sector") or info.get("sectorname") or None

    try:
        market_cap = float(market_cap_raw)
    except (TypeError, ValueError):
        market_cap = 0.0

    if issue_type and issue_type not in cfg.allowed_issue_types:
        log.info(
            "gate0.blocked_issue_type",
            ticker=ticker,
            issue_type=issue_type,
        )
        return Gate0Result(
            passed=False,
            reason=TickerExclusionReason.NON_COMMON_STOCK,
            market_cap=market_cap,
            issue_type=issue_type,
            sector=sector,
        )

    if market_cap > 0 and (market_cap < cfg.min_market_cap or market_cap > cfg.max_market_cap):
        log.info(
            "gate0.blocked_market_cap",
            ticker=ticker,
            market_cap=market_cap,
            min_cap=cfg.min_market_cap,
            max_cap=cfg.max_market_cap,
        )
        return Gate0Result(
            passed=False,
            reason=TickerExclusionReason.MARKET_CAP_OUT_OF_RANGE,
            market_cap=market_cap,
            issue_type=issue_type,
            sector=sector,
        )

    if market_cap <= 0:
        log.warning("gate0.market_cap_missing", ticker=ticker)

    log.info(
        "gate0.passed",
        ticker=ticker,
        market_cap=market_cap,
        issue_type=issue_type,
        sector=sector,
    )
    return Gate0Result(
        passed=True,
        market_cap=market_cap,
        issue_type=issue_type,
        sector=sector,
    )


async def run_gate0(
    candidate: Candidate,
    client: httpx.AsyncClient,
    api_token: str,
    config: UniverseConfig | None = None,
    stock_info_json: dict[str, Any] | None = None,
) -> Gate0Result:
    """Run Gate 0 on a single candidate.

    Steps:
      1. Check static block lists (is_universe_blocked).
      2. If not statically blocked, fetch /api/stock/{ticker}/info (cached 24h).
      3. Check issue_type is in allowed_issue_types.
      4. Check market cap is within [min_market_cap, max_market_cap].

    Args:
        candidate: The scanner candidate to check.
        client: httpx.AsyncClient for API calls.
        api_token: UW API token.
        config: Optional UniverseConfig override (for testing).

    Returns:
        Gate0Result with passed=True if the ticker is in the target universe.
    """
    cfg = config or UNIVERSE_CONFIG
    ticker = candidate.ticker.upper()

    blocked, reason = is_universe_blocked(ticker)
    if blocked:
        log.info(
            "gate0.blocked_static",
            ticker=ticker,
            reason=reason.value if reason else "unknown",
        )
        return Gate0Result(passed=False, reason=reason)

    if stock_info_json is not None:
        data = stock_info_json.get("data", stock_info_json) if isinstance(stock_info_json, dict) else {}
        if isinstance(data, list) and len(data) > 0:
            data = data[0]
        if not isinstance(data, dict):
            log.warning("gate0.replay_info_unexpected_shape", ticker=ticker)
            return Gate0Result(passed=True)
        return _gate0_result_from_info_payload(ticker, data, cfg)

    try:
        info = await uw_get_json(
            client,
            f"{UW_BASE}/api/stock/{ticker}/info",
            headers=uw_auth_headers(api_token),
            use_cache=True,
            cache_key=f"gate0:stock_info:{ticker}",
            ttl_seconds=float(cfg.cache_ttl_seconds),
        )
    except Exception as exc:
        log.warning(
            "gate0.info_fetch_failed",
            ticker=ticker,
            error=str(exc),
        )
        return Gate0Result(passed=True)

    data = info.get("data", info) if isinstance(info, dict) else {}
    if isinstance(data, list) and len(data) > 0:
        data = data[0]
    if not isinstance(data, dict):
        log.warning("gate0.info_unexpected_shape", ticker=ticker, shape=type(data).__name__)
        return Gate0Result(passed=True)

    return _gate0_result_from_info_payload(ticker, data, cfg)
