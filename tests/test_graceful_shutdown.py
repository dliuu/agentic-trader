"""Graceful shutdown: scanner sends sentinel when shutdown_event is set."""

from __future__ import annotations

import asyncio

import pytest

from scanner.main import run_scanner


@pytest.mark.asyncio
async def test_shutdown_stops_scanner(monkeypatch):
    """Scanner exits cleanly when shutdown_event is set."""
    monkeypatch.setenv("UW_API_TOKEN", "test-token-for-shutdown")
    shutdown_event = asyncio.Event()
    queue: asyncio.Queue = asyncio.Queue()
    shutdown_event.set()

    await run_scanner(
        force=True,
        max_cycles=None,
        candidate_queue=queue,
        uw_already_bootstrapped=True,
        shutdown_event=shutdown_event,
    )

    assert await queue.get() is None
