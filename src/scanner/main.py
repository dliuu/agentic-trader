"""Scanner entry point.

The main loop:
1. Wait for market hours
2. Poll UW API (flow alerts + dark pool + market tide concurrently)
3. Deduplicate
4. Run rule engine
5. Enrich with confluence signals
6. Persist candidates + push to queue
7. Sleep until next cycle
8. Repeat
"""
import argparse
import asyncio
import os
from datetime import datetime
from pathlib import Path

import structlog
import yaml
from dotenv import load_dotenv

from scanner.client.uw_client import UWClient
from scanner.client.rate_limiter import RateLimiter
from scanner.rules.engine import RuleEngine
from scanner.state.dedup import DedupCache
from scanner.state.db import ScannerDB
from scanner.output.queue import CandidateQueue
from scanner.utils.clock import MarketClock
from scanner.utils.logging import setup_logging

load_dotenv()
logger = structlog.get_logger()


async def run_scanner(force: bool = False):
    config_path = Path(__file__).resolve().parent.parent.parent / "config" / "rules.yaml"
    if not config_path.exists():
        config_path = Path("config/rules.yaml")
    config = yaml.safe_load(config_path.read_text())

    rate_limiter = RateLimiter(calls_per_minute=30)
    client = UWClient(
        api_token=os.environ["UW_API_TOKEN"],
        rate_limiter=rate_limiter,
    )
    engine = RuleEngine(config)
    dedup = DedupCache(
        ttl_minutes=config["dedup"]["ttl_minutes"],
        key_fields=config["dedup"]["key_fields"],
    )
    db_path = config["output"]["sqlite_db_path"]
    if not Path(db_path).is_absolute():
        project_root = config_path.resolve().parent.parent
        db_path = str(project_root / db_path)
    db = ScannerDB(db_path)
    queue = CandidateQueue(max_size=config["output"]["queue_max_size"])
    clock = MarketClock(config["polling"])

    await db.connect()

    cycle_count = 0
    logger.info("scanner_started", config_path=str(config_path))

    try:
        while True:
            if not force and not clock.is_market_hours():
                wait = clock.seconds_until_open()
                logger.info("market_closed", sleep_seconds=wait)
                await asyncio.sleep(min(wait, 300))
                continue

            cycle_count += 1
            cycle_start = datetime.utcnow()
            errors = 0

            try:
                flow_task = client.get_flow_alerts(
                    is_otm=True,
                    min_premium=config["filters"]["premium"]["min_premium_usd"],
                    size_greater_oi=config["filters"]["volume"]["size_greater_oi"],
                )
                dp_task = client.get_dark_pool_recent()
                tide_task = client.get_market_tide()

                alerts, dark_pool, tide = await asyncio.gather(
                    flow_task, dp_task, tide_task, return_exceptions=True
                )

                if isinstance(alerts, Exception):
                    logger.error("flow_alerts_failed", error=str(alerts))
                    alerts = []
                    errors += 1
                if isinstance(dark_pool, Exception):
                    logger.error("dark_pool_failed", error=str(dark_pool))
                    dark_pool = []
                    errors += 1
                if isinstance(tide, Exception):
                    logger.error("market_tide_failed", error=str(tide))
                    tide = None
                    errors += 1

                new_alerts = []
                for alert in alerts:
                    key_data = {
                        "ticker": alert.ticker,
                        "strike": alert.strike,
                        "expiry": alert.expiry,
                        "direction": alert.direction,
                    }
                    if not dedup.is_duplicate(key_data):
                        new_alerts.append(alert)

                candidates = engine.evaluate_batch(new_alerts)

                # MVP: Skip ConfluenceEnricher; add dark pool + tide enrichment later

                for candidate in candidates:
                    await db.save_candidate(candidate)
                    await queue.put(candidate)

                cycle_end = datetime.utcnow()
                await db.log_cycle(
                    cycle_start,
                    cycle_end,
                    alerts=len(alerts),
                    candidates=len(candidates),
                    errors=errors,
                )

                logger.info(
                    "cycle_complete",
                    cycle=cycle_count,
                    alerts=len(alerts),
                    new=len(new_alerts),
                    candidates=len(candidates),
                    dedup_cache_size=dedup.size,
                    duration_ms=int((cycle_end - cycle_start).total_seconds() * 1000),
                )

            except Exception as e:
                logger.exception("cycle_failed", cycle=cycle_count, error=str(e))

            await asyncio.sleep(config["polling"]["flow_alerts_interval_seconds"])

    finally:
        await client.close()
        await db.close()
        logger.info("scanner_stopped")


def main():
    setup_logging(json_logs=True)
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="Ignore market hours")
    args = parser.parse_args()
    asyncio.run(run_scanner(force=args.force))


if __name__ == "__main__":
    main()
