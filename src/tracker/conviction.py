"""Conviction engine — scores accumulated evidence and drives state transitions.

Pure functions. No I/O, no side effects. All thresholds from TrackerConfig.

The engine answers two questions each poll cycle:
  1. What is the conviction delta? (How much did evidence change?)
  2. What is the next state? (Should the signal advance, decay, or expire?)
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from tracker.config import TrackerConfig
from tracker.models import (
    ChainPollResult,
    FlowWatchResult,
    LedgerAggregate,
    Signal,
    SignalSnapshot,
    SignalState,
)


class ConvictionResult:
    """Output of a single conviction evaluation cycle.

    Contains the delta, the signals that fired, and the recommended
    next state. The monitor loop uses this to update the signal.
    """

    __slots__ = (
        "conviction_delta",
        "signals_fired",
        "next_state",
        "terminal_reason",
        "oi_high_water",
        "chain_spread_count",
        "days_without_flow",
    )

    def __init__(self):
        self.conviction_delta: float = 0.0
        self.signals_fired: list[str] = []
        self.next_state: SignalState | None = None  # None = no change
        self.terminal_reason: str | None = None
        self.oi_high_water: int = 0
        self.chain_spread_count: int = 0
        self.days_without_flow: int = 0


class ConvictionEngine:
    """Stateless engine that evaluates evidence for a single signal."""

    def __init__(self, config: TrackerConfig | None = None):
        self._cfg = config or TrackerConfig()
        self._scoring = self._cfg.scoring

    def evaluate(
        self,
        signal: Signal,
        chain: ChainPollResult,
        flow: FlowWatchResult,
        prev_snapshot: SignalSnapshot | None,
        ledger_aggregate: LedgerAggregate | None = None,
    ) -> ConvictionResult:
        """Evaluate one poll cycle of evidence.

        Args:
            signal: Current signal state.
            chain: Fresh chain data from the poller.
            flow: New flow events from the watcher.
            prev_snapshot: The previous snapshot (None if first poll).
            ledger_aggregate: Optional flow_ledger stats (multi-day accumulation, etc.).

        Returns:
            ConvictionResult with delta, fired signals, and recommended next state.
        """
        result = ConvictionResult()
        result.oi_high_water = signal.oi_high_water
        result.days_without_flow = signal.days_without_flow
        now = datetime.now(timezone.utc)

        # --- Check terminal conditions first ---
        terminal = self._check_terminal(signal, chain, now)
        if terminal is not None:
            result.next_state = terminal[0]
            result.terminal_reason = terminal[1]
            result.signals_fired.append(f"terminal:{terminal[1]}")
            return result

        # --- Positive signals ---
        if chain.contract_found and chain.contract_oi is not None:
            self._score_oi_change(signal, chain, prev_snapshot, result)
        self._score_new_flow(signal, flow, result, ledger_aggregate)
        self._score_chain_spread(chain, prev_snapshot, result)
        self._score_put_call_shift(signal, chain, result)
        self._score_premium_accumulation(signal, flow, result, ledger_aggregate)

        # --- Negative signals ---
        if chain.contract_found:
            self._score_spread_widening(chain, result)
        self._score_silence(signal, flow, now, result)
        self._score_spot_movement(signal, chain, result)
        self._score_dte_pressure(signal, now, result)

        # --- Clamp delta ---
        # Prevent wild swings: cap at ±20 per cycle
        result.conviction_delta = max(-20.0, min(20.0, result.conviction_delta))

        # --- Determine next state ---
        new_conviction = signal.conviction_score + result.conviction_delta
        new_conviction = max(0.0, min(100.0, new_conviction))

        result.next_state = self._next_state(signal, new_conviction, flow, chain, now)

        return result

    # ─── Terminal condition checks ───

    def _check_terminal(
        self,
        signal: Signal,
        chain: ChainPollResult,
        now: datetime,
    ) -> tuple[SignalState, str] | None:
        """Check if the signal should terminate regardless of evidence."""

        # Option expired or DTE too low
        try:
            expiry_date = date.fromisoformat(signal.expiry)
            dte = (expiry_date - now.date()).days
        except (ValueError, TypeError):
            return SignalState.EXPIRED, "unparseable_expiry"

        if dte < 0:
            return SignalState.EXPIRED, "option_expired"
        if dte < self._cfg.min_dte_for_monitoring:
            return SignalState.EXPIRED, f"dte_below_minimum_{dte}d"

        # Contract disappeared from chain
        if not chain.contract_found:
            return SignalState.EXPIRED, "contract_not_in_chain"

        # Monitoring window elapsed
        days_active = (now - signal.created_at).total_seconds() / 86400
        if days_active > self._cfg.monitoring_window_days:
            if signal.conviction_score < self._cfg.decay_window_conviction:
                return SignalState.DECAYED, (
                    f"window_elapsed_{self._cfg.monitoring_window_days}d_"
                    f"conviction_{signal.conviction_score:.0f}"
                )

        return None

    # ─── Positive scoring ───

    def _score_oi_change(
        self,
        signal: Signal,
        chain: ChainPollResult,
        prev: SignalSnapshot | None,
        result: ConvictionResult,
    ) -> None:
        """Score change in OI on the signal's contract."""
        current_oi = chain.contract_oi or 0

        # Update high water mark
        result.oi_high_water = max(signal.oi_high_water, current_oi)

        # Compare to previous snapshot (not initial OI) for delta
        if prev is not None and prev.contract_oi is not None and prev.contract_oi > 0:
            pct_change = (current_oi - prev.contract_oi) / prev.contract_oi
        elif signal.initial_oi > 0:
            pct_change = (current_oi - signal.initial_oi) / signal.initial_oi
        else:
            return

        s = self._scoring

        if pct_change > 0:
            # Positive: OI is building
            ticks = int(pct_change / 0.10)  # one tick per 10%
            bonus = min(ticks * s.oi_increase_per_10pct, s.oi_increase_cap)
            if bonus > 0:
                result.conviction_delta += bonus
                result.signals_fired.append(f"oi_increase_{pct_change:.0%}")
        elif pct_change < -0.05:  # ignore noise below 5%
            ticks = int(abs(pct_change) / 0.10)
            penalty = ticks * s.oi_decrease_per_10pct  # already negative
            result.conviction_delta += penalty
            result.signals_fired.append(f"oi_decrease_{pct_change:.0%}")

    def _score_new_flow(
        self,
        signal: Signal,
        flow: FlowWatchResult,
        result: ConvictionResult,
        ledger_agg: LedgerAggregate | None = None,
    ) -> None:
        """Score new unusual flow events on the signal's ticker."""
        if flow.events:
            s = self._scoring
            bonus = min(
                len(flow.events) * s.confirming_flow_bonus,
                s.confirming_flow_cap,
            )
            result.conviction_delta += bonus

            same_contract = sum(1 for e in flow.events if e.is_same_contract)
            if same_contract > 0:
                result.signals_fired.append(f"flow_same_contract_{same_contract}")
            same_expiry = sum(1 for e in flow.events if e.is_same_expiry)
            if same_expiry > 0:
                result.signals_fired.append(f"flow_same_expiry_{same_expiry}")
            result.signals_fired.append(f"new_flow_{len(flow.events)}_events")

            # Reset silence counter
            result.days_without_flow = 0

        if ledger_agg is not None:
            if ledger_agg.distinct_days >= 2:
                result.conviction_delta += 3.0
                result.signals_fired.append(f"multi_day_accumulation_{ledger_agg.distinct_days}d")
            if ledger_agg.distinct_strikes >= 3:
                result.conviction_delta += 2.0
                result.signals_fired.append(f"strike_spread_{ledger_agg.distinct_strikes}_strikes")

    def _score_chain_spread(
        self,
        chain: ChainPollResult,
        prev: SignalSnapshot | None,
        result: ConvictionResult,
    ) -> None:
        """Score new active strikes appearing on the same expiry."""
        current_active = sum(1 for n in chain.neighbor_strikes if n.oi > 0)
        result.chain_spread_count = current_active

        if prev is not None and prev.neighbor_strikes_active is not None:
            new_strikes = current_active - prev.neighbor_strikes_active
            if new_strikes > 0:
                bonus = new_strikes * self._scoring.ghost_strike_bonus
                result.conviction_delta += bonus
                result.signals_fired.append(f"chain_spread_{new_strikes}_new_strikes")

    def _score_put_call_shift(
        self,
        signal: Signal,
        chain: ChainPollResult,
        result: ConvictionResult,
    ) -> None:
        """Score if the put/call ratio on this expiry shifted toward the signal's direction."""
        if not chain.neighbor_strikes:
            return

        call_oi = sum(n.oi for n in chain.neighbor_strikes if n.option_type == "call")
        put_oi = sum(n.oi for n in chain.neighbor_strikes if n.option_type == "put")
        total = call_oi + put_oi
        if total == 0:
            return

        call_ratio = call_oi / total

        # Signal is bullish → want call_ratio > 0.6
        # Signal is bearish → want call_ratio < 0.4
        if signal.direction == "bullish" and call_ratio > 0.60:
            result.conviction_delta += self._scoring.put_call_shift_bonus
            result.signals_fired.append(f"call_heavy_ratio_{call_ratio:.2f}")
        elif signal.direction == "bearish" and call_ratio < 0.40:
            result.conviction_delta += self._scoring.put_call_shift_bonus
            result.signals_fired.append(f"put_heavy_ratio_{1 - call_ratio:.2f}")

    def _score_premium_accumulation(
        self,
        signal: Signal,
        flow: FlowWatchResult,
        result: ConvictionResult,
        ledger_agg: LedgerAggregate | None = None,
    ) -> None:
        """Bonus if cumulative premium across all flow exceeds 2x initial."""
        new_premium = sum(e.premium for e in flow.events)
        if ledger_agg is not None and ledger_agg.total_entries > 0:
            total_premium = ledger_agg.total_premium
        else:
            total_premium = signal.cumulative_premium + new_premium
        if signal.initial_premium > 0 and total_premium > 2.0 * signal.initial_premium:
            result.conviction_delta += self._scoring.premium_accumulation_bonus
            result.signals_fired.append(
                f"premium_accumulated_{total_premium / signal.initial_premium:.1f}x"
            )

    # ─── Negative scoring ───

    def _score_spread_widening(
        self,
        chain: ChainPollResult,
        result: ConvictionResult,
    ) -> None:
        """Penalty if bid-ask spread is dangerously wide."""
        bid = chain.contract_bid
        ask = chain.contract_ask
        if bid is not None and ask is not None and bid > 0:
            mid = (bid + ask) / 2
            spread_pct = (ask - bid) / mid * 100 if mid > 0 else 0
            if spread_pct > 20.0:
                result.conviction_delta += self._scoring.spread_widened_penalty
                result.signals_fired.append(f"spread_wide_{spread_pct:.0f}pct")

    def _score_silence(
        self,
        signal: Signal,
        flow: FlowWatchResult,
        now: datetime,
        result: ConvictionResult,
    ) -> None:
        """Penalty for prolonged silence — no new flow for N+ days."""
        if flow.events:
            # Flow was found, reset counter (handled in _score_new_flow)
            return

        # Increment days without flow
        if signal.last_flow_at:
            days_silent = (now - signal.last_flow_at).total_seconds() / 86400
        else:
            days_silent = (now - signal.created_at).total_seconds() / 86400

        result.days_without_flow = int(days_silent)

        if days_silent >= self._cfg.silence_decay_days:
            excess_days = int(days_silent - self._cfg.silence_decay_days) + 1
            penalty = excess_days * self._scoring.silence_penalty_per_day
            result.conviction_delta += penalty  # already negative
            result.signals_fired.append(f"silence_{int(days_silent)}d")

    def _score_spot_movement(
        self,
        signal: Signal,
        chain: ChainPollResult,
        result: ConvictionResult,
    ) -> None:
        """Penalty if spot price moved further from strike (deeper OTM)."""
        if chain.spot_price is None or signal.strike <= 0:
            return

        # Current distance from strike
        if signal.direction == "bullish":
            # Call: want spot to approach strike from below, or exceed it
            distance_pct = (signal.strike - chain.spot_price) / signal.strike * 100
        else:
            # Put: want spot to approach strike from above, or drop below
            distance_pct = (chain.spot_price - signal.strike) / signal.strike * 100

        # Only penalize if spot moved AWAY (distance increased)
        # We need the initial distance to compare, approximate from initial data
        if distance_pct > 15.0:  # significantly OTM
            ticks = int((distance_pct - 10.0) / 5.0)
            penalty = ticks * self._scoring.spot_moved_away_per_5pct
            result.conviction_delta += penalty
            result.signals_fired.append(f"spot_moved_away_{distance_pct:.0f}pct_otm")

    def _score_dte_pressure(
        self,
        signal: Signal,
        now: datetime,
        result: ConvictionResult,
    ) -> None:
        """Penalty as DTE drops below 14 — time is running out."""
        try:
            expiry_date = date.fromisoformat(signal.expiry)
            dte = (expiry_date - now.date()).days
        except (ValueError, TypeError):
            return

        if dte < 14:
            result.conviction_delta += self._scoring.dte_pressure_below_14
            result.signals_fired.append(f"dte_pressure_{dte}d")

    # ─── State transitions ───

    def _next_state(
        self,
        signal: Signal,
        new_conviction: float,
        flow: FlowWatchResult,
        chain: ChainPollResult,
        now: datetime,
    ) -> SignalState:
        """Determine the next state based on accumulated evidence.

        State machine:
          pending → accumulating:  first confirming signal (any positive delta in this cycle)
          accumulating → actionable:  conviction ≥ threshold AND min confirming flows AND OI ratio
          any active → decayed:  conviction dropped below decay threshold
          (terminal conditions handled in _check_terminal, not here)
        """
        cfg = self._cfg
        total_flows = signal.confirming_flows + len(flow.events)

        # Check for decay
        if new_conviction < cfg.decay_conviction and signal.state in (
            SignalState.PENDING, SignalState.ACCUMULATING
        ):
            return SignalState.DECAYED

        # pending → accumulating
        if signal.state == SignalState.PENDING:
            has_confirming = len(flow.events) > 0
            oi_increased = (
                chain.contract_oi is not None
                and signal.initial_oi > 0
                and chain.contract_oi > signal.initial_oi
            )
            if has_confirming or oi_increased:
                return SignalState.ACCUMULATING
            return SignalState.PENDING

        # accumulating → actionable
        if signal.state == SignalState.ACCUMULATING:
            conviction_met = new_conviction >= cfg.actionable_conviction
            flows_met = total_flows >= cfg.actionable_min_confirming_flows
            oi_ratio_met = (
                chain.contract_oi is not None
                and signal.initial_oi > 0
                and (chain.contract_oi / signal.initial_oi) >= cfg.actionable_min_oi_ratio
            )
            if conviction_met and flows_met and oi_ratio_met:
                return SignalState.ACTIONABLE
            return SignalState.ACCUMULATING

        # Already actionable — stay there (Agent C will transition to executed)
        return signal.state
