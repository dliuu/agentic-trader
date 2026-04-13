"""Signal tracker data models."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class SignalState(str, Enum):
    """Lifecycle states for a tracked signal."""

    PENDING = "pending"                # Just created, no confirming evidence yet
    ACCUMULATING = "accumulating"      # At least one confirming data point observed
    ACTIONABLE = "actionable"          # Conviction confirmed, ready for Agent C
    EXECUTED = "executed"              # Agent C placed the trade (terminal)
    EXPIRED = "expired"               # Option expired or DTE too low (terminal)
    DECAYED = "decayed"               # Monitoring window elapsed without confirmation (terminal)


TERMINAL_STATES = frozenset({
    SignalState.EXECUTED,
    SignalState.EXPIRED,
    SignalState.DECAYED,
})

ACTIVE_STATES = frozenset({
    SignalState.PENDING,
    SignalState.ACCUMULATING,
})

# Signals still receiving scanner ledger + ticker-level intake dedup (excludes terminal).
MONITORING_STATES = frozenset({
    SignalState.PENDING,
    SignalState.ACCUMULATING,
    SignalState.ACTIONABLE,
})


class Signal(BaseModel):
    """A living, tracked anomaly.

    Created when a candidate passes the grading pipeline (Gates 0-3 + synthesis).
    Monitored over hours/days. Accumulates evidence until it either matures
    into an actionable trade or decays away.
    """

    model_config = {"extra": "forbid"}

    id: str                                  # uuid4
    ticker: str
    strike: float
    expiry: str                              # ISO date "YYYY-MM-DD"
    option_type: str                         # "call" or "put"
    direction: str                           # "bullish" or "bearish"
    state: SignalState = SignalState.PENDING

    # --- From the original grading pass ---
    initial_score: int                       # synthesis score (78-100)
    initial_premium: float                   # premium from the triggering flow
    initial_oi: int                          # open interest at signal creation
    initial_volume: int                      # volume at signal creation
    initial_contract_adv: int = 0            # avg daily volume on this contract
    grade_id: str                            # FK to grades table

    # --- Accumulated evidence (updated by monitor) ---
    conviction_score: float                  # starts at initial_score, evolves
    snapshots_taken: int = 0
    confirming_flows: int = 0                # count of new flow events on same ticker
    oi_high_water: int = 0                   # peak OI observed since creation
    chain_spread_count: int = 0              # adjacent strikes that have lit up
    cumulative_premium: float = 0.0          # total premium across all observed flow
    days_without_flow: int = 0               # consecutive calendar days with no new flow

    # --- Timestamps ---
    created_at: datetime
    last_polled_at: datetime | None = None
    last_flow_at: datetime | None = None     # when the most recent confirming flow was seen
    matured_at: datetime | None = None       # when it became actionable
    terminal_at: datetime | None = None      # when it reached a terminal state
    terminal_reason: str | None = None       # human-readable reason for terminal state

    # --- Context for Agent C ---
    risk_params_json: str | None = None      # serialized TradeRiskParams
    anomaly_fingerprint: str = ""            # one-line summary

    @property
    def is_active(self) -> bool:
        return self.state in ACTIVE_STATES

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES


class SignalSnapshot(BaseModel):
    """Point-in-time observation of a signal's contract and neighborhood.

    One snapshot is created per active signal per poll cycle. Persisted to
    the signal_snapshots table for backtesting and audit.
    """

    model_config = {"extra": "forbid"}

    id: str                                  # uuid4
    signal_id: str                           # FK to signals table
    snapshot_at: datetime

    # --- Contract-level data ---
    contract_oi: int | None = None           # current OI on the signal's exact contract
    contract_volume: int | None = None       # current day's volume
    contract_bid: float | None = None
    contract_ask: float | None = None
    contract_spread_pct: float | None = None # (ask - bid) / mid * 100
    spot_price: float | None = None          # current underlying price

    # --- Neighborhood data ---
    neighbor_oi_total: int | None = None     # sum of OI on adjacent strikes (same expiry)
    neighbor_strikes_active: int | None = None  # count of nearby strikes with OI > 0
    neighbor_put_call_ratio: float | None = None  # call_oi / put_oi for this expiry

    # --- Flow events since last snapshot ---
    new_flow_count: int = 0                  # new unusual flow events on this ticker
    new_flow_premium: float = 0.0            # total premium of new flow events
    new_flow_same_contract: int = 0          # flow on the exact same strike+expiry
    new_flow_same_expiry: int = 0            # flow on same expiry, different strike

    # --- Conviction engine output ---
    conviction_delta: float = 0.0            # change in conviction this cycle
    conviction_after: float = 0.0            # conviction score after this cycle
    signals_fired: list[str] = Field(default_factory=list)  # e.g. ["oi_increase_15pct"]

    # --- Debug ---
    notes: str | None = None


class ChainPollResult(BaseModel):
    """Raw data from polling the option chain for a signal.

    This is the intermediate structure between the chain_poller and the
    conviction engine. Not persisted directly — its contents are folded
    into SignalSnapshot.
    """

    model_config = {"extra": "forbid"}

    ticker: str
    polled_at: datetime

    # The signal's exact contract
    contract_oi: int | None = None
    contract_volume: int | None = None
    contract_bid: float | None = None
    contract_ask: float | None = None
    contract_last_price: float | None = None
    contract_iv: float | None = None

    # Underlying
    spot_price: float | None = None

    # Neighbors (same expiry, ±N strikes)
    neighbor_strikes: list[NeighborStrike] = Field(default_factory=list)

    # Same strike, adjacent expiries
    adjacent_expiry_oi: list[AdjacentExpiryOI] = Field(default_factory=list)

    # Whether the contract still exists in the chain
    contract_found: bool = True


class NeighborStrike(BaseModel):
    """OI/volume data for a single neighboring strike."""

    model_config = {"extra": "forbid"}

    strike: float
    option_type: str                         # "call" or "put"
    oi: int = 0
    volume: int = 0
    is_ghost: bool = False                   # True if OI > 0 but was 0 in prior snapshot


class AdjacentExpiryOI(BaseModel):
    """OI on the signal's strike but at a neighboring expiry date."""

    model_config = {"extra": "forbid"}

    expiry: str
    oi: int = 0
    volume: int = 0


class FlowWatchResult(BaseModel):
    """New unusual flow events observed on the signal's ticker since last poll."""

    model_config = {"extra": "forbid"}

    ticker: str
    checked_at: datetime
    events: list[FlowEvent] = Field(default_factory=list)


class FlowEvent(BaseModel):
    """A single new flow event found by the flow watcher."""

    model_config = {"extra": "forbid"}

    alert_id: str
    strike: float
    expiry: str
    option_type: str
    premium: float
    volume: int
    fill_type: str | None = None
    is_same_contract: bool = False           # exact match on strike + expiry + option_type
    is_same_expiry: bool = False             # same expiry, different strike
    created_at: datetime


class LedgerEntry(BaseModel):
    """A single flow event recorded in the flow ledger."""

    model_config = {"extra": "forbid"}

    id: str
    signal_id: str
    alert_id: str
    ticker: str
    strike: float
    expiry: str
    option_type: str
    direction: str
    premium: float
    volume: int = 0
    open_interest: int | None = None
    execution_type: str | None = None
    underlying_price: float | None = None
    implied_volatility: float | None = None
    is_same_contract: bool = False
    is_same_expiry: bool = False
    source: str = "scanner"
    created_at: datetime
    recorded_at: datetime


class LedgerAggregate(BaseModel):
    """Summary statistics from the flow ledger for a single signal."""

    model_config = {"extra": "forbid"}

    signal_id: str
    total_entries: int = 0
    total_premium: float = 0.0
    distinct_days: int = 0
    same_contract_count: int = 0
    same_expiry_count: int = 0
    different_expiry_count: int = 0
    distinct_strikes: int = 0
    distinct_expiries: int = 0
    sweep_count: int = 0
    block_count: int = 0
    latest_entry_at: datetime | None = None
    earliest_entry_at: datetime | None = None


class NewsEventType(str, Enum):
    """Classification of a news event."""

    HEADLINE = "headline"
    SEC_FILING = "sec_filing"


class NewsEvent(BaseModel):
    """A single news or filing event detected by the news watcher."""

    model_config = {"extra": "forbid"}

    id: str
    signal_id: str
    ticker: str
    event_type: NewsEventType
    title: str
    source: str
    url: str | None = None
    published_at: datetime
    detected_at: datetime

    catalyst_matched: bool = False
    catalyst_keywords: list[str] = Field(default_factory=list)
    filing_type: str | None = None

    source_id: str = ""


class NewsWatchResult(BaseModel):
    """Output of a single news watch cycle for one signal."""

    model_config = {"extra": "forbid"}

    signal_id: str
    ticker: str
    checked_at: datetime
    events: list[NewsEvent] = Field(default_factory=list)
    has_catalyst: bool = False
    catalyst_types: list[str] = Field(default_factory=list)
    filing_detected: bool = False
    regrade_recommended: bool = False
