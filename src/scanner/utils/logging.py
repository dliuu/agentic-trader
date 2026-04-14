"""Structured logging setup with optional rotating file output."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import structlog


class TeeWriter:
    """Writes to multiple streams (e.g. stdout + file)."""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, data: str) -> None:
        for s in self.streams:
            s.write(data)
            s.flush()

    def flush(self) -> None:
        for s in self.streams:
            s.flush()


class RotatingLogWriter:
    """File-like wrapper around RotatingFileHandler for structlog.

    structlog's PrintLoggerFactory needs a file-like object with .write()
    and .flush(). This wraps a RotatingFileHandler to provide that
    interface while getting automatic rotation.
    """

    def __init__(self, path: Path, max_bytes: int = 50_000_000, backup_count: int = 5):
        self._handler = RotatingFileHandler(
            str(path),
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        self._handler.setFormatter(logging.Formatter("%(message)s"))
        self._logger = logging.getLogger(f"structlog_file_{path}")
        self._logger.handlers.clear()
        self._logger.addHandler(self._handler)
        self._logger.setLevel(logging.DEBUG)
        self._logger.propagate = False

    def write(self, data: str) -> None:
        data = data.rstrip("\n")
        if data:
            self._logger.info(data)

    def flush(self) -> None:
        self._handler.flush()


def setup_logging(
    json_logs: bool = False,
    log_file_path: Path | str | None = None,
    max_bytes: int = 50_000_000,
    backup_count: int = 5,
):
    """Configure structlog with optional rotating file output.

    Args:
        json_logs: Use JSON format instead of human-readable.
        log_file_path: If set, also write logs to this file with rotation.
        max_bytes: Max log file size before rotation (default 50MB).
        backup_count: Number of rotated files to keep (default 5).
    """
    out_stream: object = sys.stdout
    if log_file_path is not None:
        path = Path(log_file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        rotating_writer = RotatingLogWriter(path, max_bytes, backup_count)
        out_stream = TeeWriter(sys.stdout, rotating_writer)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.dev.ConsoleRenderer()
            if not json_logs
            else structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=out_stream),
        cache_logger_on_first_use=True,
    )
