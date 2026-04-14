"""Volatility context builder for deterministic vol scoring.

Builds a normalized VolContext from 4 Unusual Whales endpoints:
  - /api/stock/{ticker}/iv-rank
  - /api/stock/{ticker}/volatility/stats
  - /api/stock/{ticker}/volatility/term-structure
  - /api/stock/{ticker}/option-chains

All volatility scoring logic should operate on VolContext (never raw payloads).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog

from shared.models import Candidate
from shared.uw_http import uw_get_json
from shared.uw_runtime import get_uw_limiter

logger = structlog.get_logger()

UW_BASE = "https://api.unusualwhales.com"


@dataclass(frozen=True)
class VolContext:
    """Normalized volatility data for a single candidate.

    Built from 4 UW API calls. All scoring functions take this as input.
    """

    ticker: str

    # === From /api/stock/{ticker}/iv-rank ===
    iv_rank: float  # 0-100, percentile vs 52-week high/low range
    iv_percentile: float  # 0-100, percentile vs 52-week distribution
    current_iv: float  # Annualized IV (e.g. 0.35 = 35%)

    # === From /api/stock/{ticker}/volatility/stats ===
    realized_vol_20d: float  # 20-day annualized realized vol
    realized_vol_60d: float  # 60-day annualized realized vol (for regime detection)

    # === Derived from above ===
    iv_rv_spread: float  # current_iv - realized_vol_20d (positive = IV premium)
    iv_rv_ratio: float  # current_iv / realized_vol_20d (>1.0 = paying premium)
    rv_regime_ratio: float  # realized_vol_20d / realized_vol_60d (>1 = expanding)

    # === From /api/stock/{ticker}/volatility/term-structure ===
    near_term_iv: float  # IV at nearest standard expiry
    far_term_iv: float  # IV at 60+ day expiry
    term_structure_slope: float  # (far - near) / near; negative = inverted
    candidate_expiry_iv: float  # Interpolated IV at the candidate's specific expiry

    # === From /api/stock/{ticker}/option-chains ===
    contract_delta: float  # Absolute delta of the specific contract
    contract_gamma: float
    contract_theta: float  # Daily theta in dollars (negative number)
    contract_vega: float
    contract_mid_price: float  # (bid + ask) / 2
    contract_volume: int
    contract_oi: int

    # === Derived from contract data ===
    theta_pct_of_premium: float  # abs(theta) / mid_price — daily decay rate as fraction
    moneyness: float  # For calls: spot/strike. For puts: strike/spot.
    # >1 = ITM, <1 = OTM, 1.0 = ATM

    # === Metadata ===
    candidate_dte: int  # Days to expiry
    candidate_is_call: bool
    sector: str  # From candidate.raw_data or /stock/{ticker}/info
    fetched_at: datetime


async def build_vol_context(
    candidate: Candidate,
    client: httpx.AsyncClient,
    api_token: str,
) -> VolContext | None:
    """Fetch 4 UW endpoints and build a VolContext.

    Returns None if critical data is unavailable.
    Non-critical fields default to neutral values.
    """

    ticker = candidate.ticker
    headers = {
        "Authorization": f"Bearer {api_token}",
        "UW-CLIENT-API-ID": "100001",
        "Accept": "application/json",
    }
    limiter = get_uw_limiter()

    try:
        iv_resp, vol_resp, term_resp, chain_resp = await asyncio.gather(
            uw_get_json(
                client,
                f"{UW_BASE}/api/stock/{ticker}/iv-rank",
                headers=headers,
                limiter=limiter,
                cache_key=f"uw:iv-rank:{ticker}",
            ),
            uw_get_json(
                client,
                f"{UW_BASE}/api/stock/{ticker}/volatility/stats",
                headers=headers,
                limiter=limiter,
                cache_key=f"uw:vol-stats:{ticker}",
            ),
            uw_get_json(
                client,
                f"{UW_BASE}/api/stock/{ticker}/volatility/term-structure",
                headers=headers,
                limiter=limiter,
                cache_key=f"uw:term-structure:{ticker}",
            ),
            uw_get_json(
                client,
                f"{UW_BASE}/api/stock/{ticker}/option-chains",
                headers=headers,
                limiter=limiter,
                cache_key=f"uw:option-chains:{ticker}",
            ),
            return_exceptions=True,
        )
    except Exception as e:
        logger.error("vol_ctx.fetch_failed", ticker=ticker, error=str(e))
        return None

    responses: dict[str, Any] = {
        "iv_rank": iv_resp,
        "vol_stats": vol_resp,
        "term_structure": term_resp,
        "option_chains": chain_resp,
    }

    for name, resp in responses.items():
        if isinstance(resp, Exception):
            logger.warning(
                "vol_ctx.endpoint_failed",
                ticker=ticker,
                endpoint=name,
                error=str(resp),
            )
            return None  # all 4 endpoints required for meaningful scoring

    try:
        if not isinstance(iv_resp, dict) or not isinstance(vol_resp, dict):
            return None
        if not isinstance(term_resp, dict) or not isinstance(chain_resp, dict):
            return None
        return _assemble_vol_context(
            candidate=candidate,
            iv_data=iv_resp,
            vol_data=vol_resp,
            term_data=term_resp,
            chain_data=chain_resp,
        )
    except Exception as e:
        logger.error("vol_ctx.assembly_failed", ticker=ticker, error=str(e))
        return None


def _assemble_vol_context(
    candidate: Candidate,
    iv_data: dict[str, Any],
    vol_data: dict[str, Any],
    term_data: dict[str, Any],
    chain_data: dict[str, Any],
) -> VolContext:
    """Pure function: raw API responses → VolContext.

    This is the function unit tests should target with fixture data.
    """

    ticker = candidate.ticker

    # --- IV Rank data ---
    iv_rank = _extract_float(
        iv_data,
        ["iv_rank", "iv_rank_1y", "ivRank", "rank"],
    )
    iv_percentile = _extract_float(
        iv_data,
        ["iv_percentile", "ivPercentile", "percentile"],
        default=iv_rank,
    )
    current_iv = _extract_float_multi(
        [iv_data, vol_data],
        ["iv", "implied_volatility", "current_iv", "iv30", "impliedVolatility"],
    )

    # --- Realized vol data ---
    rv_20d = _extract_float(
        vol_data,
        [
            "realized_volatility_20d",
            "rv20",
            "realized_vol_20",
            "hv20",
            "historical_volatility_20d",
        ],
    )
    rv_60d = _extract_float(
        vol_data,
        [
            "realized_volatility_60d",
            "rv60",
            "realized_vol_60",
            "hv60",
            "historical_volatility_60d",
        ],
        default=rv_20d,
    )

    # --- Derived: IV vs RV ---
    iv_rv_spread = current_iv - rv_20d
    iv_rv_ratio = current_iv / rv_20d if rv_20d > 0.001 else 1.0
    rv_regime_ratio = rv_20d / rv_60d if rv_60d > 0.001 else 1.0

    # --- Term structure ---
    candidate_expiry = _parse_candidate_expiry(candidate.expiry)
    near_iv, far_iv, candidate_iv = _parse_term_structure(term_data, candidate_expiry)
    term_slope = (far_iv - near_iv) / near_iv if near_iv > 0.001 else 0.0

    # --- Option chain: find the specific contract ---
    strike = float(candidate.strike)
    type_str = "call" if (candidate.direction or "").lower() == "bullish" else "put"
    contract = _find_contract_in_chain(
        chain_data=chain_data,
        strike=strike,
        expiry=candidate_expiry,
        option_type=type_str,
    )

    delta = abs(float(contract.get("delta", 0.0) or 0.0))
    gamma = float(contract.get("gamma", 0.0) or 0.0)
    theta = float(contract.get("theta", 0.0) or 0.0)
    vega = float(contract.get("vega", 0.0) or 0.0)
    bid = float(contract.get("bid", 0.0) or 0.0)
    ask = float(contract.get("ask", 0.0) or 0.0)
    mid_price = (
        (bid + ask) / 2 if (bid + ask) > 0 else float(contract.get("last_price", 0.01) or 0.01)
    )
    volume = int(contract.get("volume", 0) or 0)
    oi = int(contract.get("open_interest", 0) or 0)

    theta_pct = abs(theta) / mid_price if mid_price > 0.001 else 0.0

    # Moneyness
    spot = float(candidate.underlying_price or candidate.strike)
    is_call = type_str == "call"
    moneyness = (spot / strike) if is_call else (strike / spot if spot > 0 else 1.0)

    # DTE
    dte = int(getattr(candidate, "dte", 0) or 0)

    # Sector — try candidate.raw_data first, then default
    sector = _extract_sector(candidate)

    return VolContext(
        ticker=ticker,
        iv_rank=iv_rank,
        iv_percentile=iv_percentile,
        current_iv=current_iv,
        realized_vol_20d=rv_20d,
        realized_vol_60d=rv_60d,
        iv_rv_spread=iv_rv_spread,
        iv_rv_ratio=iv_rv_ratio,
        rv_regime_ratio=rv_regime_ratio,
        near_term_iv=near_iv,
        far_term_iv=far_iv,
        term_structure_slope=term_slope,
        candidate_expiry_iv=candidate_iv,
        contract_delta=delta,
        contract_gamma=gamma,
        contract_theta=theta,
        contract_vega=vega,
        contract_mid_price=mid_price,
        contract_volume=volume,
        contract_oi=oi,
        theta_pct_of_premium=theta_pct,
        moneyness=moneyness,
        candidate_dte=dte,
        candidate_is_call=is_call,
        sector=sector,
        fetched_at=datetime.now(timezone.utc),
    )


def _parse_term_structure(
    term_data: dict[str, Any], candidate_expiry: datetime
) -> tuple[float, float, float]:
    """Extract near-term IV, far-term IV, and interpolated IV at candidate expiry.

    Returns (near_term_iv, far_term_iv, candidate_expiry_iv).

    Term structure data is expected as a list of {expiry, iv} pairs sorted by date.
    """

    data = _unwrap_data(term_data)
    if not isinstance(data, list) or len(data) == 0:
        return (0.20, 0.20, 0.20)

    points: list[tuple[datetime, float]] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        expiry_str = entry.get("expiry") or entry.get("expiration_date") or entry.get("date")
        iv_val = entry.get("iv") or entry.get("implied_volatility")
        if expiry_str and iv_val is not None:
            try:
                exp_date = datetime.fromisoformat(str(expiry_str).replace("Z", "+00:00"))
                points.append((exp_date, float(iv_val)))
            except (ValueError, TypeError):
                continue

    if len(points) < 2:
        iv = points[0][1] if points else 0.20
        return (iv, iv, iv)

    points.sort(key=lambda x: x[0])
    near_iv = points[0][1]
    far_iv = points[-1][1]
    candidate_iv = _interpolate_iv(points, candidate_expiry)
    return (near_iv, far_iv, candidate_iv)


def _interpolate_iv(points: list[tuple[datetime, float]], target: datetime) -> float:
    """Linear interpolation of IV at a target expiry date."""

    if target <= points[0][0]:
        return points[0][1]
    if target >= points[-1][0]:
        return points[-1][1]

    for i in range(len(points) - 1):
        if points[i][0] <= target <= points[i + 1][0]:
            total_span = (points[i + 1][0] - points[i][0]).total_seconds()
            if total_span == 0:
                return points[i][1]
            frac = (target - points[i][0]).total_seconds() / total_span
            return points[i][1] + frac * (points[i + 1][1] - points[i][1])

    return points[-1][1]


def _find_contract_in_chain(
    chain_data: dict[str, Any],
    strike: float,
    expiry: datetime,
    option_type: str,
) -> dict[str, Any]:
    """Find the specific contract in the option chain response.

    Returns the contract dict, or an empty dict if not found.
    """

    data = _unwrap_data(chain_data)
    if not isinstance(data, list):
        data = [data] if data else []

    expiry_str = expiry.strftime("%Y-%m-%d")
    type_str = option_type.lower()

    for contract in data:
        if not isinstance(contract, dict):
            continue
        c_strike = float(contract.get("strike", contract.get("strike_price", 0)) or 0)
        c_expiry = str(contract.get("expiry", contract.get("expiration_date", "")) or "")[:10]
        c_type = str(contract.get("type", contract.get("option_type", "")) or "").lower()

        if (abs(c_strike - strike) < 0.01) and (c_expiry == expiry_str) and (c_type == type_str):
            return contract

    logger.warning(
        "vol_ctx.contract_not_found",
        strike=strike,
        expiry=expiry_str,
        type=type_str,
        chain_size=len(data),
    )
    return {}


def _unwrap_data(response: dict[str, Any]) -> Any:
    """UW API wraps responses in 'data' key, sometimes as a list."""

    return response.get("data", response)


def _extract_float(
    data_source: dict[str, Any], field_names: list[str], default: float | None = None
) -> float:
    """Try multiple field names in order. Unwraps 'data' wrapper first."""

    data: Any = _unwrap_data(data_source) if isinstance(data_source, dict) and ("data" in data_source) else data_source
    if isinstance(data, list):
        data = data[0] if data else {}

    if not isinstance(data, dict):
        data = {}

    for field in field_names:
        val = data.get(field)
        if val is not None:
            try:
                return float(val)
            except (ValueError, TypeError):
                continue

    if default is not None:
        return default

    raise KeyError(f"None of {field_names} found in keys: {list(data.keys())}")


def _extract_float_multi(sources: list[dict[str, Any]], field_names: list[str]) -> float:
    """Try field names across multiple response dicts."""

    for source in sources:
        try:
            return _extract_float(source, field_names)
        except KeyError:
            continue
    raise KeyError(f"None of {field_names} found in any of {len(sources)} sources")


def _extract_sector(candidate: Candidate) -> str:
    """Get sector from candidate's raw_data, with fallback."""

    raw: Any = getattr(candidate, "raw_data", {}) or {}
    if isinstance(raw, dict):
        for field in ["sector", "industry_group", "gics_sector"]:
            val = raw.get(field)
            if val:
                return str(val)
    return "Unknown"


def _parse_candidate_expiry(expiry_value: str) -> datetime:
    """Parse Candidate.expiry into a datetime.

    Candidate.expiry in this repo is an ISO date string (YYYY-MM-DD).
    For alignment with other agents, anchor to end-of-day UTC when possible.
    """

    try:
        # If it is already a full datetime, accept it.
        if "T" in expiry_value:
            dt = datetime.fromisoformat(expiry_value.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        d = datetime.fromisoformat(expiry_value)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except Exception:
        return datetime.now(timezone.utc)


def build_vol_context_from_saved_json(
    candidate: Candidate,
    *,
    chain_response: dict[str, Any],
    vol_stats_response: dict[str, Any] | None,
    iv_rank_response: dict[str, Any] | None = None,
    term_structure_response: dict[str, Any] | None = None,
) -> VolContext | None:
    """Assemble ``VolContext`` from backfilled JSON (replay without live vol endpoints).

    When IV rank or term-structure payloads are missing, uses neutral placeholders
    so volatility scoring can still run from chain + vol stats alone.
    """
    neutral_iv: dict[str, Any] = {
        "data": {
            "iv_rank": 50.0,
            "iv_percentile": 50.0,
            "iv": 0.35,
            "implied_volatility": 0.35,
        }
    }
    neutral_term: dict[str, Any] = {"data": []}
    neutral_vol: dict[str, Any] = {
        "data": {
            "realized_volatility_20d": 0.25,
            "realized_volatility_60d": 0.25,
            "implied_volatility": 0.35,
        }
    }
    iv_r = iv_rank_response if iv_rank_response is not None else neutral_iv
    term_r = term_structure_response if term_structure_response is not None else neutral_term
    vs = vol_stats_response if vol_stats_response is not None else neutral_vol
    try:
        return _assemble_vol_context(
            candidate=candidate,
            iv_data=iv_r,
            vol_data=vs,
            term_data=term_r,
            chain_data=chain_response,
        )
    except Exception as e:
        logger.error("vol_ctx.saved_snapshot_failed", ticker=candidate.ticker, error=str(e))
        return None

