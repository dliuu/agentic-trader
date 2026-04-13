"""Watch for new unusual flow on a signal's ticker.

Checks two sources:
  1. UW /api/option-trades/flow-alerts?ticker_symbol={ticker} — catches
     flow that the scanner might have filtered out.
  2. Scanner's candidates table — catches flow the scanner already flagged.

Returns FlowWatchResult with all new flow events since the signal's
last poll timestamp.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
import structlog

from shared.uw_http import uw_get
from shared.uw_validation import uw_auth_headers
from tracker.flow_ledger import FlowLedger
from tracker.models import FlowEvent, FlowWatchResult, Signal

log = structlog.get_logger()

UW_BASE = "https://api.unusualwhales.com"


class FlowWatcher:
    """Detect new unusual flow on a signal's ticker."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        api_token: str,
        scanner_db_path: str | None = None,
        *,
        flow_ledger: FlowLedger | None = None,
    ):
        self._client = client
        self._headers = uw_auth_headers(api_token)
        self._scanner_db_path = scanner_db_path
        self._flow_ledger = flow_ledger

    async def check(self, signal: Signal) -> FlowWatchResult:
        """Check for new flow events on the signal's ticker.

        Args:
            signal: The active signal to check. Uses signal.last_polled_at
                    as the cutoff timestamp — only returns events after this.

        Returns:
            FlowWatchResult containing all new flow events found.
        """
        now = datetime.now(timezone.utc)
        cutoff = signal.last_polled_at or signal.created_at

        # Source 1: UW flow alerts API
        uw_events = await self._fetch_uw_flow(signal, cutoff)

        # Source 2: Scanner candidates table
        scanner_events = await self._fetch_scanner_candidates(signal, cutoff)

        # Source 3: Flow ledger (sub-threshold scanner flow already persisted)
        ledger_events = await self._fetch_ledger_entries(signal, cutoff)

        # Merge and deduplicate by alert_id
        seen_ids: set[str] = set()
        merged: list[FlowEvent] = []
        for event in uw_events + scanner_events + ledger_events:
            if event.alert_id not in seen_ids:
                seen_ids.add(event.alert_id)
                merged.append(event)

        if merged:
            log.info(
                "flow_watcher.new_flow",
                ticker=signal.ticker,
                signal_id=signal.id,
                count=len(merged),
                total_premium=sum(e.premium for e in merged),
            )

        return FlowWatchResult(
            ticker=signal.ticker,
            checked_at=now,
            events=merged,
        )

    async def _fetch_uw_flow(
        self, signal: Signal, cutoff: datetime
    ) -> list[FlowEvent]:
        """Query UW flow alerts for the signal's ticker."""
        try:
            resp = await uw_get(
                self._client,
                f"{UW_BASE}/api/option-trades/flow-alerts",
                headers=self._headers,
                params={
                    "ticker_symbol": signal.ticker,
                    "limit": 50,
                },
            )
            resp.raise_for_status()
            raw = resp.json()
        except Exception as exc:
            log.warning(
                "flow_watcher.uw_fetch_failed",
                ticker=signal.ticker,
                error=str(exc),
            )
            return []

        data = raw.get("data", raw) if isinstance(raw, dict) else raw
        if not isinstance(data, list):
            return []

        events: list[FlowEvent] = []
        for item in data:
            if not isinstance(item, dict):
                continue

            # Parse timestamp
            created_raw = item.get("created_at") or item.get("timestamp") or ""
            try:
                created = datetime.fromisoformat(
                    str(created_raw).replace("Z", "+00:00")
                )
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue

            # Only include events after the cutoff
            if created <= cutoff:
                continue

            alert_id = str(item.get("id", ""))
            if not alert_id:
                continue

            strike = self._parse_float(item.get("strike") or item.get("strike_price")) or 0.0
            expiry = str(item.get("expiry") or item.get("expiration_date") or "")
            opt_raw = str(item.get("type") or item.get("option_type") or "").lower()
            option_type = "call" if opt_raw in ("call", "calls", "c") else "put"
            premium = self._parse_float(item.get("total_premium") or item.get("premium")) or 0.0
            volume = self._parse_int(item.get("total_size") or item.get("volume")) or 0

            # Determine fill type
            fill_type = item.get("execution_type")
            if fill_type is None:
                if item.get("has_sweep"):
                    fill_type = "sweep"
                elif item.get("has_floor"):
                    fill_type = "block"

            is_same_contract = (
                strike == signal.strike
                and expiry == signal.expiry
                and option_type == signal.option_type
            )
            is_same_expiry = expiry == signal.expiry and not is_same_contract

            events.append(FlowEvent(
                alert_id=alert_id,
                strike=strike,
                expiry=expiry,
                option_type=option_type,
                premium=premium,
                volume=volume,
                fill_type=fill_type,
                is_same_contract=is_same_contract,
                is_same_expiry=is_same_expiry,
                created_at=created,
            ))

        return events

    async def _fetch_scanner_candidates(
        self, signal: Signal, cutoff: datetime
    ) -> list[FlowEvent]:
        """Check the scanner's candidates table for new entries.

        This catches flow that the scanner flagged but that may or may not
        have survived the grader gates.
        """
        if self._scanner_db_path is None:
            return []

        try:
            import aiosqlite

            async with aiosqlite.connect(self._scanner_db_path) as db:
                cursor = await db.execute(
                    "SELECT id, ticker, strike, expiry, premium_usd, direction, "
                    "scanned_at, signals_json "
                    "FROM candidates "
                    "WHERE ticker = ? AND scanned_at > ? "
                    "ORDER BY scanned_at DESC LIMIT 20",
                    (signal.ticker, cutoff.isoformat()),
                )
                rows = await cursor.fetchall()
        except Exception as exc:
            log.warning(
                "flow_watcher.scanner_db_failed",
                error=str(exc),
            )
            return []

        events: list[FlowEvent] = []
        for row in rows:
            row_id, _ticker, strike, expiry, premium, direction, scanned_at_str, _ = row
            try:
                scanned_at = datetime.fromisoformat(scanned_at_str)
                if scanned_at.tzinfo is None:
                    scanned_at = scanned_at.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue

            option_type = "call" if direction == "bullish" else "put"
            is_same_contract = (
                strike == signal.strike
                and expiry == signal.expiry
                and option_type == signal.option_type
            )
            is_same_expiry = expiry == signal.expiry and not is_same_contract

            events.append(FlowEvent(
                alert_id=f"scanner:{row_id}",
                strike=strike,
                expiry=expiry,
                option_type=option_type,
                premium=premium,
                volume=0,  # scanner candidates don't always carry volume
                fill_type=None,
                is_same_contract=is_same_contract,
                is_same_expiry=is_same_expiry,
                created_at=scanned_at,
            ))

        return events

    async def _fetch_ledger_entries(
        self, signal: Signal, cutoff: datetime
    ) -> list[FlowEvent]:
        """Load ledger rows for this signal since cutoff as FlowEvents."""
        if self._flow_ledger is None:
            return []
        try:
            entries = await self._flow_ledger.get_entries(signal.id, since=cutoff)
        except Exception as exc:
            log.warning(
                "flow_watcher.ledger_read_failed",
                signal_id=signal.id,
                error=str(exc),
            )
            return []
        out: list[FlowEvent] = []
        for le in entries:
            created = le.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            ft = (le.execution_type or "").lower() or None
            out.append(
                FlowEvent(
                    alert_id=le.alert_id,
                    strike=le.strike,
                    expiry=le.expiry,
                    option_type=le.option_type,
                    premium=le.premium,
                    volume=le.volume,
                    fill_type=ft,
                    is_same_contract=le.is_same_contract,
                    is_same_expiry=le.is_same_expiry,
                    created_at=created,
                )
            )
        return out

    @staticmethod
    def _parse_float(v: Any) -> float | None:
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_int(v: Any) -> int | None:
        if v is None:
            return None
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None
