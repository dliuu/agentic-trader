"""Insider + congressional context for Gate 3 insider tracker."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import structlog

from shared.finnhub_client import FinnhubClient
from shared.filters import InsiderScoringConfig
from shared.models import Candidate, SubScore

log = structlog.get_logger()

UW_BASE = "https://api.unusualwhales.com"


@dataclass
class DerivedInsiderSignals:
    """Deterministic pre-computation to reduce LLM reasoning burden."""

    cluster_buys: list[dict] = field(default_factory=list)
    cluster_sells: list[dict] = field(default_factory=list)

    buy_count_90d: int = 0
    sell_count_90d: int = 0
    buy_sell_ratio: float | None = None
    net_insider_value_90d: float = 0.0

    insider_trades_before_flow: list[dict] = field(default_factory=list)
    insider_trades_after_flow: list[dict] = field(default_factory=list)
    days_since_last_insider_buy: int | None = None
    days_since_last_insider_sell: int | None = None

    num_political_holders: int = 0
    political_holder_names: list[str] = field(default_factory=list)
    recent_congressional_trades: list[dict] = field(default_factory=list)
    congressional_direction: str | None = None

    uw_finnhub_agreement: bool | None = None

    mspr_current: float | None = None
    mspr_trend: str | None = None

    total_insider_transactions: int = 0
    data_freshness_days: int | None = None
    has_sufficient_data: bool = False


@dataclass
class InsiderContext:
    """Everything the insider tracker LLM needs to grade a candidate."""

    ticker: str
    option_type: str
    trade_direction: str
    scanned_at: datetime

    form4_filings: list[dict]
    buy_sell_summary: dict | None
    insider_flow: list[dict] | None

    political_holders: list[dict]
    congressional_trades: list[dict]

    finnhub_transactions: list[dict]
    finnhub_mspr: dict | None

    derived: DerivedInsiderSignals
    data_availability: dict[str, bool]


def _infer_trade_direction(candidate: Candidate) -> str:
    return candidate.direction if candidate.direction in ("bullish", "bearish") else "bullish"


def _infer_option_type(candidate: Candidate) -> str:
    return "call" if candidate.direction == "bullish" else "put"


def _safe_list(result: Any) -> list:
    if isinstance(result, Exception):
        return []
    if isinstance(result, list):
        return result
    return []


def _safe_dict(result: Any) -> dict | None:
    if isinstance(result, Exception):
        return None
    if isinstance(result, dict):
        return result
    return None


def _uw_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "UW-CLIENT-API-ID": "100001",
        "Accept": "application/json",
    }


def _extract_list(payload: Any) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return data
    return []


def _extract_dict_payload(payload: Any) -> dict | None:
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            return data
        return payload
    return None


def _parse_date(val: str | None) -> datetime | None:
    if not val:
        return None
    s = str(val).strip()
    if s.endswith("Z"):
        s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        pass
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_date_naive(val: str | None) -> datetime | None:
    """Parse for comparison; normalize to UTC date boundaries."""
    dt = _parse_date(val)
    if dt is None:
        return None
    return dt


def _detect_clusters(
    transactions: list[dict],
    window_days: int = 14,
    min_insiders: int = 2,
    direction: str = "buy",
) -> list[dict]:
    code = "P" if direction == "buy" else "S"
    relevant: list[dict] = []
    for t in transactions:
        tt = str(t.get("transaction_type", "")).upper()
        if tt == code:
            relevant.append(t)

    if len(relevant) < min_insiders:
        return []

    def sort_key(t: dict) -> str:
        return str(t.get("filing_date") or t.get("filed_at") or "")

    relevant.sort(key=sort_key)

    clusters: list[dict] = []
    i = 0
    while i < len(relevant):
        window_start = _parse_date_naive(
            str(relevant[i].get("filing_date") or relevant[i].get("filed_at") or "")
        )
        if window_start is None:
            i += 1
            continue
        window_end = window_start + timedelta(days=window_days)

        window_txns: list[dict] = []
        j = i
        while j < len(relevant):
            txn_date = _parse_date_naive(
                str(relevant[j].get("filing_date") or relevant[j].get("filed_at") or "")
            )
            if txn_date is None:
                j += 1
                continue
            if txn_date <= window_end:
                window_txns.append(relevant[j])
                j += 1
            else:
                break

        unique_insiders = {str(t.get("insider_name") or t.get("name") or "") for t in window_txns}
        unique_insiders.discard("")
        if len(unique_insiders) >= min_insiders:
            total_value = sum(float(t.get("value", 0) or 0) for t in window_txns)
            clusters.append(
                {
                    "start_date": str(relevant[i].get("filing_date") or relevant[i].get("filed_at")),
                    "end_date": str(relevant[j - 1].get("filing_date") or relevant[j - 1].get("filed_at")),
                    "insiders": sorted(unique_insiders),
                    "total_value": total_value,
                    "transaction_count": len(window_txns),
                }
            )
            i = j
        else:
            i += 1

    return clusters


def _cross_validate_sources(
    uw_transactions: list[dict],
    finnhub_transactions: list[dict],
    lookback_days: int = 90,
) -> bool | None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    uw_buys = 0
    uw_sells = 0
    for t in uw_transactions:
        if str(t.get("transaction_type", "")).upper() != "P":
            continue
        fd = _parse_date_naive(str(t.get("filing_date") or t.get("filed_at") or ""))
        if fd and fd >= cutoff:
            uw_buys += 1
    for t in uw_transactions:
        if str(t.get("transaction_type", "")).upper() != "S":
            continue
        fd = _parse_date_naive(str(t.get("filing_date") or t.get("filed_at") or ""))
        if fd and fd >= cutoff:
            uw_sells += 1

    fh_buys = 0
    fh_sells = 0
    for t in finnhub_transactions:
        ch = float(t.get("change", 0) or 0)
        fd = _parse_date_naive(str(t.get("filingDate") or t.get("transactionDate") or ""))
        if fd is None or fd < cutoff:
            continue
        if ch > 0:
            fh_buys += 1
        elif ch < 0:
            fh_sells += 1

    if uw_buys + uw_sells == 0 or fh_buys + fh_sells == 0:
        return None

    uw_direction = "buy" if uw_buys > uw_sells else "sell"
    fh_direction = "buy" if fh_buys > fh_sells else "sell"
    return uw_direction == fh_direction


def _normalize_uw_form4_row(item: dict) -> dict:
    return {
        "insider_name": item.get("insider_name") or item.get("name") or "",
        "title": item.get("insider_title") or item.get("title") or "",
        "transaction_type": str(item.get("transaction_type") or item.get("trade_type") or "").upper(),
        "filing_date": str(item.get("filing_date") or item.get("filed_at") or item.get("created_at") or ""),
        "shares": int(float(item.get("shares", 0) or 0)),
        "value": float(item.get("value", 0) or 0),
        "source": "uw_form4",
    }


def _normalize_finnhub_row(item: dict) -> dict:
    ch = float(item.get("change", 0) or 0)
    if ch > 0:
        tt = "P"
    elif ch < 0:
        tt = "S"
    else:
        tt = "M"
    filing = str(item.get("filingDate") or item.get("transactionDate") or "")
    return {
        "insider_name": item.get("name") or "",
        "title": str(item.get("position", "") or item.get("title", "") or ""),
        "transaction_type": tt,
        "filing_date": filing,
        "shares": abs(int(float(item.get("change", 0) or 0))),
        "value": abs(float(item.get("transactionPrice", 0) or 0) * float(item.get("change", 0) or 0))
        if item.get("transactionPrice")
        else float(item.get("value", 0) or 0),
        "source": "finnhub",
    }


def _txn_sort_date(t: dict) -> str:
    return str(t.get("date") or t.get("filing_date") or "")


def _merge_and_dedup_transactions(
    form4: list[dict],
    finnhub_raw: list[dict],
) -> list[dict]:
    uw_norm = [_normalize_uw_form4_row(x) for x in form4]
    fh_norm = [_normalize_finnhub_row(x) for x in finnhub_raw]

    def key(t: dict) -> tuple[str, str, int]:
        name = str(t.get("insider_name", "")).lower()
        d = str(t.get("filing_date", ""))[:10]
        shares = int(t.get("shares", 0) or 0)
        return (name, d, shares)

    merged: dict[tuple[str, str, int], dict] = {}
    for t in uw_norm + fh_norm:
        if not t.get("insider_name"):
            continue
        k = key(t)
        if k not in merged:
            t2 = dict(t)
            t2["date"] = str(t2.get("filing_date", ""))[:10]
            merged[k] = t2

    out = list(merged.values())
    out.sort(key=_txn_sort_date, reverse=True)
    return out


def _days_between(later: datetime, earlier: datetime) -> int:
    return max(0, (later.date() - earlier.date()).days)


def _compute_derived_signals(
    form4: list[dict],
    _buy_sell_summary: dict | None,
    _insider_flow: list[dict] | None,
    pol_holders: list[dict],
    cong_trades: list[dict],
    fh_transactions: list[dict],
    fh_mspr: dict | None,
    candidate: Candidate,
    cfg: InsiderScoringConfig,
) -> DerivedInsiderSignals:
    derived = DerivedInsiderSignals()
    now = datetime.now(timezone.utc)
    scanned = candidate.scanned_at
    if scanned.tzinfo is None:
        scanned = scanned.replace(tzinfo=timezone.utc)

    normalized_form4 = [_normalize_uw_form4_row(x) for x in form4]

    ratio_cutoff = now - timedelta(days=cfg.ratio_lookback_days)
    tx180_cutoff = now - timedelta(days=cfg.transaction_lookback_days)
    cong_cutoff = now - timedelta(days=cfg.congressional_lookback_days)

    for t in normalized_form4:
        fd = _parse_date_naive(str(t.get("filing_date", "")))
        if fd is None:
            continue
        if fd >= ratio_cutoff:
            tt = str(t.get("transaction_type", "")).upper()
            if tt == "P":
                derived.buy_count_90d += 1
                derived.net_insider_value_90d += float(t.get("value", 0) or 0)
            elif tt == "S":
                derived.sell_count_90d += 1
                derived.net_insider_value_90d -= float(t.get("value", 0) or 0)

    if derived.buy_count_90d + derived.sell_count_90d > 0:
        if derived.sell_count_90d == 0:
            derived.buy_sell_ratio = float(derived.buy_count_90d) if derived.buy_count_90d else None
        else:
            derived.buy_sell_ratio = derived.buy_count_90d / derived.sell_count_90d
    else:
        derived.buy_sell_ratio = None

    merged_for_clusters = list(normalized_form4)
    derived.cluster_buys = _detect_clusters(
        merged_for_clusters,
        window_days=cfg.cluster_window_days,
        min_insiders=cfg.cluster_min_insiders,
        direction="buy",
    )
    derived.cluster_sells = _detect_clusters(
        merged_for_clusters,
        window_days=cfg.cluster_window_days,
        min_insiders=cfg.cluster_min_insiders,
        direction="sell",
    )

    all_for_180: list[dict] = []
    for t in normalized_form4:
        fd = _parse_date_naive(str(t.get("filing_date", "")))
        if fd and fd >= tx180_cutoff:
            all_for_180.append(dict(t))

    derived.total_insider_transactions = len(all_for_180)

    last_buy: datetime | None = None
    last_sell: datetime | None = None
    for t in normalized_form4:
        fd = _parse_date_naive(str(t.get("filing_date", "")))
        if fd is None:
            continue
        tt = str(t.get("transaction_type", "")).upper()
        if tt == "P" and (last_buy is None or fd > last_buy):
            last_buy = fd
        if tt == "S" and (last_sell is None or fd > last_sell):
            last_sell = fd

    if last_buy:
        derived.days_since_last_insider_buy = _days_between(now, last_buy)
    if last_sell:
        derived.days_since_last_insider_sell = _days_between(now, last_sell)

    before: list[dict] = []
    after: list[dict] = []
    for t in all_for_180:
        fd = _parse_date_naive(str(t.get("filing_date", "")))
        if fd is None:
            continue
        row = dict(t)
        if fd <= scanned:
            before.append(row)
        else:
            after.append(row)
    derived.insider_trades_before_flow = sorted(before, key=lambda x: str(x.get("filing_date", "")), reverse=True)[
        :20
    ]
    derived.insider_trades_after_flow = sorted(after, key=lambda x: str(x.get("filing_date", "")), reverse=True)[
        :20
    ]

    derived.num_political_holders = len(pol_holders)
    derived.political_holder_names = [
        str(h.get("politician") or h.get("name") or "") for h in pol_holders if h.get("politician") or h.get("name")
    ]

    recent_cong: list[dict] = []
    cong_buys = 0
    cong_sells = 0
    for t in cong_trades:
        fd = _parse_date_naive(
            str(t.get("filing_date") or t.get("filed_at") or t.get("created_at") or "")
        )
        if fd is None or fd < cong_cutoff:
            continue
        recent_cong.append(t)
        tx = str(t.get("transaction_type") or t.get("trade_type") or "").lower()
        if any(x in tx for x in ("buy", "purchase", "p")):
            cong_buys += 1
        elif any(x in tx for x in ("sell", "sale", "s")):
            cong_sells += 1

    derived.recent_congressional_trades = recent_cong[:20]
    if cong_buys + cong_sells > 0:
        if cong_buys > cong_sells:
            derived.congressional_direction = "buying"
        elif cong_sells > cong_buys:
            derived.congressional_direction = "selling"
        else:
            derived.congressional_direction = "mixed"

    derived.uw_finnhub_agreement = _cross_validate_sources(
        normalized_form4,
        fh_transactions,
        lookback_days=cfg.ratio_lookback_days,
    )

    if fh_mspr:
        series = fh_mspr.get("data")
        if isinstance(series, list) and series:
            def sort_key(x: dict) -> tuple[int, int]:
                return (int(x.get("year", 0) or 0), int(x.get("month", 0) or 0))

            sorted_m = sorted(series, key=sort_key)
            last = sorted_m[-1]
            prev = sorted_m[-2] if len(sorted_m) >= 2 else None
            mcur = last.get("mspr")
            if mcur is None:
                mcur = last.get("change")
            derived.mspr_current = float(mcur) if mcur is not None else None
            if prev is not None and derived.mspr_current is not None:
                pm = prev.get("mspr")
                if pm is None:
                    pm = prev.get("change")
                if pm is not None:
                    prev_f = float(pm)
                    if derived.mspr_current > prev_f + 1:
                        derived.mspr_trend = "improving"
                    elif derived.mspr_current < prev_f - 1:
                        derived.mspr_trend = "declining"
                    else:
                        derived.mspr_trend = "stable"

    dates: list[datetime] = []
    for t in normalized_form4:
        fd = _parse_date_naive(str(t.get("filing_date", "")))
        if fd:
            dates.append(fd)
    for t in fh_transactions:
        fd = _parse_date_naive(str(t.get("filingDate") or t.get("transactionDate") or ""))
        if fd:
            dates.append(fd)
    if dates:
        most_recent = max(dates)
        derived.data_freshness_days = _days_between(now, most_recent)

    fh_in_range = 0
    for t in fh_transactions:
        fd = _parse_date_naive(str(t.get("filingDate") or t.get("transactionDate") or ""))
        if fd and fd >= tx180_cutoff:
            fh_in_range += 1

    derived.has_sufficient_data = (derived.total_insider_transactions + fh_in_range) >= cfg.min_transactions_for_analysis

    return derived


async def _fetch_uw_form4(client: httpx.AsyncClient, headers: dict[str, str], ticker: str) -> list[dict]:
    try:
        resp = await client.get(f"{UW_BASE}/api/insider/{ticker}", headers=headers, timeout=15.0)
        if resp.status_code >= 400:
            return []
        return _extract_list(resp.json())
    except Exception as e:
        log.warning("insider_ctx.uw_form4_failed", ticker=ticker, error=str(e))
        return []


async def _fetch_uw_buy_sells(client: httpx.AsyncClient, headers: dict[str, str], ticker: str) -> dict | None:
    try:
        resp = await client.get(
            f"{UW_BASE}/api/stock/{ticker}/insider-buy-sells",
            headers=headers,
            timeout=15.0,
        )
        if resp.status_code >= 400:
            return None
        return _extract_dict_payload(resp.json())
    except Exception as e:
        log.warning("insider_ctx.uw_buy_sells_failed", ticker=ticker, error=str(e))
        return None


async def _fetch_uw_insider_flow(client: httpx.AsyncClient, headers: dict[str, str], ticker: str) -> list[dict]:
    try:
        resp = await client.get(
            f"{UW_BASE}/api/insider/{ticker}/ticker-flow",
            headers=headers,
            timeout=15.0,
        )
        if resp.status_code >= 400:
            return []
        return _extract_list(resp.json())
    except Exception as e:
        log.warning("insider_ctx.uw_flow_failed", ticker=ticker, error=str(e))
        return []


async def _fetch_uw_political_holders(
    client: httpx.AsyncClient, headers: dict[str, str], ticker: str
) -> list[dict]:
    try:
        resp = await client.get(
            f"{UW_BASE}/api/politician-portfolios/holders/{ticker}",
            headers=headers,
            timeout=15.0,
        )
        if resp.status_code >= 400:
            return []
        return _extract_list(resp.json())
    except Exception as e:
        log.warning("insider_ctx.uw_political_failed", ticker=ticker, error=str(e))
        return []


async def _fetch_uw_congressional_trades(
    client: httpx.AsyncClient, headers: dict[str, str], ticker: str
) -> list[dict]:
    try:
        resp = await client.get(
            f"{UW_BASE}/api/congress/recent-trades",
            headers=headers,
            params={"limit": 500},
            timeout=20.0,
        )
        if resp.status_code >= 400:
            return []
        rows = _extract_list(resp.json())
        t_up = ticker.upper()
        out: list[dict] = []
        for row in rows:
            sym = str(row.get("ticker") or row.get("symbol") or row.get("ticker_symbol") or "").upper()
            if sym == t_up:
                out.append(row)
        return out
    except Exception as e:
        log.warning("insider_ctx.uw_congress_failed", ticker=ticker, error=str(e))
        return []


async def _fetch_finnhub_transactions(fh: FinnhubClient, ticker: str) -> list[dict]:
    try:
        return await fh.stock_insider_transactions(ticker)
    except Exception as e:
        log.warning("insider_ctx.finnhub_tx_failed", ticker=ticker, error=str(e))
        return []


async def _fetch_finnhub_mspr(fh: FinnhubClient, ticker: str) -> dict | None:
    try:
        return await fh.stock_insider_sentiment(ticker)
    except Exception as e:
        log.warning("insider_ctx.finnhub_mspr_failed", ticker=ticker, error=str(e))
        return None


async def build_insider_context(
    candidate: Candidate,
    uw_client: httpx.AsyncClient,
    uw_api_token: str,
    finnhub_client: FinnhubClient,
    config: InsiderScoringConfig | None = None,
) -> InsiderContext:
    """Fetch all insider data sources in parallel. Never raises — empty collections on failure."""
    cfg = config or InsiderScoringConfig()
    headers = _uw_headers(uw_api_token)
    ticker = candidate.ticker.upper()

    results = await asyncio.gather(
        _fetch_uw_form4(uw_client, headers, ticker),
        _fetch_uw_buy_sells(uw_client, headers, ticker),
        _fetch_uw_insider_flow(uw_client, headers, ticker),
        _fetch_uw_political_holders(uw_client, headers, ticker),
        _fetch_uw_congressional_trades(uw_client, headers, ticker),
        _fetch_finnhub_transactions(finnhub_client, ticker),
        _fetch_finnhub_mspr(finnhub_client, ticker),
        return_exceptions=True,
    )

    form4 = _safe_list(results[0])
    buy_sells = _safe_dict(results[1])
    insider_flow = _safe_list(results[2])
    pol_holders = _safe_list(results[3])
    cong_trades = _safe_list(results[4])
    fh_transactions = _safe_list(results[5])
    fh_mspr = _safe_dict(results[6])

    availability = {
        "uw_form4": len(form4) > 0,
        "uw_buy_sells": buy_sells is not None,
        "uw_insider_flow": len(insider_flow) > 0,
        "uw_political_holders": len(pol_holders) > 0,
        "uw_congressional_trades": len(cong_trades) > 0,
        "finnhub_transactions": len(fh_transactions) > 0,
        "finnhub_mspr": fh_mspr is not None,
    }

    derived = _compute_derived_signals(
        form4,
        buy_sells,
        insider_flow,
        pol_holders,
        cong_trades,
        fh_transactions,
        fh_mspr,
        candidate,
        cfg,
    )

    return InsiderContext(
        ticker=ticker,
        option_type=_infer_option_type(candidate),
        trade_direction=_infer_trade_direction(candidate),
        scanned_at=candidate.scanned_at,
        form4_filings=form4,
        buy_sell_summary=buy_sells,
        insider_flow=insider_flow,
        political_holders=pol_holders,
        congressional_trades=cong_trades,
        finnhub_transactions=fh_transactions,
        finnhub_mspr=fh_mspr,
        derived=derived,
        data_availability=availability,
    )


def should_skip_insider_analysis(ctx: InsiderContext) -> tuple[bool, str]:
    """Determine if we should skip the LLM call."""
    if not ctx.derived.has_sufficient_data:
        if ctx.derived.num_political_holders == 0 and len(ctx.derived.recent_congressional_trades) == 0:
            return True, "No insider or congressional data available for this ticker"
    return False, ""


def make_skip_score() -> SubScore:
    """Neutral score when no data is available."""
    return SubScore(
        agent="insider_tracker",
        score=50,
        rationale=(
            "No insider transaction or congressional trading data available for this ticker. "
            "Score is neutral — absence of insider activity is not inherently negative."
        ),
        signals=[],
        skipped=True,
        skip_reason="No insider or congressional data available",
    )


def build_cluster_details_section(ctx: InsiderContext) -> str:
    lines: list[str] = []
    if ctx.derived.cluster_buys:
        lines.append("### Cluster buys")
        for c in ctx.derived.cluster_buys[:5]:
            lines.append(
                f"- {c.get('start_date')} → {c.get('end_date')}: "
                f"{len(c.get('insiders', []))} insiders, "
                f"${float(c.get('total_value', 0)):,.0f} ({c.get('transaction_count')} txns)"
            )
    else:
        lines.append("### Cluster buys\n(None)")

    if ctx.derived.cluster_sells:
        lines.append("### Cluster sells")
        for c in ctx.derived.cluster_sells[:5]:
            lines.append(
                f"- {c.get('start_date')} → {c.get('end_date')}: "
                f"{len(c.get('insiders', []))} insiders, "
                f"${float(c.get('total_value', 0)):,.0f} ({c.get('transaction_count')} txns)"
            )
    else:
        lines.append("### Cluster sells\n(None)")

    return "\n".join(lines)


def build_insider_transactions_section(ctx: InsiderContext, max_rows: int = 20) -> str:
    merged = _merge_and_dedup_transactions(ctx.form4_filings, ctx.finnhub_transactions)
    if not merged:
        return "(No insider transactions found for this ticker)"

    lines: list[str] = []
    for t in merged[:max_rows]:
        lines.append(
            f"- {t.get('date', '')} | {t.get('insider_name', '')} ({t.get('title', '')}) | "
            f"{t.get('transaction_type', '')} | {int(t.get('shares', 0)):,} shares | "
            f"${float(t.get('value', 0) or 0):,.0f} | Source: {t.get('source', '')}"
        )
    return "\n".join(lines)


def build_congressional_section(ctx: InsiderContext) -> str:
    parts: list[str] = []
    if ctx.political_holders:
        parts.append("### Current Political Holders")
        for h in ctx.political_holders:
            parts.append(
                f"- {h.get('politician', h.get('name', 'Unknown'))} "
                f"({h.get('party', '?')}, {h.get('chamber', '?')})"
            )
    else:
        parts.append("### Current Political Holders\n(None)")

    parts.append("")

    if ctx.congressional_trades:
        parts.append("### Recent Congressional Trades (this ticker)")
        for t in ctx.congressional_trades[:10]:
            parts.append(
                f"- {t.get('filing_date', t.get('filed_at', '?'))} | "
                f"{t.get('politician', t.get('name', '?'))} | "
                f"{t.get('transaction_type', t.get('trade_type', '?'))} | "
                f"{t.get('amount', t.get('value', '?'))}"
            )
    else:
        parts.append("### Recent Congressional Trades (this ticker)\n(None found)")

    return "\n".join(parts)


def build_data_availability_section(ctx: InsiderContext) -> str:
    lines: list[str] = []
    for source, available in ctx.data_availability.items():
        status = "✓ Available" if available else "✗ No data"
        lines.append(f"- {source}: {status}")
    return "\n".join(lines)


def format_trades_list(trades: list[dict]) -> str:
    if not trades:
        return "(None)"
    lines: list[str] = []
    for t in trades[:15]:
        lines.append(
            f"- {t.get('filing_date', '')} | {t.get('insider_name', '')} | "
            f"{t.get('transaction_type', '')} | ${float(t.get('value', 0) or 0):,.0f}"
        )
    return "\n".join(lines)
