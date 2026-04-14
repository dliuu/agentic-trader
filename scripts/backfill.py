"""Historical UW data collector for multi-day pipeline replay.

Usage:
    python scripts/backfill.py \\
      --start-date 2025-04-01 \\
      --end-date 2025-04-30 \\
      --output data/backfill/
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "src"))

UW_BASE = "https://api.unusualwhales.com"


def _daterange(d0: date, d1: date) -> list[date]:
    out: list[date] = []
    cur = d0
    while cur <= d1:
        out.append(cur)
        cur += timedelta(days=1)
    return out


def _is_weekend(d: date) -> bool:
    return d.weekday() >= 5


async def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill UW flow, chains, headlines, vol for replay.")
    parser.add_argument("--start-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--output", required=True, help="e.g. data/backfill/")
    parser.add_argument(
        "--skip-holidays",
        action="store_true",
        help="Skip days where flow-alerts returns empty (no full holiday calendar).",
    )
    args = parser.parse_args()

    token = (os.environ.get("UW_API_TOKEN") or os.environ.get("UNUSUAL_WHALES_API_TOKEN") or "").strip()
    if not token:
        print("Set UW_API_TOKEN (or UNUSUAL_WHALES_API_TOKEN) in the environment or .env")
        return 1

    start = date.fromisoformat(args.start_date)
    end = date.fromisoformat(args.end_date)
    out_root = Path(args.output)
    out_root.mkdir(parents=True, exist_ok=True)

    import httpx

    from shared.config import load_config
    from shared.uw_http import uw_get
    from shared.uw_validation import bootstrap_uw_runtime_from_config, uw_auth_headers

    config_path = project_root / "config" / "rules.yaml"
    config = load_config(config_path) if config_path.exists() else {}
    await bootstrap_uw_runtime_from_config(config)
    headers = uw_auth_headers(token)

    stats: dict[str, Any] = {
        "start_date": args.start_date,
        "end_date": args.end_date,
        "days_fetched": 0,
        "days_skipped_existing": 0,
        "days_skipped_weekend": 0,
        "days_skipped_empty_flow": 0,
        "days_flow_error": 0,
        "total_tickers_seen": set(),
        "api_calls": 0,
        "errors": [],
    }

    try:
        import tqdm

        day_list = [d for d in _daterange(start, end) if not _is_weekend(d)]
        bar = tqdm.tqdm(day_list, desc="Backfill")
    except ImportError:
        tqdm = None  # type: ignore[assignment]
        day_list = [d for d in _daterange(start, end) if not _is_weekend(d)]
        bar = day_list

    stock_info_mem: dict[str, dict[str, Any]] = {}

    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0)) as client:

        async def _get(path: str, **kwargs: Any) -> httpx.Response:
            stats["api_calls"] += 1
            url = path if path.startswith("http") else f"{UW_BASE}{path}"
            return await uw_get(client, url, headers=headers, **kwargs)

        for d in bar:
            ds = d.isoformat()
            day_dir = out_root / ds
            flow_path = day_dir / "flow_alerts.json"

            if flow_path.is_file():
                print(f"skipping, already fetched: {ds}")
                stats["days_skipped_existing"] += 1
                if tqdm is not None and hasattr(bar, "set_postfix"):
                    bar.set_postfix(calls=stats["api_calls"])  # type: ignore[attr-defined]
                continue

            day_dir.mkdir(parents=True, exist_ok=True)
            (day_dir / "chains").mkdir(exist_ok=True)
            (day_dir / "headlines").mkdir(exist_ok=True)
            (day_dir / "stock_info").mkdir(exist_ok=True)
            (day_dir / "vol_stats").mkdir(exist_ok=True)

            try:
                r = await _get(
                    "/api/option-trades/flow-alerts",
                    params={
                        "is_otm": "true",
                        "min_premium": 25000,
                        "size_greater_oi": "true",
                        "limit": 100,
                        "date": ds,
                    },
                )
                r.raise_for_status()
                raw = r.json()
            except Exception as e:
                stats["days_flow_error"] += 1
                stats["errors"].append({"date": ds, "phase": "flow_alerts", "error": str(e)})
                print(f"ERROR flow-alerts {ds}: {e}")
                continue

            data = raw.get("data", raw) if isinstance(raw, dict) else raw
            if not isinstance(data, list):
                data = []

            if args.skip_holidays and len(data) == 0:
                stats["days_skipped_empty_flow"] += 1
                print(f"skip empty flow day: {ds}")
                continue

            flow_path.write_text(json.dumps(data, indent=2))

            tickers = sorted(
                {
                    str(item.get("ticker") or item.get("ticker_symbol") or "").upper()
                    for item in data
                    if isinstance(item, dict)
                }
                - {""}
            )
            for t in tickers:
                stats["total_tickers_seen"].add(t)

            for t in tickers:
                chain_fp = day_dir / "chains" / f"{t}.json"
                if not chain_fp.is_file():
                    try:
                        cr = await _get(f"/api/stock/{t}/option-chains")
                        cr.raise_for_status()
                        chain_fp.write_text(json.dumps(cr.json(), indent=2))
                    except Exception as e:
                        stats["errors"].append({"date": ds, "ticker": t, "phase": "chain", "error": str(e)})
                        print(f"WARN chain {t} {ds}: {e}")

                hl_fp = day_dir / "headlines" / f"{t}.json"
                if not hl_fp.is_file():
                    try:
                        hr = await _get(
                            "/api/news/headlines",
                            params={"ticker": t, "limit": 50},
                        )
                        hr.raise_for_status()
                        hl_fp.write_text(json.dumps(hr.json(), indent=2))
                    except Exception as e:
                        stats["errors"].append({"date": ds, "ticker": t, "phase": "headlines", "error": str(e)})
                        print(f"WARN headlines {t} {ds}: {e}")

                info_fp = day_dir / "stock_info" / f"{t}.json"
                if t not in stock_info_mem:
                    try:
                        ir = await _get(f"/api/stock/{t}/info")
                        ir.raise_for_status()
                        stock_info_mem[t] = ir.json()
                    except Exception as e:
                        stats["errors"].append({"date": ds, "ticker": t, "phase": "stock_info", "error": str(e)})
                        print(f"WARN stock_info {t}: {e}")
                        stock_info_mem[t] = {}
                if not info_fp.is_file() and stock_info_mem.get(t):
                    info_fp.write_text(json.dumps(stock_info_mem[t], indent=2))

                vs_fp = day_dir / "vol_stats" / f"{t}.json"
                if not vs_fp.is_file():
                    try:
                        vr = await _get(f"/api/stock/{t}/volatility/stats")
                        vr.raise_for_status()
                        vs_fp.write_text(json.dumps(vr.json(), indent=2))
                    except Exception as e:
                        stats["errors"].append({"date": ds, "ticker": t, "phase": "vol_stats", "error": str(e)})
                        print(f"WARN vol_stats {t} {ds}: {e}")

            stats["days_fetched"] += 1
            if tqdm is not None and hasattr(bar, "set_postfix"):
                bar.set_postfix(calls=stats["api_calls"])  # type: ignore[attr-defined]

    manifest = {
        "start_date": args.start_date,
        "end_date": args.end_date,
        "days_fetched": stats["days_fetched"],
        "days_skipped_existing": stats["days_skipped_existing"],
        "days_skipped_weekend": sum(1 for x in _daterange(start, end) if _is_weekend(x)),
        "days_skipped_empty_flow": stats["days_skipped_empty_flow"],
        "days_flow_error": stats["days_flow_error"],
        "unique_tickers": len(stats["total_tickers_seen"]),
        "total_api_calls": stats["api_calls"],
        "errors": stats["errors"],
        "written_at": datetime.now(timezone.utc).isoformat(),
    }
    (out_root / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"Done. manifest -> {out_root / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
