"""CLI for full-pipeline replay. Core logic: ``replay.runner.run_replay_pipeline``."""

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
    parser = argparse.ArgumentParser(description="Full pipeline replay with simulated clock.")
    parser.add_argument("--data-dir", required=True, type=str)
    parser.add_argument("--output", required=True, type=str)
    parser.add_argument("--config", default=str(project_root / "config" / "rules.yaml"))
    parser.add_argument(
        "--mock-llm",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Skip Gate 3 LLM (default: on).",
    )
    args = parser.parse_args()
    if not args.mock_llm:
        print("Live Gate 3 in replay is not wired; use --mock-llm (default).")
        return 1

    from shared.config import load_config
    from replay.runner import run_replay_pipeline

    config = load_config(Path(args.config))
    result = await run_replay_pipeline(
        data_dir=Path(args.data_dir),
        output_dir=Path(args.output),
        config=config,
        mock_llm=args.mock_llm,
    )
    if not result.get("ok", True):
        print(result.get("error", "replay failed"))
        return 1
    print(f"Replay complete -> {result.get('output_dir')} ({result.get('signals_created', 0)} signals)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
