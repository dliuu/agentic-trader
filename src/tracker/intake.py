"""Signal intake — creates Signal objects from ScoredTrade grader output.

Sits between the grader's scored_queue and the signal tracker's SQLite store.
Each ScoredTrade that arrives has already passed Gates 0-3 + synthesis with
score >= 78. The intake converts it to a Signal in PENDING state.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import structlog

from grader.models import ScoredTrade
from tracker.config import TrackerConfig
from tracker.models import Signal, SignalState
from tracker.signal_store import SignalStore

log = structlog.get_logger()


async def run_signal_intake(
    scored_queue: asyncio.Queue[ScoredTrade | None],
    config: TrackerConfig | None = None,
) -> None:
    """Consume ScoredTrades and create Signal objects.

    Runs as an asyncio task alongside the scanner and grader.
    Exits when it receives None (sentinel) from the queue.
    """
    cfg = config or TrackerConfig()
    store = SignalStore()

    while True:
        scored_trade = await scored_queue.get()

        if scored_trade is None:
            log.info("signal_intake.shutdown")
            scored_queue.task_done()
            break

        try:
            await _process_scored_trade(scored_trade, store, cfg)
        except Exception as exc:
            log.error(
                "signal_intake.error",
                ticker=scored_trade.candidate.ticker,
                error=str(exc),
            )
        finally:
            scored_queue.task_done()


async def _process_scored_trade(
    scored_trade: ScoredTrade,
    store: SignalStore,
    cfg: TrackerConfig,
) -> None:
    """Convert a ScoredTrade to a Signal and persist it."""
    candidate = scored_trade.candidate

    # Check capacity limit
    active_count = await store.count_active()
    if active_count >= cfg.max_active_signals:
        log.warning(
            "signal_intake.capacity_limit",
            ticker=candidate.ticker,
            active=active_count,
            max=cfg.max_active_signals,
        )
        return

    option_type = "call" if candidate.direction == "bullish" else "put"

    # One monitored signal per ticker — second strike enriches via flow ledger, not a new row
    if await store.has_active_signal_for_ticker(candidate.ticker):
        log.info(
            "signal_intake.ticker_already_tracked",
            ticker=candidate.ticker,
            strike=candidate.strike,
            expiry=candidate.expiry,
        )
        return

    # Check for duplicate (same contract already being tracked)
    is_dup = await store.check_duplicate_signal(
        candidate.ticker, candidate.strike, candidate.expiry
    )
    if is_dup:
        log.info(
            "signal_intake.duplicate_skipped",
            ticker=candidate.ticker,
            strike=candidate.strike,
            expiry=candidate.expiry,
        )
        return

    # Serialize risk params if present
    risk_json = None
    if scored_trade.risk is not None:
        risk_json = scored_trade.risk.model_dump_json()

    # Build anomaly fingerprint
    score = scored_trade.grade.score if scored_trade.grade else 0
    fingerprint = (
        f"${candidate.premium_usd:,.0f} on {candidate.ticker} "
        f"{candidate.expiry} {candidate.strike}{option_type[0].upper()}, "
        f"score {score}"
    )

    signal = Signal(
        id=str(uuid.uuid4()),
        ticker=candidate.ticker,
        strike=candidate.strike,
        expiry=candidate.expiry,
        option_type=option_type,
        direction=candidate.direction,
        state=SignalState.PENDING,
        initial_score=score,
        initial_premium=candidate.premium_usd,
        initial_oi=candidate.open_interest,
        initial_volume=candidate.volume,
        initial_contract_adv=0,
        grade_id=candidate.id,
        conviction_score=float(score),
        oi_high_water=candidate.open_interest,
        cumulative_premium=candidate.premium_usd,
        created_at=datetime.now(timezone.utc),
        risk_params_json=risk_json,
        anomaly_fingerprint=fingerprint,
    )

    await store.create_signal(signal)
    log.info(
        "signal_intake.created",
        signal_id=signal.id,
        ticker=signal.ticker,
        strike=signal.strike,
        expiry=signal.expiry,
        score=signal.initial_score,
        fingerprint=fingerprint,
    )
