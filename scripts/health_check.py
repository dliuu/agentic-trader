"""Health check — verifies heartbeat files are recent.

Usage:
    python scripts/health_check.py [--max-age 600]

Exit codes:
    0 = healthy
    1 = unhealthy (heartbeat stale or missing)
    2 = partially healthy (one component stale)
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


def check_heartbeat(path: Path, max_age_seconds: int) -> tuple[bool, str]:
    """Check if a heartbeat file is recent enough."""
    if not path.exists():
        return False, f"MISSING: {path}"

    age = time.time() - path.stat().st_mtime
    if age > max_age_seconds:
        return False, f"STALE: {path} (age={int(age)}s, max={max_age_seconds}s)"

    return True, f"OK: {path} (age={int(age)}s)"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--max-age",
        type=int,
        default=600,
        help="Max heartbeat age in seconds (default: 600 = 10 min)",
    )
    parser.add_argument(
        "--data-dir",
        type=str,
        default="data",
        help="Data directory (default: data)",
    )
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    heartbeats = [
        data_dir / "heartbeat.txt",
        data_dir / "monitor_heartbeat.txt",
    ]

    results: list[tuple[bool, str]] = []
    for hb in heartbeats:
        ok, msg = check_heartbeat(hb, args.max_age)
        results.append((ok, msg))
        print(msg)

    all_ok = all(ok for ok, _ in results)
    any_ok = any(ok for ok, _ in results)

    if all_ok:
        print("HEALTH: OK")
        return 0
    if any_ok:
        print("HEALTH: DEGRADED")
        return 2
    print("HEALTH: UNHEALTHY")
    return 1


if __name__ == "__main__":
    sys.exit(main())
