"""Library entry point for full-pipeline replay (used by ``scripts/replay.py`` and sweeps)."""

from __future__ import annotations

import csv
import json
import re
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _day_start_utc(d: str) -> datetime:
    y, m, dd = (int(x) for x in d.split("-"))
    return datetime(y, m, dd, 0, 0, 0, tzinfo=timezone.utc)


def _day_end_utc(d: str) -> datetime:
    y, m, dd = (int(x) for x in d.split("-"))
    return datetime(y, m, dd, 23, 59, 59, tzinfo=timezone.utc)


async def run_replay_pipeline(
    *,
    data_dir: Path,
    output_dir: Path,
    config: dict,
    mock_llm: bool = True,
) -> dict[str, Any]:
    """Run replay; ``config`` must be a loaded rules dict (e.g. ``load_config``)."""
    if not mock_llm:
        raise ValueError("Live Gate 3 replay is not implemented; use mock_llm=True.")

    data_dir = Path(data_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    replay_db = output_dir / "replay.db"

    from grader.context.sector_cache import SectorBenchmarkCache
    from grader.gate0 import run_gate0
    from grader.gate1 import run_gate1
    from grader.gate1_5 import run_gate1_5
    from grader.models import GradeResponse, ScoredTrade, TradeRiskParams
    from replay.helpers import (
        build_explainability_context_for_replay,
        build_flow_watch_result,
        hot_ticker_count_for_date,
        load_json_file,
        mock_synthesis_score,
        run_gate2_from_backfill,
    )
    from scanner.models.flow_alert import FlowAlert
    from scanner.models.market_tide import MarketTide
    from scanner.rules.confluence import ConfluenceEnricher
    from scanner.rules.engine import RuleEngine
    from scanner.state.dedup import DedupCache
    from shared.config import gate_thresholds_from_config
    from tracker.chain_poller import ChainPoller
    from tracker.config import load_tracker_config
    from tracker.conviction import ConvictionEngine
    from tracker.flow_ledger import FlowLedger, ledger_entry_from_flow_alert
    from tracker.intake import _process_scored_trade
    from tracker.models import SignalSnapshot, SignalState
    from tracker.signal_store import SignalStore

    gate_thr = gate_thresholds_from_config(config)
    tracker_cfg = load_tracker_config(config)

    day_dirs = sorted(
        p.name
        for p in data_dir.iterdir()
        if p.is_dir() and ISO_DATE.match(p.name) and (p / "flow_alerts.json").is_file()
    )
    if not day_dirs:
        return {
            "ok": False,
            "error": f"No YYYY-MM-DD directories with flow_alerts.json under {data_dir}",
            "signals_created": 0,
        }

    out_dir = output_dir
    store = SignalStore(db_path=str(replay_db))
    ledger = FlowLedger(db_path=str(replay_db))
    engine = RuleEngine(config)
    enricher = ConfluenceEnricher(config)
    dedup = DedupCache(
        ttl_minutes=int(config.get("dedup", {}).get("ttl_minutes", 60)),
        key_fields=list(config.get("dedup", {}).get("key_fields", ["ticker", "strike", "expiry", "direction"])),
    )

    sector_cache = SectorBenchmarkCache(
        benchmarks={},
        market_iv_rank=50.0,
        market_iv=0.25,
        market_iv_rv_ratio=1.0,
        refreshed_at=datetime.now(timezone.utc),
        ticker_snapshots=[],
    )

    ticker_flow_dates: dict[str, list[str]] = defaultdict(list)
    for ds in day_dirs:
        raw = load_json_file(data_dir / ds / "flow_alerts.json")
        if not isinstance(raw, list):
            raw = (raw or {}).get("data", []) if isinstance(raw, dict) else []
        tickers: set[str] = set()
        for item in raw:
            if isinstance(item, dict) and item.get("ticker"):
                tickers.add(str(item["ticker"]).upper())
        for t in tickers:
            ticker_flow_dates[t].append(ds)

    conviction_engine = ConvictionEngine(tracker_cfg)
    replay_log: list[dict[str, Any]] = []
    traj_rows: list[dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as http_client:
        day_num = 0
        for ds in day_dirs:
            day_num += 1
            sim_end = _day_end_utc(ds)
            sim_created = _day_start_utc(ds).replace(hour=14, minute=0)

            raw_flow = load_json_file(data_dir / ds / "flow_alerts.json")
            raw_list = raw_flow if isinstance(raw_flow, list) else (raw_flow or {}).get("data", [])
            alerts: list[FlowAlert] = []
            for item in raw_list:
                try:
                    alerts.append(FlowAlert.model_validate(item))
                except Exception as e:
                    replay_log.append({"date": ds, "event": "alert_parse_skip", "error": str(e)})

            watched = await store.get_watched_tickers()
            watched_by_id: dict[str, FlowAlert] = {}
            for a in alerts:
                if a.ticker.upper() in watched:
                    watched_by_id[a.id] = a
            discovery = [a for a in alerts if a.ticker.upper() not in watched]
            new_alerts: list[FlowAlert] = []
            for alert in discovery:
                key_data = {
                    "ticker": alert.ticker,
                    "strike": alert.strike,
                    "expiry": alert.expiry,
                    "direction": alert.direction,
                }
                if not dedup.is_duplicate(key_data):
                    new_alerts.append(alert)

            candidates = engine.evaluate_batch(new_alerts)
            tide = MarketTide(direction="neutral")
            candidates = [enricher.enrich(c, [], tide) for c in candidates]

            scored_count = 0
            for candidate in candidates:
                day_path = data_dir / ds
                stock_path = day_path / "stock_info" / f"{candidate.ticker.upper()}.json"
                stock_info = load_json_file(stock_path)

                g0 = await run_gate0(
                    candidate,
                    http_client,
                    config.get("uw_api_token") or "",
                    stock_info_json=stock_info if isinstance(stock_info, dict) else None,
                )
                if not g0.passed:
                    continue

                passed_g1, flow_score = await run_gate1(candidate, gate_cfg=gate_thr)
                if not passed_g1:
                    continue

                hot_n = hot_ticker_count_for_date(ticker_flow_dates, candidate.ticker, ds)
                headlines_raw = load_json_file(day_path / "headlines" / f"{candidate.ticker.upper()}.json")
                exp_ctx = build_explainability_context_for_replay(
                    candidate,
                    headlines_json=headlines_raw if isinstance(headlines_raw, (dict, list)) else None,
                    sector=g0.sector,
                    hot_ticker_count_14d=hot_n,
                    earnings_json=None,
                    reference_time=sim_end,
                )
                g15 = await run_gate1_5(
                    candidate,
                    flow_score,
                    http_client,
                    config.get("uw_api_token") or "",
                    scanner_db_path=None,
                    sector=g0.sector,
                    explainability_ctx_override=exp_ctx,
                    gate_cfg=gate_thr,
                )
                if not g15.passed:
                    continue

                chain_raw = load_json_file(day_path / "chains" / f"{candidate.ticker.upper()}.json")
                vol_raw = load_json_file(day_path / "vol_stats" / f"{candidate.ticker.upper()}.json")
                passed_g2, vol_score, risk_score = run_gate2_from_backfill(
                    candidate,
                    flow_score,
                    chain_raw if isinstance(chain_raw, dict) else {},
                    vol_raw if isinstance(vol_raw, dict) else None,
                    sector_cache,
                    gate_cfg=gate_thr,
                )
                if not passed_g2:
                    continue

                syn = mock_synthesis_score(flow_score, vol_score, risk_score)
                threshold = int((config.get("grader") or {}).get("score_threshold", gate_thr.final_score_min))
                if syn < threshold:
                    continue
                grade = GradeResponse(
                    score=max(1, min(100, syn)),
                    verdict="pass",
                    rationale="mock-llm replay synthesis",
                    signals_confirmed=[s.rule_name for s in candidate.signals],
                    likely_directional=True,
                )
                risk_params = TradeRiskParams(
                    recommended_position_size=float(risk_score.recommended_position_size),
                    recommended_stop_loss_pct=float(risk_score.recommended_stop_loss_pct),
                    max_entry_spread_pct=float(risk_score.max_entry_spread_pct),
                )
                st = ScoredTrade(
                    candidate=candidate,
                    grade=grade,
                    risk=risk_params,
                    graded_at=sim_end,
                    model_used="mock-llm-replay",
                    latency_ms=0,
                    input_tokens=0,
                    output_tokens=0,
                )

                await _process_scored_trade(st, store, tracker_cfg, created_at_override=sim_created)
                scored_count += 1

            # Ledger for watched tickers (same day flow)
            ticker_map = await store.get_ticker_signal_map()
            signal_by_ticker: dict[str, Any] = {}
            for t, sid in ticker_map.items():
                sig = await store.get_signal(sid)
                if sig is not None:
                    signal_by_ticker[t] = sig
            entries = []
            for alert in alerts:
                tu = alert.ticker.upper()
                if tu not in ticker_map:
                    continue
                sig = signal_by_ticker.get(tu)
                if sig is None:
                    continue
                if await ledger.has_alert(str(alert.id)):
                    continue
                entries.append(
                    ledger_entry_from_flow_alert(
                        alert,
                        signal_id=sig.id,
                        signal=sig,
                        source="replay",
                        recorded_at=sim_end,
                    )
                )
            if entries:
                await ledger.record_batch(entries)

            active = await store.get_active_signals()
            poller = ChainPoller(http_client, "replay-token", tracker_cfg)
            for sig in active:
                chain_fp = data_dir / ds / "chains" / f"{sig.ticker.upper()}.json"
                raw_chain = load_json_file(chain_fp)
                if not isinstance(raw_chain, dict):
                    replay_log.append(
                        {"date": ds, "event": "chain_missing", "ticker": sig.ticker, "signal_id": sig.id}
                    )
                    continue
                chain_result = poller.from_saved_json(raw_chain, sig, polled_at=sim_end)
                cutoff = sig.last_polled_at or sig.created_at
                flow_result = build_flow_watch_result(
                    sig,
                    alerts,
                    cutoff=cutoff,
                    checked_at=sim_end,
                )
                prev = await store.get_latest_snapshot(sig.id)
                try:
                    agg = await ledger.aggregate(sig.id)
                except Exception:
                    agg = None
                cres = conviction_engine.evaluate(
                    sig,
                    chain_result,
                    flow_result,
                    prev,
                    ledger_aggregate=agg,
                    news=None,
                    as_of=sim_end,
                )
                new_conv = max(0.0, min(100.0, sig.conviction_score + cres.conviction_delta))
                next_state = cres.next_state if cres.next_state is not None else sig.state

                snap = SignalSnapshot(
                    id=str(uuid.uuid4()),
                    signal_id=sig.id,
                    snapshot_at=sim_end,
                    contract_oi=chain_result.contract_oi,
                    contract_volume=chain_result.contract_volume,
                    contract_bid=chain_result.contract_bid,
                    contract_ask=chain_result.contract_ask,
                    contract_spread_pct=None,
                    spot_price=chain_result.spot_price,
                    neighbor_oi_total=sum(n.oi for n in chain_result.neighbor_strikes),
                    neighbor_strikes_active=sum(1 for n in chain_result.neighbor_strikes if n.oi > 0),
                    neighbor_put_call_ratio=None,
                    new_flow_count=len(flow_result.events),
                    new_flow_premium=sum(e.premium for e in flow_result.events),
                    new_flow_same_contract=sum(1 for e in flow_result.events if e.is_same_contract),
                    new_flow_same_expiry=sum(1 for e in flow_result.events if e.is_same_expiry),
                    conviction_delta=cres.conviction_delta,
                    conviction_after=new_conv,
                    signals_fired=cres.signals_fired,
                    notes=None,
                )
                await store.add_snapshot(snap)

                updates: dict[str, Any] = {
                    "conviction_score": new_conv,
                    "snapshots_taken": sig.snapshots_taken + 1,
                    "last_polled_at": sim_end,
                    "oi_high_water": cres.oi_high_water,
                    "chain_spread_count": cres.chain_spread_count,
                    "days_without_flow": cres.days_without_flow,
                }
                if flow_result.events:
                    updates["last_flow_at"] = max(e.created_at for e in flow_result.events)
                    updates["confirming_flows"] = sig.confirming_flows + len(flow_result.events)
                    updates["cumulative_premium"] = sig.cumulative_premium + sum(
                        e.premium for e in flow_result.events
                    )

                if next_state != sig.state:
                    updates["state"] = next_state
                    if next_state == SignalState.ACTIONABLE:
                        updates["matured_at"] = sim_end
                    if next_state in (SignalState.DECAYED, SignalState.EXPIRED):
                        updates["terminal_at"] = sim_end
                        updates["terminal_reason"] = cres.terminal_reason or next_state.value

                await store.update_signal(sig.id, **updates)

                traj_rows.append(
                    {
                        "signal_id": sig.id,
                        "ticker": sig.ticker,
                        "date": ds,
                        "day_number": day_num,
                        "conviction_score": round(new_conv, 4),
                        "conviction_delta": round(cres.conviction_delta, 4),
                        "state": (next_state.value if hasattr(next_state, "value") else str(next_state)),
                        "contract_oi": chain_result.contract_oi or "",
                        "new_flow_count": len(flow_result.events),
                        "signals_fired": json.dumps(cres.signals_fired),
                    }
                )

            replay_log.append({"date": ds, "candidates_graded": scored_count, "active_signals": len(active)})

    # Summaries
    import aiosqlite

    signals_summary: list[dict[str, Any]] = []
    async with aiosqlite.connect(str(replay_db)) as adb:
        cur = await adb.execute("SELECT * FROM signals ORDER BY created_at")
        rows = await cur.fetchall()
        cols = [d[0] for d in cur.description]
        for row in rows:
            rowd = dict(zip(cols, row))
            sid = rowd["id"]
            snaps = await store.get_snapshots(sid, limit=200)
            traj = [float(s.conviction_after or s.conviction_delta or 0) for s in reversed(snaps)]
            if traj and traj[-1] == 0 and rowd.get("conviction_score"):
                traj = [float(rowd["initial_score"])] + traj[1:]
            if not traj and rowd.get("conviction_score") is not None:
                traj = [float(rowd["conviction_score"])]
            signals_summary.append(
                {
                    "signal_id": sid,
                    "ticker": rowd["ticker"],
                    "strike": rowd["strike"],
                    "expiry": rowd["expiry"],
                    "direction": rowd["direction"],
                    "initial_score": rowd["initial_score"],
                    "created_date": (rowd.get("created_at") or "")[:10],
                    "final_state": str(rowd.get("state", "")),
                    "final_conviction": float(rowd.get("conviction_score") or 0),
                    "days_tracked": len(snaps),
                    "confirming_flows": rowd.get("confirming_flows", 0),
                    "oi_high_water": rowd.get("oi_high_water", 0),
                    "conviction_trajectory": traj or [float(rowd.get("initial_score", 0))],
                    "matured_date": (rowd.get("matured_at") or "")[:10] or None,
                    "terminal_date": (rowd.get("terminal_at") or "")[:10] or None,
                    "terminal_reason": rowd.get("terminal_reason"),
                }
            )

    (out_dir / "signals_summary.json").write_text(json.dumps(signals_summary, indent=2))
    (out_dir / "replay_log.json").write_text(json.dumps(replay_log, indent=2))

    if traj_rows:
        fields = list(traj_rows[0].keys())
        with (out_dir / "conviction_trajectories.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(traj_rows)

    return {
        "ok": True,
        "output_dir": str(out_dir.resolve()),
        "replay_db": str(replay_db.resolve()),
        "signals_created": len(signals_summary),
        "day_count": len(day_dirs),
    }
