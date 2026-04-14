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
from pathlib import Path

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
from grader.llm_client import LLMClient
from tracker.guardrails import check_guardrails, compute_position_size
from tracker.operations_config import OperationsConfig, load_operations_config
from tracker.portfolio_config import PortfolioConfig

log = structlog.get_logger()


async def run_monitor(
    client: httpx.AsyncClient,
    api_token: str,
    executor_queue: asyncio.Queue[Signal],
    config: TrackerConfig | None = None,
    enrichment_config: EnrichmentConfig | None = None,
    polling_config: dict | None = None,
    scanner_db_path: str | None = None,
    llm_client: LLMClient | None = None,
    finnhub_api_key: str = "",
    *,
    max_cycles: int | None = None,
    operations: OperationsConfig | None = None,
    shutdown_event: asyncio.Event | None = None,
    portfolio_config: PortfolioConfig | None = None,
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
        enrichment_config: Optional enrichment config (enables ledger/news/regrader).
        llm_client: Pre-constructed Claude client for re-grader (optional).
        finnhub_api_key: Finnhub key for sentiment/insider context in re-grade.
    """
    cfg = config or TrackerConfig()

    if not cfg.enabled:
        log.info("monitor.disabled")
        return

    store = SignalStore()
    poller = ChainPoller(client, api_token, config=cfg)
    ecfg = enrichment_config

    ledger: FlowLedger | None = None
    news_watcher: NewsWatcher | None = None
    regrader: Regrader | None = None

    if ecfg is not None:
        if ecfg.ledger.enabled:
            ledger = FlowLedger()
        if ecfg.news.enabled:
            news_watcher = NewsWatcher(client, api_token, config=ecfg.news)
        if ecfg.regrader.enabled and llm_client is not None:
            regrader = Regrader(
                llm_client,
                client,
                api_token,
                finnhub_api_key or "",
                store,
                config=ecfg.regrader,
                news_watcher=news_watcher,
            )

    watcher = FlowWatcher(
        client,
        api_token,
        scanner_db_path=scanner_db_path,
        flow_ledger=ledger,
    )
    engine = ConvictionEngine(config=cfg)

    # Build market clock for hours detection
    clock = None
    if polling_config:
        try:
            clock = MarketClock(polling_config)
        except Exception:
            pass

    cycle_count = 0
    last_cleanup_at: datetime | None = None
    CLEANUP_INTERVAL_HOURS = 24
    consecutive_api_failures = 0
    ops = operations or load_operations_config({})
    heartbeat_path = Path("data/monitor_heartbeat.txt")
    heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    guardrail_blocked_signals: set[str] = set()
    executor_notified: set[str] = set()

    log.info(
        "monitor.started",
        max_active=cfg.max_active_signals,
        enrichment={
            "ledger": ledger is not None,
            "news_watcher": news_watcher is not None,
            "regrader": regrader is not None,
        },
    )

    while max_cycles is None or cycle_count < max_cycles:
        if shutdown_event is not None and shutdown_event.is_set():
            log.info("monitor.shutdown_graceful")
            break

        cycle_count += 1

        if clock and clock.is_market_hours():
            interval = cfg.poll_interval_market_seconds
        else:
            interval = cfg.poll_interval_off_hours_seconds

        try:
            active_signals = await store.get_active_signals()
        except Exception as exc:
            log.error("monitor.fetch_signals_failed", error=str(exc))
            heartbeat_path.write_text(
                f"{datetime.now(timezone.utc).isoformat()}\n"
                f"cycle={cycle_count}\n"
                f"active_signals=0\n"
                f"consecutive_failures={consecutive_api_failures}\n"
            )
            await asyncio.sleep(interval)
            continue

        if not active_signals:
            heartbeat_path.write_text(
                f"{datetime.now(timezone.utc).isoformat()}\n"
                f"cycle={cycle_count}\n"
                f"active_signals=0\n"
                f"consecutive_failures={consecutive_api_failures}\n"
            )
            await asyncio.sleep(interval)
            continue

        log.info(
            "monitor.cycle_start",
            cycle=cycle_count,
            active_signals=len(active_signals),
        )

        cycle_api_errors = 0
        cycle_signal_count = 0
        for signal in active_signals:
            cycle_signal_count += 1
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
                    ledger,
                    portfolio_config=portfolio_config,
                    guardrail_blocked_signals=guardrail_blocked_signals,
                    executor_notified=executor_notified,
                )
            except Exception as exc:
                cycle_api_errors += 1
                log.error(
                    "monitor.signal_error",
                    signal_id=signal.id,
                    ticker=signal.ticker,
                    error=str(exc),
                )

        log.info("monitor.cycle_complete", cycle=cycle_count)

        cb = ops.circuit_breaker
        did_backoff = False
        if cycle_signal_count > 0 and cycle_api_errors == cycle_signal_count:
            consecutive_api_failures += 1
            if consecutive_api_failures >= cb.max_consecutive_failures:
                backoff_sleep = interval * cb.backoff_multiplier
                log.error(
                    "monitor.circuit_breaker",
                    consecutive_failures=consecutive_api_failures,
                    backoff_seconds=backoff_sleep,
                )
                await asyncio.sleep(backoff_sleep)
                did_backoff = True
            else:
                log.warning(
                    "monitor.all_signals_failed",
                    consecutive=consecutive_api_failures,
                    max_before_backoff=cb.max_consecutive_failures,
                )
        else:
            if consecutive_api_failures > 0:
                log.info("monitor.circuit_breaker_reset", was=consecutive_api_failures)
            consecutive_api_failures = 0

        now = datetime.now(timezone.utc)
        if (
            last_cleanup_at is None
            or (now - last_cleanup_at).total_seconds() > CLEANUP_INTERVAL_HOURS * 3600
        ):
            try:
                from tracker.cleanup import CleanupConfig, run_cleanup

                oc = ops.cleanup
                cleanup_run = CleanupConfig(
                    ledger_retention_days=cfg.ledger.retention_days,
                    snapshot_retention_days=oc.snapshot_retention_days,
                    news_retention_days=oc.news_retention_days,
                    regrade_retention_days=oc.regrade_retention_days,
                    terminal_signal_retention_days=oc.terminal_signal_retention_days,
                    purge_terminal_signals=cfg.ledger.purge_terminal_signals,
                    size_warning_mb=oc.size_warning_mb,
                )
                await run_cleanup(cleanup_run)
                last_cleanup_at = now
            except Exception as exc:
                log.warning("monitor.cleanup_failed", error=str(exc))

        heartbeat_path.write_text(
            f"{datetime.now(timezone.utc).isoformat()}\n"
            f"cycle={cycle_count}\n"
            f"active_signals={len(active_signals)}\n"
            f"consecutive_failures={consecutive_api_failures}\n"
        )

        if max_cycles is not None and cycle_count >= max_cycles:
            break

        if not did_backoff:
            await asyncio.sleep(interval)

    log.info("monitor.stopped")


async def _process_signal(
    signal: Signal,
    store: SignalStore,
    poller: ChainPoller,
    watcher: FlowWatcher,
    news_watcher: NewsWatcher | None,
    regrader: Regrader | None,
    engine: ConvictionEngine,
    executor_queue: asyncio.Queue[Signal],
    cfg: TrackerConfig,
    ledger: FlowLedger | None,
    *,
    portfolio_config: PortfolioConfig | None = None,
    guardrail_blocked_signals: set[str] | None = None,
    executor_notified: set[str] | None = None,
) -> None:
    """Run one poll cycle for a single signal."""
    now = datetime.now(timezone.utc)

    # 1. Get previous snapshot for comparison
    prev_snapshot = await store.get_latest_snapshot(signal.id)

    # 2. Poll the chain
    chain = await poller.poll(signal, prev_snapshot)

    # 3. Watch for new flow
    flow = await watcher.check(signal)

    # 3a–3b: persist watcher events to ledger, then aggregate for conviction
    ledger_agg = None
    if ledger is not None:
        if flow.events:
            try:
                for event in flow.events:
                    await ledger.record(
                        ledger_entry_from_flow_event(
                            event,
                            signal_id=signal.id,
                            signal=signal,
                            source="flow_watcher",
                            recorded_at=now,
                        )
                    )
            except Exception as exc:
                log.warning(
                    "monitor.ledger_write_failed",
                    signal_id=signal.id,
                    ticker=signal.ticker,
                    error=str(exc),
                )
        try:
            ledger_agg = await ledger.aggregate(signal.id)
        except Exception as exc:
            log.warning(
                "monitor.ledger_aggregate_failed",
                signal_id=signal.id,
                ticker=signal.ticker,
                error=str(exc),
            )

    # 3c: check news (cadence-gated internally)
    news = None
    if news_watcher is not None:
        try:
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
        except Exception as exc:
            log.warning(
                "monitor.news_check_failed",
                signal_id=signal.id,
                ticker=signal.ticker,
                error=str(exc),
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
        # Re-evaluate state transition with blended score (pure).
        new_state = engine.next_state(signal, new_conviction, flow, chain, now)

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

    if new_state in TERMINAL_STATES:
        if guardrail_blocked_signals is not None:
            guardrail_blocked_signals.discard(signal.id)
        if executor_notified is not None:
            executor_notified.discard(signal.id)

    if ledger is not None and new_state in (
        SignalState.EXPIRED,
        SignalState.DECAYED,
        SignalState.EXECUTED,
    ):
        # Ledger retention policy is controlled by enrichment config; monitor only purges on terminal.
        try:
            deleted = await ledger.purge_terminal(signal.id)
            if deleted:
                log.info("monitor.ledger_purged_signal", signal_id=signal.id, rows=deleted)
        except Exception as exc:
            log.warning("monitor.ledger_purge_failed", signal_id=signal.id, error=str(exc))

    # 8. Portfolio guardrails + push actionable signals to executor
    is_newly_actionable = (
        new_state == SignalState.ACTIONABLE and signal.state != SignalState.ACTIONABLE
    )
    was_blocked = (
        guardrail_blocked_signals is not None and signal.id in guardrail_blocked_signals
    )

    if (
        portfolio_config is not None
        and guardrail_blocked_signals is not None
        and executor_notified is not None
    ):
        if is_newly_actionable or (new_state == SignalState.ACTIONABLE and was_blocked):
            updated_signal = await store.get_signal(signal.id)
            if updated_signal:
                violation = await check_guardrails(
                    updated_signal, chain, portfolio_config, store
                )
                if violation:
                    guardrail_blocked_signals.add(signal.id)
                    log.warning(
                        "monitor.guardrail_blocked",
                        signal_id=signal.id,
                        ticker=signal.ticker,
                        rule=violation.rule,
                        limit=violation.limit,
                        actual=violation.actual,
                        message=violation.message,
                    )
                else:
                    guardrail_blocked_signals.discard(signal.id)
                    if executor_notified is not None and signal.id not in executor_notified:
                        position = compute_position_size(
                            updated_signal, chain, portfolio_config
                        )
                        log.info(
                            "monitor.signal_cleared_guardrails",
                            signal_id=signal.id,
                            ticker=signal.ticker,
                            position_size_usd=round(position.dollar_size, 2),
                            contracts=position.contracts,
                            max_loss_usd=round(position.max_loss_usd, 2),
                        )
                        await executor_queue.put(updated_signal)
                        executor_notified.add(signal.id)
    elif is_newly_actionable:
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
