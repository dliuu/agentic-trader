"""Run full pipeline: scanner + grader as concurrent tasks."""

import argparse
import asyncio
import signal as signal_mod
import sys
from pathlib import Path

import httpx
import structlog
from dotenv import load_dotenv

from grader.main import run_grader
from grader.llm_client import LLMClient
from grader.models import ScoredTrade
from scanner.main import run_scanner
from scanner.utils.logging import setup_logging
from shared.config import load_config
from shared.models import Candidate
from shared.uw_validation import UWTokenError, bootstrap_uw_runtime_from_config
from tracker.config import load_tracker_config
from tracker.enrichment_config import load_enrichment_config
from tracker.intake import run_signal_intake
from tracker.models import Signal
from tracker.monitor import run_monitor
from tracker.operations_config import load_operations_config
from tracker.portfolio_config import load_portfolio_config

load_dotenv()

_pipeline_log = structlog.get_logger(__name__)


async def main(force: bool = False, max_cycles: int | None = None):
    config_path = Path(__file__).resolve().parent.parent.parent / "config" / "rules.yaml"
    if not config_path.exists():
        config_path = Path("config/rules.yaml")
    config = load_config(config_path)
    await bootstrap_uw_runtime_from_config(config)

    shutdown_event = asyncio.Event()

    def handle_shutdown(signum, frame):
        _pipeline_log.info("pipeline.shutdown_requested", signal=signum)
        shutdown_event.set()

    signal_mod.signal(signal_mod.SIGTERM, handle_shutdown)
    signal_mod.signal(signal_mod.SIGINT, handle_shutdown)

    tracker_cfg = load_tracker_config(config)
    enrichment_cfg = load_enrichment_config(config)
    operations_cfg = load_operations_config(config)
    portfolio_cfg = load_portfolio_config(config)

    candidate_queue: asyncio.Queue[Candidate] = asyncio.Queue()
    scored_queue: asyncio.Queue[ScoredTrade | None] = asyncio.Queue()
    executor_queue: asyncio.Queue[Signal] = asyncio.Queue()

    # Resolve scanner DB path for flow watcher
    scanner_db_path = config["output"]["sqlite_db_path"]
    if not Path(scanner_db_path).is_absolute():
        project_root = config_path.resolve().parent.parent
        scanner_db_path = str(project_root / scanner_db_path)

    llm_client = None
    if (
        enrichment_cfg is not None
        and enrichment_cfg.regrader.enabled
        and config.get("anthropic_api_key")
    ):
        llm_client = LLMClient(
            api_key=config["anthropic_api_key"],
            model=enrichment_cfg.regrader.model,
            max_tokens=enrichment_cfg.regrader.max_tokens,
            timeout=enrichment_cfg.regrader.timeout_seconds,
        )

    async with httpx.AsyncClient(timeout=15.0) as http_client:
        try:
            tasks = [
                run_scanner(
                    force=force,
                    max_cycles=max_cycles,
                    candidate_queue=candidate_queue,
                    uw_already_bootstrapped=True,
                    shutdown_event=shutdown_event,
                ),
                run_grader(
                    candidate_queue,
                    scored_queue,
                    uw_already_bootstrapped=True,
                    shutdown_event=shutdown_event,
                ),
            ]

            if tracker_cfg.enabled:
                tasks.append(
                    run_signal_intake(scored_queue, config=tracker_cfg)
                )
                tasks.append(
                    run_monitor(
                        client=http_client,
                        api_token=config["uw_api_token"],
                        executor_queue=executor_queue,
                        config=tracker_cfg,
                        enrichment_config=enrichment_cfg,
                        polling_config=config.get("polling"),
                        scanner_db_path=scanner_db_path,
                        llm_client=llm_client,
                        finnhub_api_key=config.get("finnhub_api_key", ""),
                        max_cycles=max_cycles,
                        operations=operations_cfg,
                        shutdown_event=shutdown_event,
                        portfolio_config=portfolio_cfg,
                    )
                )

            await asyncio.gather(*tasks)
        finally:
            if llm_client is not None:
                await llm_client.close()


def cli():
    project_root = Path(__file__).resolve().parent.parent.parent
    config_path = project_root / "config" / "rules.yaml"
    config = load_config(config_path) if config_path.exists() else {}
    log_cfg = config.get("logging") or {}
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
        help="Run at most N polling cycles, then exit",
    )
    args = parser.parse_args()
    try:
        asyncio.run(main(force=args.force, max_cycles=args.max_cycles))
    except UWTokenError as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(1) from e


if __name__ == "__main__":
    cli()
