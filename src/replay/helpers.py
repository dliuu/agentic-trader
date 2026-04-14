"""Pure helpers and offline gate-2 scoring for historical pipeline replay."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from grader.agents.flow_analyst import candidate_to_flow
from grader.agents.risk_analyst import (
    extract_days_to_earnings,
    extract_next_earnings_datetime,
    extract_option_chain_data,
    extract_realized_vol,
    score_risk_conviction,
)
from grader.agents.volatility_analyst import VOL_CONFIG, _score_from_context
from grader.context.explainability_ctx import ExplainabilityContext
from grader.context.sector_ctx import parse_sector_tide
from grader.context.vol_ctx import build_vol_context_from_saved_json
from grader.context.sector_cache import SectorBenchmarkCache
from shared.filters import GateThresholds, RiskConvictionConfig
from shared.models import Candidate, SubScore
from scanner.models.flow_alert import FlowAlert
from tracker.models import FlowEvent, FlowWatchResult, Signal


def find_contract(
    chain_data: list[dict[str, Any]],
    strike: float,
    expiry: str,
    option_type: str,
) -> dict[str, Any] | None:
    """Find the specific contract in a flat chain list."""
    exp = expiry[:10]
    ot = option_type.lower()
    for contract in chain_data:
        if not isinstance(contract, dict):
            continue
        c_strike = float(contract.get("strike", 0) or contract.get("strike_price", 0) or 0)
        c_exp = str(contract.get("expiry", contract.get("expiration_date", "")) or "")[:10]
        c_type = str(contract.get("option_type", contract.get("type", "")) or "").lower()
        if c_type in ("calls", "c"):
            c_type = "call"
        if c_type in ("puts", "p"):
            c_type = "put"
        if abs(c_strike - strike) < 0.01 and c_exp == exp and c_type == ot:
            return contract
    return None


def mock_synthesis_score(flow_score: SubScore, vol_score: SubScore, risk_score: SubScore) -> int:
    """Deterministic stand-in for Gate 3 synthesis when ``--mock-llm`` is set."""
    return int(0.6 * flow_score.score + 0.2 * vol_score.score + 0.2 * risk_score.score)


def _headlines_from_backfill_json(
    raw: dict[str, Any] | list[Any] | None,
    *,
    catalyst_lookback_hours: int = 48,
    reference_time: datetime,
) -> list[dict[str, Any]]:
    """Normalize saved headlines (UW ``/api/news/headlines`` or similar) for Gate 1.5."""
    if raw is None:
        return []
    data = raw.get("data", raw) if isinstance(raw, dict) else raw
    if not isinstance(data, list):
        return []
    cutoff = reference_time - timedelta(hours=catalyst_lookback_hours)
    out: list[dict[str, Any]] = []
    for item in data[:40]:
        if not isinstance(item, dict):
            continue
        published_raw = item.get("published_at") or item.get("created_at")
        try:
            published_at = datetime.fromisoformat(str(published_raw).replace("Z", "+00:00"))
            if published_at.tzinfo is None:
                published_at = published_at.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        if published_at < cutoff:
            continue
        title = item.get("headline") or item.get("title") or ""
        out.append(
            {
                "title": str(title),
                "source": str(item.get("source", "unknown")),
                "published_at": published_at.isoformat(),
            }
        )
    return out


def build_explainability_context_for_replay(
    candidate: Candidate,
    *,
    headlines_json: dict[str, Any] | list[Any] | None,
    sector: str | None,
    hot_ticker_count_14d: int,
    earnings_json: dict[str, Any] | None,
    reference_time: datetime,
    catalyst_lookback_hours: int = 48,
) -> ExplainabilityContext:
    """Build Gate 1.5 context from backfill files (no live HTTP)."""
    days_to_earnings = extract_days_to_earnings(earnings_json)
    edt = extract_next_earnings_datetime(earnings_json)
    earnings_date = edt.date().isoformat() if edt else None

    headlines_48h = _headlines_from_backfill_json(
        headlines_json,
        catalyst_lookback_hours=catalyst_lookback_hours,
        reference_time=reference_time,
    )

    return ExplainabilityContext(
        ticker=candidate.ticker.upper(),
        days_to_earnings=days_to_earnings,
        earnings_date=earnings_date,
        flow_alert_count_14d=hot_ticker_count_14d,
        sector=sector,
        sector_call_put_ratio=None,
        headlines_48h=headlines_48h,
        fetch_errors=[],
    )


def apply_sector_tide_from_json(
    ctx: ExplainabilityContext,
    sector_tide_json: dict[str, Any] | None,
) -> None:
    """Mutate ``ctx`` with call/put ratio when a saved sector-tide response is available."""
    if sector_tide_json is None or not ctx.sector:
        return
    tide = parse_sector_tide(sector_tide_json)
    if tide is not None:
        ctx.sector_call_put_ratio = tide.call_put_ratio


def build_flow_watch_result(
    signal: Signal,
    alerts: list[FlowAlert],
    *,
    cutoff: datetime,
    checked_at: datetime,
) -> FlowWatchResult:
    """Build ``FlowWatchResult`` from historical alerts (replay substitute for ``FlowWatcher``)."""
    events: list[FlowEvent] = []
    for alert in alerts:
        if alert.ticker.upper() != signal.ticker.upper():
            continue
        created = alert.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created <= cutoff:
            continue
        opt_raw = str(alert.type or "").lower()
        option_type = "call" if opt_raw in ("call", "calls", "c") else "put"
        fill_type = alert.execution_type
        if fill_type:
            fill_type = str(fill_type).lower()
        is_same_contract = (
            alert.strike == signal.strike
            and alert.expiry == signal.expiry
            and option_type == signal.option_type
        )
        is_same_expiry = alert.expiry == signal.expiry and not is_same_contract
        events.append(
            FlowEvent(
                alert_id=str(alert.id),
                strike=alert.strike,
                expiry=alert.expiry,
                option_type=option_type,
                premium=float(alert.total_premium),
                volume=int(alert.total_size),
                fill_type=fill_type,
                is_same_contract=is_same_contract,
                is_same_expiry=is_same_expiry,
                created_at=created,
            )
        )
    return FlowWatchResult(ticker=signal.ticker, checked_at=checked_at, events=events)


def run_gate2_from_backfill(
    candidate: Candidate,
    flow_score: SubScore,
    chain_raw: dict[str, Any] | None,
    vol_stats_raw: dict[str, Any] | None,
    sector_cache: SectorBenchmarkCache,
    *,
    risk_cfg: RiskConvictionConfig | None = None,
    gate_cfg: GateThresholds | None = None,
) -> tuple[bool, SubScore, SubScore]:
    """Run Gate 2 using saved chain + vol stats (replay)."""
    chains_payload = chain_raw if isinstance(chain_raw, dict) else {}
    vol_payload = vol_stats_raw if isinstance(vol_stats_raw, dict) else None

    fc = candidate_to_flow(candidate)
    risk_ctx = {
        "option_chain_data": extract_option_chain_data(chains_payload or None, fc),
        "annualized_realized_vol": extract_realized_vol(vol_payload),
        "days_to_earnings": extract_days_to_earnings(None),
    }
    risk_score = score_risk_conviction(
        candidate=fc,
        option_chain_data=risk_ctx["option_chain_data"],
        annualized_realized_vol=risk_ctx["annualized_realized_vol"],
        days_to_earnings=risk_ctx["days_to_earnings"],
        cfg=risk_cfg,
    )

    vctx = build_vol_context_from_saved_json(
        candidate,
        chain_response=chains_payload,
        vol_stats_response=vol_payload,
    )
    if vctx is None:
        vol_score = SubScore(
            agent="volatility_analyst",
            score=50,
            rationale="Replay: incomplete vol/chain snapshot — neutral score.",
            signals=[],
            skipped=True,
            skip_reason="replay_vol_context_unavailable",
        )
    else:
        vol_score = _score_from_context(vctx, sector_cache, VOL_CONFIG)

    gcfg = gate_cfg or GateThresholds()
    if risk_score.untradeable or risk_score.recommended_position_size == 0.0:
        return False, vol_score, risk_score

    gate_avg = (flow_score.score + vol_score.score + risk_score.score) / 3
    passed = gate_avg >= gcfg.deterministic_avg_min
    return passed, vol_score, risk_score


def load_json_file(path: Any) -> Any | None:
    """Load JSON from path; return None if missing."""
    from pathlib import Path

    p = Path(path)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def hot_ticker_count_for_date(
    ticker_flow_dates: dict[str, list[str]],
    ticker: str,
    current_date: str,
    lookback_days: int = 14,
) -> int:
    """Count distinct backfill dates with flow for ``ticker`` in the lookback window."""
    from datetime import date as date_cls

    t = ticker.upper()
    cur = date_cls.fromisoformat(current_date[:10])
    start = cur - timedelta(days=lookback_days)
    dates = ticker_flow_dates.get(t, [])
    n = 0
    for d in dates:
        try:
            dd = date_cls.fromisoformat(d[:10])
        except ValueError:
            continue
        if start <= dd <= cur:
            n += 1
    return n
