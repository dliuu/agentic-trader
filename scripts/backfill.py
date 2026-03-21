"""Pull historical flow for backtesting.

Usage:
    python scripts/backfill.py [--days 7] [--output data/backfill/]

Fetches flow alerts and dark pool data from the UW API
and saves to JSON files for replay and analysis.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
from datetime import datetime, timedelta

from dotenv import load_dotenv

load_dotenv()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=1, help="Days of data to fetch")
    parser.add_argument("--output", type=str, default="data/backfill", help="Output directory")
    args = parser.parse_args()

    token = os.environ.get("UW_API_TOKEN")
    if not token:
        print("Set UW_API_TOKEN in .env")
        return 1

    from scanner.client.uw_client import UWClient
    from scanner.client.rate_limiter import RateLimiter

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    limiter = RateLimiter(calls_per_minute=20)
    client = UWClient(api_token=token, rate_limiter=limiter)

    try:
        alerts = await client.get_flow_alerts(limit=100)
        dp = await client.get_dark_pool_recent()
        tide = await client.get_market_tide()

        ts = datetime.utcnow().strftime("%Y%m%d_%H%M")
        (out_dir / f"flow_alerts_{ts}.json").write_text(
            json.dumps({"data": [a.model_dump(mode="json") for a in alerts]}, indent=2)
        )
        (out_dir / f"dark_pool_{ts}.json").write_text(
            json.dumps({"data": [p.model_dump(mode="json") for p in dp]}, indent=2)
        )
        print(f"Saved to {out_dir}")
    finally:
        await client.close()
    return 0


if __name__ == "__main__":
    exit(asyncio.run(main()))
