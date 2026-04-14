"""Signal monitor — poll active signals and update conviction.

Main async loop that runs alongside the scanner and grader.
For each active signal, every poll cycle:
  1. Check terminal conditions (expiry, DTE)
  2. Poll the option chain (chain_poller)
  3. Watch for new flow (flow_watcher)
  3.5 Poll headlines + SEC EDGAR on cadence (news_watcher), persist catalyst rows
  4. Evaluate conviction (conviction engine)
  4.5 Optional LLM re-grade (milestone-triggered) — blend into conviction before snapshot
  5. Persist snapshot + update signal state
  6. Push actionable signals to the executor queue
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import httpx
import structlog

from scanner.utils.clock import MarketClock
from tracker.chain_poller import ChainPoller
from tracker.config import TrackerConfig
from tracker.conviction import ConvictionEngine
from tracker.flow_ledger import FlowLedger, ledger_entry_from_flow_event
from tracker.flow_watcher import FlowWatcher
from tracker.enrichment_config import EnrichmentConfig
from tracker.models import RegradeResult, Signal, SignalSnapshot, SignalState, TERMINAL_STATES
from tracker.news_watcher import NewsWatcher
from tracker.regrader import Regrader
from tracker.signal_store import SignalStore

log = structlog.get_logger()


async def run_monitor(
    client: httpx.AsyncClient,
    api_token: str,
    executor_queue: asyncio.Queue[Signal],
    config: TrackerConfig | None = None,
    polling_config: dict | None = None,
    scanner_db_path: str | None = None,
    *,
    max_cycles: int | None = None,
    enrichment_cfg: EnrichmentConfig | None = None,
    anthropic_api_key: str = "",
    finnhub_api_key: str = "",
) -> None:
    """Main monitor loop.

    Args:
        client: Shared httpx.AsyncClient for UW API calls.
        api_token: UW API token.
        executor_queue: Queue to push actionable signals for Agent C.
        config: TrackerConfig (loaded from rules.yaml).
        polling_config: The 'polling' section from rules.yaml (for MarketClock).
        scanner_db_path: Path to the scanner's SQLite DB for flow watcher.
        max_cycles: If set, run at most N cycles then exit (for testing).
        enrichment_cfg: Optional `enrichment` block (re-grader settings).
        anthropic_api_key: Claude API key for re-grader (optional).
        finnhub_api_key: Finnhub key for sentiment/insider context in re-grade.
    """
    cfg = config or TrackerConfig()

    if not cfg.enabled:
        log.info("monitor.disabled")
        return

    store = SignalStore()
    poller = ChainPoller(client, api_token, config=cfg)
    flow_ledger = FlowLedger() if cfg.ledger.enabled else None
    watcher = FlowWatcher(
        client,
        api_token,
        scanner_db_path=scanner_db_path,
        flow_ledger=flow_ledger,
    )
    news_watcher = NewsWatcher(client, api_token, config=cfg.news)
    engine = ConvictionEngine(config=cfg)

    regrader: Regrader | None = None
    enc = enrichment_cfg or EnrichmentConfig()
    if enc.regrader.enabled and anthropic_api_key.strip():
        try:
            from grader.llm_client import LLMClient

            llm = LLMClient(
                api_key=anthropic_api_key.strip(),
                model=enc.regrader.model,
                max_tokens=enc.regrader.max_tokens,
                timeout=enc.regrader.timeout_seconds,
            )
            regrader = Regrader(
                llm,
                client,
                api_token,
                finnhub_api_key or "",
                store,
                config=enc.regrader,
                news_watcher=news_watcher,
            )
            log.info("monitor.regrader_enabled")
        except Exception as exc:
            log.warning("monitor.regrader_init_failed", error=str(exc))

    # Build market clock for hours detection
    clock = None
    if polling_config:
        try:
            clock = MarketClock(polling_config)
        except Exception:
            pass

    cycle_count = 0
    log.info("monitor.started", max_active=cfg.max_active_signals)

    while max_cycles is None or cycle_count < max_cycles:
        cycle_count += 1

        # Determine sleep interval based on market hours
        if clock and clock.is_market_hours():
            interval = cfg.poll_interval_market_seconds
        else:
            interval = cfg.poll_interval_off_hours_seconds

        # Fetch all active signals
        try:
            active_signals = await store.get_active_signals()
        except Exception as exc:
            log.error("monitor.fetch_signals_failed", error=str(exc))
            await asyncio.sleep(interval)
            continue

        if not active_signals:
            await asyncio.sleep(interval)
            continue

        log.info(
            "monitor.cycle_start",
            cycle=cycle_count,
            active_signals=len(active_signals),
        )

        for signal in active_signals:
            try:
                await _process_signal(
                    signal,
                    store,
                    poller,
                    watcher,
                    news_watcher,
                    regrader,
                    engine,
                    executor_queue,
                    cfg,
                    flow_ledger,
                )
            except Exception as exc:
                log.error(
                    "monitor.signal_error",
                    signal_id=signal.id,
                    ticker=signal.ticker,
                    error=str(exc),
                )

        log.info("monitor.cycle_complete", cycle=cycle_count)

        if (
            flow_ledger is not None
            and cfg.ledger.retention_days > 0
            and cycle_count > 0
            and cycle_count % 20 == 0
        ):
            try:
                n = await flow_ledger.purge_entries_older_than(cfg.ledger.retention_days)
                if n:
                    log.info("monitor.ledger_retention_purge", rows=n)
            except Exception as exc:
                log.warning("monitor.ledger_retention_failed", error=str(exc))

        if max_cycles is not None and cycle_count >= max_cycles:
            break

        await asyncio.sleep(interval)

    log.info("monitor.stopped")


async def _process_signal(
    signal: Signal,
    store: SignalStore,
    poller: ChainPoller,
    watcher: FlowWatcher,
    news_watcher: NewsWatcher,
    regrader: Regrader | None,
    engine: ConvictionEngine,
    executor_queue: asyncio.Queue[Signal],
    cfg: TrackerConfig,
    flow_ledger: FlowLedger | None,
) -> None:
    """Run one poll cycle for a single signal."""
    now = datetime.now(timezone.utc)

    # 1. Get previous snapshot for comparison
    prev_snapshot = await store.get_latest_snapshot(signal.id)

    # 2. Poll the chain
    chain = await poller.poll(signal, prev_snapshot)

    # 3. Watch for new flow
    flow = await watcher.check(signal)

    # 3.5–3.6: persist watcher events to ledger, then aggregate for conviction
    ledger_agg = None
    if flow_ledger is not None:
        for event in flow.events:
            await flow_ledger.record(
                ledger_entry_from_flow_event(
                    event,
                    signal_id=signal.id,
                    signal=signal,
                    source="flow_watcher",
                    recorded_at=now,
                )
            )
        ledger_agg = await flow_ledger.aggregate(signal.id)

    news = await news_watcher.check(signal)
    if news.events:
        await news_watcher.persist_events(news.events)
        log.info(
            "monitor.news_events",
            signal_id=signal.id,
            ticker=signal.ticker,
            count=len(news.events),
            catalyst=news.has_catalyst,
            filing=news.filing_detected,
        )

    if news.regrade_recommended:
        log.info(
            "monitor.regrade_trigger_news",
            signal_id=signal.id,
            ticker=signal.ticker,
            catalysts=news.catalyst_types,
        )

    # 4. Evaluate conviction
    result = engine.evaluate(
        signal, chain, flow, prev_snapshot, ledger_aggregate=ledger_agg, news=news
    )

    # 5. Compute new values (deterministic conviction this cycle)
    new_conviction = max(
        0.0,
        min(100.0, signal.conviction_score + result.conviction_delta),
    )
    new_confirming = signal.confirming_flows + len(flow.events)
    new_cumulative = signal.cumulative_premium + sum(e.premium for e in flow.events)
    new_state = result.next_state or signal.state

    regrade = RegradeResult(signal_id=signal.id, triggered=False)
    milestone_signal = signal.model_copy(
        update={
            "cumulative_premium": new_cumulative,
            "confirming_flows": new_confirming,
        }
    )
    if (
        regrader is not None
        and new_state not in TERMINAL_STATES
    ):
        try:
            regrade = await regrader.maybe_regrade(
                signal,
                chain,
                flow,
                news,
                ledger_agg,
                new_conviction,
                signal_for_milestones=milestone_signal,
            )
        except Exception as exc:
            log.warning(
                "monitor.regrade_failed",
                signal_id=signal.id,
                ticker=signal.ticker,
                error=str(exc),
            )

    if regrade.triggered and regrade.blended_conviction is not None:
        new_conviction = regrade.blended_conviction
        log.info(
            "monitor.conviction_blended",
            signal_id=signal.id,
            ticker=signal.ticker,
            deterministic=round(regrade.deterministic_conviction or 0, 1),
            llm_score=regrade.synthesis_score,
            blended=round(new_conviction, 1),
            trigger=regrade.trigger_reason,
        )

    # 6. Build and persist snapshot
    # Compute spread percentage
    spread_pct = None
    if chain.contract_bid is not None and chain.contract_ask is not None:
        mid = (chain.contract_bid + chain.contract_ask) / 2
        if mid > 0:
            spread_pct = (chain.contract_ask - chain.contract_bid) / mid * 100

    # Compute neighbor aggregates
    neighbor_oi_total = sum(n.oi for n in chain.neighbor_strikes) if chain.neighbor_strikes else None
    neighbor_active = sum(1 for n in chain.neighbor_strikes if n.oi > 0) if chain.neighbor_strikes else None
    neighbor_pcr = None
    if chain.neighbor_strikes:
        call_oi = sum(n.oi for n in chain.neighbor_strikes if n.option_type == "call")
        put_oi = sum(n.oi for n in chain.neighbor_strikes if n.option_type == "put")
        if (call_oi + put_oi) > 0:
            neighbor_pcr = call_oi / (call_oi + put_oi)

    snapshot = SignalSnapshot(
        id=str(uuid.uuid4()),
        signal_id=signal.id,
        snapshot_at=now,
        contract_oi=chain.contract_oi,
        contract_volume=chain.contract_volume,
        contract_bid=chain.contract_bid,
        contract_ask=chain.contract_ask,
        contract_spread_pct=spread_pct,
        spot_price=chain.spot_price,
        neighbor_oi_total=neighbor_oi_total,
        neighbor_strikes_active=neighbor_active,
        neighbor_put_call_ratio=neighbor_pcr,
        new_flow_count=len(flow.events),
        new_flow_premium=sum(e.premium for e in flow.events),
        new_flow_same_contract=sum(1 for e in flow.events if e.is_same_contract),
        new_flow_same_expiry=sum(1 for e in flow.events if e.is_same_expiry),
        conviction_delta=result.conviction_delta,
        conviction_after=new_conviction,
        signals_fired=result.signals_fired,
    )

    # Check snapshot cap
    if signal.snapshots_taken < cfg.max_snapshots_per_signal:
        await store.add_snapshot(snapshot)

    # 7. Update signal in DB
    update_fields: dict = {
        "conviction_score": new_conviction,
        "state": new_state,
        "snapshots_taken": signal.snapshots_taken + 1,
        "confirming_flows": new_confirming,
        "oi_high_water": result.oi_high_water,
        "chain_spread_count": result.chain_spread_count,
        "cumulative_premium": new_cumulative,
        "days_without_flow": result.days_without_flow,
        "last_polled_at": now,
    }

    if flow.events:
        update_fields["last_flow_at"] = now

    if regrade.triggered and regrade.regraded_at is not None:
        update_fields["regrade_count"] = signal.regrade_count + 1
        update_fields["last_regraded_at"] = regrade.regraded_at
        update_fields["milestones_fired"] = list(signal.milestones_fired) + [
            regrade.trigger_reason or ""
        ]

    if new_state == SignalState.ACTIONABLE and signal.state != SignalState.ACTIONABLE:
        update_fields["matured_at"] = now
        log.info(
            "monitor.signal_actionable",
            signal_id=signal.id,
            ticker=signal.ticker,
            conviction=new_conviction,
            confirming_flows=new_confirming,
            oi_ratio=(
                chain.contract_oi / signal.initial_oi
                if chain.contract_oi and signal.initial_oi > 0 else 0
            ),
        )

    if new_state in (SignalState.EXPIRED, SignalState.DECAYED):
        update_fields["terminal_at"] = now
        update_fields["terminal_reason"] = result.terminal_reason or new_state.value
        log.info(
            "monitor.signal_terminal",
            signal_id=signal.id,
            ticker=signal.ticker,
            state=new_state.value,
            reason=result.terminal_reason,
            conviction=new_conviction,
        )

    await store.update_signal(signal.id, **update_fields)

    if (
        flow_ledger is not None
        and cfg.ledger.purge_terminal_signals
        and new_state in (SignalState.EXPIRED, SignalState.DECAYED, SignalState.EXECUTED)
    ):
        try:
            deleted = await flow_ledger.purge_terminal(signal.id)
            if deleted:
                log.info("monitor.ledger_purged_signal", signal_id=signal.id, rows=deleted)
        except Exception as exc:
            log.warning("monitor.ledger_purge_failed", signal_id=signal.id, error=str(exc))

    # 8. Push actionable signals to executor
    if new_state == SignalState.ACTIONABLE and signal.state != SignalState.ACTIONABLE:
        # Re-fetch the signal with updated fields for Agent C
        updated_signal = await store.get_signal(signal.id)
        if updated_signal:
            await executor_queue.put(updated_signal)

    # Log state transitions
    if new_state != signal.state:
        log.info(
            "monitor.state_change",
            signal_id=signal.id,
            ticker=signal.ticker,
            from_state=signal.state.value,
            to_state=new_state.value,
            conviction=round(new_conviction, 1),
            delta=round(result.conviction_delta, 1),
            signals=result.signals_fired[:5],
        )
