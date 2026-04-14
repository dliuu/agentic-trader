"""Rotating log writer used by setup_logging."""

from __future__ import annotations

from pathlib import Path

from scanner.utils.logging import RotatingLogWriter


def test_rotating_log_writer_rotates(tmp_path: Path) -> None:
    path = tmp_path / "app.log"
    writer = RotatingLogWriter(path, max_bytes=800, backup_count=2)
    line = "x" * 200 + "\n"
    for _ in range(20):
        writer.write(line)
    writer.flush()
    assert path.exists()
    backups = list(tmp_path.glob("app.log*"))
    assert len(backups) >= 1
