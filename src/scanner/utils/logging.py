"""Structured logging setup.

Configures structlog for JSON output in production,
human-readable in development.
"""
from __future__ import annotations

import logging
import sys
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


def setup_logging(json_logs: bool = False, log_file_path: Path | str | None = None):
    """Configure structlog. Call from main before starting.

    Args:
        json_logs: Use JSON format instead of human-readable.
        log_file_path: If set, also append logs to this file (in addition to stdout).
    """
    out_stream: object = sys.stdout
    if log_file_path is not None:
        path = Path(log_file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        log_file = path.open("a", encoding="utf-8")
        out_stream = TeeWriter(sys.stdout, log_file)

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
