"""Run full pipeline: scanner + grader as concurrent tasks."""

import argparse
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

from grader.main import run_grader
from grader.models import ScoredTrade
from scanner.main import run_scanner
from scanner.utils.logging import setup_logging
from shared.config import load_config
from shared.models import Candidate
from shared.uw_validation import UWTokenError, bootstrap_uw_runtime_from_config

load_dotenv()


async def main(force: bool = False, max_cycles: int | None = None):
    config_path = Path(__file__).resolve().parent.parent.parent / "config" / "rules.yaml"
    if not config_path.exists():
        config_path = Path("config/rules.yaml")
    config = load_config(config_path)
    await bootstrap_uw_runtime_from_config(config)

    candidate_queue: asyncio.Queue[Candidate] = asyncio.Queue()
    scored_queue: asyncio.Queue[ScoredTrade] = asyncio.Queue()

    await asyncio.gather(
        run_scanner(
            force=force,
            max_cycles=max_cycles,
            candidate_queue=candidate_queue,
            uw_already_bootstrapped=True,
        ),
        run_grader(candidate_queue, scored_queue, uw_already_bootstrapped=True),
    )


def cli():
    project_root = Path(__file__).resolve().parent.parent.parent
    setup_logging(
        json_logs=True,
        log_file_path=project_root / "scanner.json.log",
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
