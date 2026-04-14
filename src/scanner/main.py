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
import sys
from datetime import datetime
from pathlib import Path

import httpx
import structlog
from dotenv import load_dotenv

from scanner.client.uw_client import UWClient
from scanner.models.market_tide import MarketTide
from scanner.rules.engine import RuleEngine
from scanner.rules.confluence import ConfluenceEnricher
from scanner.state.dedup import DedupCache
from scanner.state.db import ScannerDB
from scanner.output.queue import CandidateQueue
from scanner.utils.clock import MarketClock
from scanner.utils.logging import setup_logging
from shared.config import load_config
from shared.uw_runtime import get_uw_limiter
from shared.uw_validation import UWTokenError, bootstrap_uw_runtime_from_config, require_uw_api_token
from tracker.config import load_tracker_config
from tracker.flow_ledger import FlowLedger, ledger_entry_from_flow_alert
from tracker.signal_store import SignalStore

load_dotenv()
logger = structlog.get_logger()


def _is_429(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429
    return "429" in str(exc)


def _cycle_has_429(*results: object) -> bool:
    for r in results:
        if isinstance(r, BaseException) and _is_429(r):
            return True
    return False


async def run_scanner(
    force: bool = False,
    max_cycles: int | None = None,
    candidate_queue: asyncio.Queue | None = None,
    *,
    uw_already_bootstrapped: bool = False,
    shutdown_event: asyncio.Event | None = None,
):
    config_path = Path(__file__).resolve().parent.parent.parent / "config" / "rules.yaml"
    if not config_path.exists():
        config_path = Path("config/rules.yaml")
    config = load_config(config_path)

    if not uw_already_bootstrapped:
        await bootstrap_uw_runtime_from_config(config)
    else:
        require_uw_api_token()

    token = require_uw_api_token()
    rate_limiter = get_uw_limiter()
    client = UWClient(api_token=token, rate_limiter=rate_limiter)
    engine = RuleEngine(config)
    enricher = ConfluenceEnricher(config)
    dedup = DedupCache(
        ttl_minutes=config["dedup"]["ttl_minutes"],
        key_fields=config["dedup"]["key_fields"],
    )
    db_path = config["output"]["sqlite_db_path"]
    if not Path(db_path).is_absolute():
        project_root = config_path.resolve().parent.parent
        db_path = str(project_root / db_path)
    db = ScannerDB(db_path)
    heartbeat_path = Path(db_path).resolve().parent / "heartbeat.txt"
    heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    queue = (
        candidate_queue
        if candidate_queue is not None
        else CandidateQueue(max_size=config["output"]["queue_max_size"])
    )
    clock = MarketClock(config["polling"])
    uw_cfg = config.get("unusual_whales") or {}
    base_interval = float(config["polling"]["flow_alerts_interval_seconds"])
    backoff_max = float(uw_cfg.get("poll_backoff_max_multiplier", 4.0))
    poll_backoff = 1.0

    await db.connect()

    cycle_count = 0
    logger.info("scanner_started", config_path=str(config_path))

    try:
        while max_cycles is None or cycle_count < max_cycles:
            if shutdown_event is not None and shutdown_event.is_set():
                logger.info("scanner.shutdown_graceful")
                break

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

                raw_a, raw_d, raw_t = await asyncio.gather(
                    flow_task, dp_task, tide_task, return_exceptions=True
                )

                if _cycle_has_429(raw_a, raw_d, raw_t):
                    poll_backoff = min(poll_backoff * 1.5, backoff_max)
                    logger.info("scanner.poll_backoff_increased", multiplier=round(poll_backoff, 2))

                alerts = raw_a
                dark_pool = raw_d
                tide = raw_t
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

                if not _cycle_has_429(raw_a, raw_d, raw_t) and errors == 0:
                    poll_backoff = max(poll_backoff / 1.25, 1.0)

                tracker_cfg = load_tracker_config(config)
                ledger_on = tracker_cfg.enabled and tracker_cfg.ledger.enabled
                ticker_signal_map: dict[str, str] = {}
                signal_by_ticker: dict[str, object] = {}
                if ledger_on:
                    try:
                        sig_store = SignalStore()
                        ticker_signal_map = await sig_store.get_ticker_signal_map()
                        for t, sid in ticker_signal_map.items():
                            sig = await sig_store.get_signal(sid)
                            if sig is not None:
                                signal_by_ticker[t] = sig
                    except Exception as exc:
                        logger.warning("scanner.watched_map_failed", error=str(exc))
                        ticker_signal_map = {}
                        signal_by_ticker = {}

                watched_set = set(ticker_signal_map.keys())
                watched_by_id: dict[str, object] = {}
                for alert in alerts:
                    if alert.ticker.upper() in watched_set:
                        watched_by_id[alert.id] = alert

                if ledger_on and watched_set:
                    for t in list(watched_set):
                        try:
                            extra = await client.get_flow_alerts(
                                is_otm=False,
                                min_premium=1,
                                size_greater_oi=False,
                                limit=100,
                                ticker=t,
                            )
                            for a in extra:
                                watched_by_id[a.id] = a
                        except Exception as exc:
                            logger.warning(
                                "scanner.watched_supplement_fetch_failed",
                                ticker=t,
                                error=str(exc),
                            )

                watched_alerts = list(watched_by_id.values())
                discovery_alerts = [a for a in alerts if a.ticker.upper() not in watched_set]

                if ledger_on and watched_alerts:
                    try:
                        ledger = FlowLedger()
                        entries = []
                        for alert in watched_alerts:
                            t = alert.ticker.upper()
                            sid = ticker_signal_map.get(t)
                            sig = signal_by_ticker.get(t)
                            if not sid or sig is None:
                                continue
                            entries.append(
                                ledger_entry_from_flow_alert(
                                    alert,
                                    signal_id=sid,
                                    signal=sig,
                                    source="scanner",
                                )
                            )
                        if entries:
                            await ledger.record_batch(entries)
                            logger.info(
                                "scanner.flow_ledger_batch",
                                rows=len(entries),
                                tickers=len(watched_set),
                            )
                    except Exception as exc:
                        logger.warning("scanner.flow_ledger_failed", error=str(exc))

                new_alerts = []
                for alert in discovery_alerts:
                    key_data = {
                        "ticker": alert.ticker,
                        "strike": alert.strike,
                        "expiry": alert.expiry,
                        "direction": alert.direction,
                    }
                    if not dedup.is_duplicate(key_data):
                        new_alerts.append(alert)

                candidates = engine.evaluate_batch(new_alerts)

                # Always enrich: use neutral tide when API failed so dark pool still runs
                tide_fallback = tide if tide is not None else MarketTide(direction="neutral")
                candidates = [
                    enricher.enrich(c, dark_pool, tide_fallback) for c in candidates
                ]

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

                dp_confirmed = sum(1 for c in candidates if c.dark_pool_confirmation)
                tide_aligned = sum(1 for c in candidates if c.market_tide_aligned)
                logger.info(
                    "cycle_complete",
                    cycle=cycle_count,
                    alerts=len(alerts),
                    new=len(new_alerts),
                    candidates=len(candidates),
                    dark_pool_confirmed=dp_confirmed,
                    market_tide_aligned=tide_aligned,
                    dedup_cache_size=dedup.size,
                    duration_ms=int((cycle_end - cycle_start).total_seconds() * 1000),
                    poll_backoff_multiplier=round(poll_backoff, 2),
                )

            except Exception as e:
                logger.exception("cycle_failed", cycle=cycle_count, error=str(e))

            heartbeat_path.write_text(datetime.utcnow().isoformat())

            if max_cycles is not None and cycle_count >= max_cycles:
                break
            await asyncio.sleep(base_interval * poll_backoff)

    finally:
        await client.close()
        await db.close()
        if candidate_queue is not None and not isinstance(candidate_queue, CandidateQueue):
            await queue.put(None)  # Sentinel so grader exits (asyncio.Queue only)
        logger.info("scanner_stopped")


def main():
    project_root = Path(__file__).resolve().parent.parent.parent
    cfg_path = project_root / "config" / "rules.yaml"
    raw = {}
    if cfg_path.exists():
        import yaml

        raw = yaml.safe_load(cfg_path.read_text()) or {}
    log_cfg = raw.get("logging") or {}
    setup_logging(
        json_logs=True,
        log_file_path=project_root / "scanner.json.log",
        max_bytes=int(log_cfg.get("max_file_size_mb", 50)) * 1_000_000,
        backup_count=int(log_cfg.get("backup_count", 5)),
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="Ignore market hours")
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=None,
        metavar="N",
        help="Run at most N polling cycles, then exit (default: run indefinitely)",
    )
    args = parser.parse_args()
    try:
        asyncio.run(run_scanner(force=args.force, max_cycles=args.max_cycles))
    except UWTokenError as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(1) from e


if __name__ == "__main__":
    main()
