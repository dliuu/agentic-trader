"""CLI for outcome measurement. Core: ``replay.measure.measure_outcomes_to_json``."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "src"))


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--replay-db", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--price-source", choices=("yfinance",), default="yfinance")
    parser.add_argument("--tp-threshold", type=float, default=2.0)
    parser.add_argument("--fn-threshold", type=float, default=5.0)
    args = parser.parse_args()

    try:
        from replay.measure import measure_outcomes_to_json
    except ImportError as e:
        print("Install yfinance: pip install -e '.[backtest]'")
        print(e)
        return 1

    db_path = Path(args.replay_db)
    if not db_path.is_file():
        print(f"Replay DB not found: {db_path}")
        return 1

    await measure_outcomes_to_json(
        db_path,
        Path(args.output),
        tp_threshold=args.tp_threshold,
        fn_threshold=args.fn_threshold,
    )
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
