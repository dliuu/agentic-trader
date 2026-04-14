"""scripts/health_check.py exit codes."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _script() -> Path:
    return Path(__file__).resolve().parent.parent / "scripts" / "health_check.py"


def test_health_check_all_missing(tmp_path: Path) -> None:
    r = subprocess.run(
        [sys.executable, str(_script()), "--data-dir", str(tmp_path), "--max-age", "600"],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 1
    assert "UNHEALTHY" in r.stdout


def test_health_check_degraded(tmp_path: Path) -> None:
    hb = tmp_path / "heartbeat.txt"
    hb.write_text("ok\n")
    hb.touch()
    r = subprocess.run(
        [sys.executable, str(_script()), "--data-dir", str(tmp_path), "--max-age", "600"],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 2
    assert "DEGRADED" in r.stdout


def test_health_check_ok(tmp_path: Path) -> None:
    for name in ("heartbeat.txt", "monitor_heartbeat.txt"):
        p = tmp_path / name
        p.write_text("ok\n")
        p.touch()
    r = subprocess.run(
        [sys.executable, str(_script()), "--data-dir", str(tmp_path), "--max-age", "600"],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0
    assert "HEALTH: OK" in r.stdout
