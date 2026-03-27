"""Async context builder for the deterministic sector analyst (UW API)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import httpx
import structlog

logger = structlog.get_logger()

UW_BASE = "https://api.unusualwhales.com"

SECTOR_SLUG_MAP: dict[str, str] = {
    "Technology": "technology",
    "Healthcare": "healthcare",
    "Health Care": "healthcare",
    "Financial Services": "financial-services",
    "Financials": "financial-services",
    "Consumer Cyclical": "consumer-cyclical",
    "Consumer Discretionary": "consumer-cyclical",
    "Consumer Defensive": "consumer-defensive",
    "Consumer Staples": "consumer-defensive",
    "Industrials": "industrials",
    "Energy": "energy",
    "Utilities": "utilities",
    "Real Estate": "real-estate",
    "Basic Materials": "basic-materials",
    "Materials": "basic-materials",
    "Communication Services": "communication-services",
}

BIOTECH_SECTORS: set[str] = {"Healthcare", "Health Care"}

HIGH_IMPACT_EVENT_KEYWORDS: list[str] = [
    "fomc",
    "federal funds rate",
    "interest rate decision",
    "cpi",
    "consumer price index",
    "nonfarm payroll",
    "non-farm payroll",
    "jobs report",
    "unemployment rate",
    "pce",
    "gdp",
    "ppi",
    "retail sales",
    "ism manufacturing",
    "ism services",
    "jackson hole",
]


@dataclass(frozen=True)
class SectorTide:
    sector: str
    bullish_premium: float
    bearish_premium: float
    net_premium: float
    call_volume: float
    put_volume: float
    call_put_ratio: float
    raw: dict[str, Any]


@dataclass(frozen=True)
class MarketTide:
    bullish_premium: float
    bearish_premium: float
    net_premium: float
    call_volume: float
    put_volume: float
    call_put_ratio: float
    raw: dict[str, Any]


@dataclass(frozen=True)
class EconomicEvent:
    name: str
    date: str
    is_high_impact: bool
    raw: dict[str, Any]


@dataclass(frozen=True)
class SectorETF:
    sector: str
    ticker: str
    performance_1d: float
    performance_5d: float
    performance_1m: float
    raw: dict[str, Any]


@dataclass(frozen=True)
class FDADate:
    ticker: str
    drug_name: str
    event_type: str
    date: str
    raw: dict[str, Any]


@dataclass
class SectorContext:
    ticker: str
    ticker_sector: str | None
    sector_slug: str
    is_biotech: bool
    has_upcoming_fda: bool
    sector_tide: SectorTide | None
    market_tide: MarketTide | None
    economic_events: list[EconomicEvent]
    high_impact_events: list[EconomicEvent]
    sector_etf: SectorETF | None
    fda_dates: list[FDADate]
    fetch_errors: list[str] = field(default_factory=list)


def _unwrap_data_list(payload: Any) -> list[Any]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        d = payload.get("data", payload)
        if isinstance(d, list):
            return d
        if isinstance(d, dict):
            return [d]
    return []


def _float_from(d: dict[str, Any], *keys: str, default: float = 0.0) -> float:
    for k in keys:
        if k in d and d[k] is not None:
            try:
                return float(d[k])
            except (TypeError, ValueError):
                continue
    return default


def _str_from(d: dict[str, Any], *keys: str, default: str = "") -> str:
    for k in keys:
        v = d.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return default


def _tide_dict_to_sector_tide(d: dict[str, Any], sector_label: str = "") -> SectorTide:
    return SectorTide(
        sector=_str_from(d, "sector", default=sector_label),
        bullish_premium=_float_from(d, "bullish_premium", "bullishPremium"),
        bearish_premium=_float_from(d, "bearish_premium", "bearishPremium"),
        net_premium=_float_from(d, "net_premium", "netPremium"),
        call_volume=_float_from(d, "call_volume", "callVolume"),
        put_volume=_float_from(d, "put_volume", "putVolume"),
        call_put_ratio=_float_from(d, "call_put_ratio", "callPutRatio", "cp_ratio"),
        raw=dict(d),
    )


def parse_sector_tide(payload: Any) -> SectorTide | None:
    """Parse sector-tide API payload into SectorTide."""
    if payload is None:
        return None
    if isinstance(payload, list):
        if not payload:
            return None
        if isinstance(payload[0], dict):
            return _tide_dict_to_sector_tide(payload[0])
        return None
    if isinstance(payload, dict):
        inner = payload.get("data", payload)
        if isinstance(inner, list):
            if not inner:
                return None
            if isinstance(inner[0], dict):
                return _tide_dict_to_sector_tide(inner[0])
            return None
        if isinstance(inner, dict):
            return _tide_dict_to_sector_tide(inner)
    return None


def parse_market_tide(payload: Any) -> MarketTide | None:
    if payload is None:
        return None
    d: dict[str, Any] | None = None
    if isinstance(payload, dict):
        inner = payload.get("data", payload)
        if isinstance(inner, dict):
            d = inner
        elif isinstance(inner, list) and inner and isinstance(inner[0], dict):
            d = inner[0]
    elif isinstance(payload, list) and payload and isinstance(payload[0], dict):
        d = payload[0]
    if not d:
        return None
    return MarketTide(
        bullish_premium=_float_from(d, "bullish_premium", "bullishPremium"),
        bearish_premium=_float_from(d, "bearish_premium", "bearishPremium"),
        net_premium=_float_from(d, "net_premium", "netPremium"),
        call_volume=_float_from(d, "call_volume", "callVolume"),
        put_volume=_float_from(d, "put_volume", "putVolume"),
        call_put_ratio=_float_from(d, "call_put_ratio", "callPutRatio", "cp_ratio"),
        raw=dict(d),
    )


def _event_name_high_impact(name: str) -> bool:
    lower = name.lower()
    return any(kw in lower for kw in HIGH_IMPACT_EVENT_KEYWORDS)


def parse_economic_calendar(payload: Any) -> list[EconomicEvent]:
    raw_list = _unwrap_data_list(payload)
    out: list[EconomicEvent] = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        name = _str_from(item, "name", "title", "event", "event_name")
        dt = _str_from(item, "date", "datetime", "release_date", "time")
        hi = _event_name_high_impact(name)
        out.append(EconomicEvent(name=name, date=dt, is_high_impact=hi, raw=dict(item)))
    return out


def parse_sector_etfs(payload: Any, sector_slug: str) -> SectorETF | None:
    raw_list = _unwrap_data_list(payload)
    slug_norm = sector_slug.lower().replace(" ", "-")
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        sec = _str_from(item, "sector", "name", "group")
        sec_cmp = sec.lower().replace(" ", "-")
        if slug_norm in sec_cmp or sec_cmp in slug_norm:
            return SectorETF(
                sector=sec,
                ticker=_str_from(item, "ticker", "symbol"),
                performance_1d=_float_from(
                    item, "performance_1d", "performance1d", "perf_1d", "change_1d"
                ),
                performance_5d=_float_from(
                    item, "performance_5d", "performance5d", "perf_5d", "change_5d"
                ),
                performance_1m=_float_from(
                    item, "performance_1m", "performance1m", "perf_1m", "change_1m"
                ),
                raw=dict(item),
            )
    return None


def parse_fda_calendar(payload: Any, ticker: str) -> list[FDADate]:
    raw_list = _unwrap_data_list(payload)
    t_up = ticker.upper()
    out: list[FDADate] = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        sym = _str_from(item, "ticker", "symbol").upper()
        if sym != t_up:
            continue
        out.append(
            FDADate(
                ticker=sym,
                drug_name=_str_from(item, "drug_name", "drugName", "drug"),
                event_type=_str_from(item, "event_type", "eventType", "type"),
                date=_str_from(item, "date", "datetime"),
                raw=dict(item),
            )
        )
    return out


def _extract_sector_from_info(payload: Any) -> str | None:
    if not payload or not isinstance(payload, dict):
        return None
    data = payload.get("data", payload)
    if isinstance(data, dict):
        s = data.get("sector")
        if s:
            return str(s).strip()
    s = payload.get("sector")
    if s:
        return str(s).strip()
    return None


def _resolve_sector_slug(ticker_sector: str) -> str:
    if ticker_sector in SECTOR_SLUG_MAP:
        return SECTOR_SLUG_MAP[ticker_sector]
    return ticker_sector.lower().replace(" ", "-")


async def _get_json(
    client: httpx.AsyncClient,
    api_token: str,
    path: str,
    params: dict[str, Any] | None = None,
) -> Any | None:
    headers = {
        "Authorization": f"Bearer {api_token}",
        "UW-CLIENT-API-ID": "100001",
    }
    url = f"{UW_BASE}{path}" if path.startswith("/") else f"{UW_BASE}/{path}"
    try:
        r = await client.get(url, headers=headers, params=params or {})
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as e:
        logger.warning(
            "sector_ctx.http_error",
            path=path,
            status_code=e.response.status_code,
        )
        return None
    except Exception as e:
        logger.warning("sector_ctx.request_failed", path=path, error=str(e))
        return None


def _parse_event_date_loose(s: str) -> datetime | None:
    s = (s or "").strip()
    if not s:
        return None
    if "T" in s:
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            pass
    if len(s) >= 10 and s[4] == "-":
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            pass
    try:
        return datetime.strptime(s[:10], "%m/%d/%Y")
    except ValueError:
        return None


def _fda_event_date(f: FDADate) -> datetime | None:
    return _parse_event_date_loose(f.date)


def _filter_future_fda(dates: list[FDADate], ref: date) -> list[FDADate]:
    out: list[FDADate] = []
    for f in dates:
        dt = _fda_event_date(f)
        if dt is None:
            out.append(f)
            continue
        if dt.date() >= ref:
            out.append(f)
    return out


async def build_sector_context(
    ticker: str,
    client: httpx.AsyncClient,
    api_token: str,
    ticker_sector: str | None = None,
    *,
    reference_date: date | None = None,
) -> SectorContext:
    """Fetch UW sector/market/econ data. Never raises; errors go to fetch_errors."""
    ref = reference_date or date.today()
    fetch_errors: list[str] = []
    sector_name = ticker_sector

    if sector_name is None:
        info = await _get_json(client, api_token, f"/api/stock/{ticker}/info")
        if info is None:
            fetch_errors.append("stock_info_failed")
        sector_name = _extract_sector_from_info(info)

    if not sector_name:
        return SectorContext(
            ticker=ticker,
            ticker_sector=None,
            sector_slug="unknown",
            is_biotech=False,
            has_upcoming_fda=False,
            sector_tide=None,
            market_tide=None,
            economic_events=[],
            high_impact_events=[],
            sector_etf=None,
            fda_dates=[],
            fetch_errors=fetch_errors + ["sector_unresolved"],
        )

    sector_slug = _resolve_sector_slug(sector_name)
    is_biotech = sector_name in BIOTECH_SECTORS

    async def fetch_sector_tide() -> SectorTide | None:
        j = await _get_json(client, api_token, f"/api/market/{sector_slug}/sector-tide")
        return parse_sector_tide(j)

    async def fetch_market_tide() -> MarketTide | None:
        j = await _get_json(client, api_token, "/api/market/market-tide")
        return parse_market_tide(j)

    async def fetch_econ() -> list[EconomicEvent]:
        j = await _get_json(client, api_token, "/api/market/economic-calendar")
        return parse_economic_calendar(j)

    async def fetch_etfs() -> SectorETF | None:
        j = await _get_json(client, api_token, "/api/market/sector-etfs")
        return parse_sector_etfs(j, sector_slug)

    async def fetch_fda() -> list[FDADate]:
        j = await _get_json(client, api_token, "/api/market/fda-calendar")
        return parse_fda_calendar(j, ticker)

    if is_biotech:
        results = await asyncio.gather(
            fetch_sector_tide(),
            fetch_market_tide(),
            fetch_econ(),
            fetch_etfs(),
            fetch_fda(),
            return_exceptions=True,
        )
    else:
        r = await asyncio.gather(
            fetch_sector_tide(),
            fetch_market_tide(),
            fetch_econ(),
            fetch_etfs(),
            return_exceptions=True,
        )
        results = (*r, [])

    sector_tide: SectorTide | None = None
    market_tide: MarketTide | None = None
    economic_events: list[EconomicEvent] = []
    sector_etf: SectorETF | None = None
    fda_raw: list[FDADate] = []

    labels = ["sector_tide", "market_tide", "economic", "sector_etfs"]
    if is_biotech:
        labels.append("fda_calendar")

    for i, res in enumerate(results):
        label = labels[i] if i < len(labels) else f"fetched_{i}"
        if isinstance(res, Exception):
            fetch_errors.append(f"{label}:{res!s}")
            continue
        if i == 0:
            sector_tide = res  # type: ignore[assignment]
        elif i == 1:
            market_tide = res  # type: ignore[assignment]
        elif i == 2:
            economic_events = res  # type: ignore[assignment]
        elif i == 3:
            sector_etf = res  # type: ignore[assignment]
        elif i == 4:
            fda_raw = res  # type: ignore[assignment]

    fda_dates = _filter_future_fda(fda_raw, ref)
    has_upcoming_fda = len(fda_dates) > 0

    high_impact_events = [e for e in economic_events if e.is_high_impact]

    return SectorContext(
        ticker=ticker,
        ticker_sector=sector_name,
        sector_slug=sector_slug,
        is_biotech=is_biotech,
        has_upcoming_fda=has_upcoming_fda,
        sector_tide=sector_tide,
        market_tide=market_tide,
        economic_events=economic_events,
        high_impact_events=high_impact_events,
        sector_etf=sector_etf,
        fda_dates=fda_dates,
        fetch_errors=fetch_errors,
    )
