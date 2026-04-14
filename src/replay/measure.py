"""Outcome measurement for replay DB (yfinance). Used by CLI and parameter sweeps."""

from __future__ import annotations

import json
import statistics
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


async def _load_signals(db_path: Path) -> list[dict[str, Any]]:
    import aiosqlite

    out: list[dict[str, Any]] = []
    async with aiosqlite.connect(str(db_path)) as db:
        cur = await db.execute("SELECT * FROM signals ORDER BY created_at")
        rows = await cur.fetchall()
        cols = [d[0] for d in cur.description]
        for row in rows:
            out.append(dict(zip(cols, row)))
    return out


async def _first_snapshot_spot(db_path: Path, signal_id: str) -> float | None:
    import aiosqlite

    async with aiosqlite.connect(str(db_path)) as db:
        cur = await db.execute(
            "SELECT spot_price FROM signal_snapshots WHERE signal_id = ? "
            "ORDER BY snapshot_at ASC LIMIT 1",
            (signal_id,),
        )
        row = await cur.fetchone()
        if row and row[0] is not None:
            return float(row[0])
    return None


def _classify(
    *,
    final_state: str,
    move_pct: float | None,
    correct_direction: bool | None,
    tp_threshold: float,
    fn_threshold: float,
) -> str:
    st = (final_state or "").lower()
    if move_pct is None or correct_direction is None:
        return "unmeasurable"
    if st == "actionable":
        if correct_direction and move_pct is not None and move_pct >= tp_threshold:
            return "TP"
        return "FP"
    if st in ("decayed", "expired"):
        if correct_direction and move_pct is not None and move_pct >= fn_threshold:
            return "FN"
        return "TN"
    return "unclassified"


async def measure_outcomes_to_json(
    replay_db: Path,
    output_json: Path,
    *,
    tp_threshold: float = 2.0,
    fn_threshold: float = 5.0,
) -> dict[str, Any]:
    """Compute outcomes from ``replay_db`` and write ``output_json``. Returns the report dict."""
    import yfinance as yf

    db_path = Path(replay_db)
    signals = await _load_signals(db_path)
    per_signal: list[dict[str, Any]] = []
    tp = fp = fn = tn = 0

    for row in signals:
        sid = str(row["id"])
        ticker = str(row["ticker"])
        direction = str(row["direction"])
        strike = float(row["strike"])
        opt_type = str(row["option_type"])
        created = _parse_dt(row.get("created_at"))
        matured = _parse_dt(row.get("matured_at"))
        terminal = _parse_dt(row.get("terminal_at"))
        final_state = str(row.get("state") or "")
        expiry_s = str(row.get("expiry") or "")[:10]

        entry = await _first_snapshot_spot(db_path, sid)

        exit_dt = matured or terminal
        if exit_dt is None and expiry_s:
            try:
                exit_dt = datetime.combine(
                    date.fromisoformat(expiry_s),
                    time(16, 0),
                    tzinfo=timezone.utc,
                )
            except ValueError:
                exit_dt = None
        if exit_dt is None and created:
            exit_dt = created + timedelta(days=30)

        move_pct: float | None = None
        correct_direction: bool | None = None
        max_fav: float | None = None
        option_itm: bool | None = None
        exit_price: float | None = None

        if entry is not None and created and exit_dt:
            start = (created - timedelta(days=1)).strftime("%Y-%m-%d")
            end = (exit_dt + timedelta(days=2)).strftime("%Y-%m-%d")
            try:
                hist = yf.Ticker(ticker).history(start=start, end=end)
            except Exception:
                hist = None
            if hist is None or hist.empty:
                move_pct = None
            else:
                try:
                    row0 = hist.iloc[0]
                    entry_adj = float(row0.get("Open", row0.get("open", entry)))
                    if entry_adj and entry_adj > 0:
                        entry = entry_adj
                except Exception:
                    pass
                try:
                    last_row = hist.iloc[-1]
                    exit_price = float(last_row.get("Close", last_row.get("close", 0)))
                except Exception:
                    exit_price = None
                if exit_price and entry:
                    if direction == "bullish":
                        move_pct = (exit_price - entry) / entry * 100
                        correct_direction = move_pct > 0
                        hi = float(hist["High"].max())
                        max_fav = (hi - entry) / entry * 100
                    else:
                        move_pct = (entry - exit_price) / entry * 100
                        correct_direction = move_pct > 0
                        lo = float(hist["Low"].min())
                        max_fav = (entry - lo) / entry * 100
                    if opt_type == "call":
                        option_itm = exit_price > strike
                    else:
                        option_itm = exit_price < strike

        cat = _classify(
            final_state=final_state,
            move_pct=move_pct,
            correct_direction=correct_direction,
            tp_threshold=tp_threshold,
            fn_threshold=fn_threshold,
        )
        if cat == "TP":
            tp += 1
        elif cat == "FP":
            fp += 1
        elif cat == "FN":
            fn += 1
        elif cat == "TN":
            tn += 1

        per_signal.append(
            {
                "signal_id": sid,
                "ticker": ticker,
                "direction": direction,
                "strike": strike,
                "expiry": expiry_s,
                "initial_score": row.get("initial_score"),
                "final_state": final_state,
                "final_conviction": float(row.get("conviction_score") or 0),
                "entry_price": entry,
                "exit_price": exit_price,
                "move_pct": move_pct,
                "correct_direction": correct_direction,
                "option_itm_at_exit": option_itm,
                "classification": cat,
                "days_tracked": row.get("snapshots_taken"),
                "confirming_flows": row.get("confirming_flows"),
                "max_favorable_move_pct": max_fav,
            }
        )

    measurable = [s for s in per_signal if s["classification"] not in ("unmeasurable", "unclassified")]
    tp_l = [s for s in measurable if s["classification"] == "TP"]
    fp_l = [s for s in measurable if s["classification"] == "FP"]

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = (
        2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
    )

    actionable_states = {"actionable"}
    decayed = sum(1 for row in signals if str(row.get("state", "")).lower() == "decayed")
    expired = sum(1 for row in signals if str(row.get("state", "")).lower() == "expired")
    actionable_n = sum(1 for row in signals if str(row.get("state", "")).lower() in actionable_states)

    date_min = min((str(r.get("created_at") or "")[:10] for r in signals), default="")
    date_max = max((str(r.get("created_at") or "")[:10] for r in signals), default="")

    out: dict[str, Any] = {
        "meta": {
            "replay_db": str(db_path),
            "date_range": f"{date_min} to {date_max}",
            "total_signals_created": len(signals),
            "signals_reached_actionable": actionable_n,
            "signals_decayed": decayed,
            "signals_expired": expired,
            "tp_threshold_pct": tp_threshold,
            "fn_threshold_pct": fn_threshold,
        },
        "metrics": {
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "avg_tp_move_pct": round(statistics.mean([s["move_pct"] for s in tp_l if s["move_pct"] is not None]), 4)
            if tp_l
            else 0.0,
            "avg_fp_move_pct": round(statistics.mean([s["move_pct"] for s in fp_l if s["move_pct"] is not None]), 4)
            if fp_l
            else 0.0,
            "avg_days_to_actionable": 0.0,
            "median_initial_score": float(statistics.median([r.get("initial_score") or 0 for r in signals]))
            if signals
            else 0.0,
        },
        "signals": per_signal,
    }

    out_path = Path(output_json)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2))
    return out
