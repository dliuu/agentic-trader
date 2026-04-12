"""Poll option chain data for a tracked signal.

Makes one UW API call per signal per poll cycle to
/api/stock/{ticker}/option-chains. Extracts the signal's contract,
neighboring strikes, and adjacent expiry data.

All thresholds (neighbor radius, etc.) come from TrackerConfig.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
import structlog

from shared.uw_http import uw_get_json
from shared.uw_validation import uw_auth_headers
from tracker.config import TrackerConfig
from tracker.models import (
    AdjacentExpiryOI,
    ChainPollResult,
    NeighborStrike,
    Signal,
    SignalSnapshot,
)

log = structlog.get_logger()

UW_BASE = "https://api.unusualwhales.com"


class ChainPoller:
    """Fetches option chain snapshots for active signals."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        api_token: str,
        config: TrackerConfig | None = None,
    ):
        self._client = client
        self._headers = uw_auth_headers(api_token)
        self._cfg = config or TrackerConfig()

    async def poll(
        self,
        signal: Signal,
        prev_snapshot: SignalSnapshot | None = None,
    ) -> ChainPollResult:
        """Fetch chain data for a single signal.

        Args:
            signal: The active signal to poll.
            prev_snapshot: The most recent prior snapshot (used to detect
                           ghost strikes — strikes with OI that previously
                           had none). May be None for the first poll.

        Returns:
            ChainPollResult with contract data, neighbors, and adjacent expiries.
        """
        now = datetime.now(timezone.utc)

        try:
            raw = await uw_get_json(
                self._client,
                f"{UW_BASE}/api/stock/{signal.ticker}/option-chains",
                headers=self._headers,
                use_cache=False,  # always want fresh data for monitoring
            )
        except Exception as exc:
            log.warning(
                "chain_poller.fetch_failed",
                ticker=signal.ticker,
                error=str(exc),
            )
            return ChainPollResult(
                ticker=signal.ticker,
                polled_at=now,
                contract_found=False,
            )

        # UW option-chains returns a nested structure.
        # The exact shape varies: may be {"data": [...]} or just [...]
        chains = raw.get("data", raw) if isinstance(raw, dict) else raw
        if not isinstance(chains, list):
            chains = [chains] if isinstance(chains, dict) else []

        # Build lookup: (expiry, strike, option_type) -> contract_data
        contracts: dict[tuple[str, float, str], dict] = {}
        for entry in chains:
            if not isinstance(entry, dict):
                continue
            exp = str(entry.get("expiry") or entry.get("expiration_date") or "")
            stk = self._parse_float(entry.get("strike") or entry.get("strike_price"))
            opt = self._normalize_option_type(
                str(entry.get("option_type") or entry.get("type") or "")
            )
            if exp and stk is not None and opt:
                contracts[(exp, stk, opt)] = entry

        # 1. Find the signal's exact contract
        key = (signal.expiry, signal.strike, signal.option_type)
        contract = contracts.get(key)

        if contract is None:
            log.info(
                "chain_poller.contract_not_found",
                ticker=signal.ticker,
                strike=signal.strike,
                expiry=signal.expiry,
            )
            return ChainPollResult(
                ticker=signal.ticker,
                polled_at=now,
                contract_found=False,
            )

        contract_oi = self._parse_int(contract.get("open_interest") or contract.get("oi"))
        contract_vol = self._parse_int(contract.get("volume"))
        contract_bid = self._parse_float(contract.get("bid"))
        contract_ask = self._parse_float(contract.get("ask"))
        contract_last = self._parse_float(contract.get("last_price") or contract.get("last"))
        contract_iv = self._parse_float(contract.get("implied_volatility") or contract.get("iv"))
        spot = self._parse_float(
            contract.get("underlying_price") or contract.get("stock_price")
        )

        # 2. Find neighbors: same expiry, ±N strikes, both calls and puts
        all_strikes_this_expiry = sorted({
            stk for (exp, stk, _), _ in contracts.items()
            if exp == signal.expiry
        })

        # Find signal's position in the strike ladder
        try:
            center_idx = all_strikes_this_expiry.index(signal.strike)
        except ValueError:
            center_idx = -1

        radius = self._cfg.neighbor_strike_radius
        if center_idx >= 0:
            start = max(0, center_idx - radius)
            end = min(len(all_strikes_this_expiry), center_idx + radius + 1)
            neighbor_strikes_list = all_strikes_this_expiry[start:end]
        else:
            neighbor_strikes_list = []

        if prev_snapshot and prev_snapshot.neighbor_strikes_active is not None:
            # Per-strike OI not stored on snapshots; conviction uses active-count delta.
            pass

        neighbors: list[NeighborStrike] = []
        for stk in neighbor_strikes_list:
            if stk == signal.strike:
                continue  # skip the signal's own strike
            for opt_type in ("call", "put"):
                nkey = (signal.expiry, stk, opt_type)
                ndata = contracts.get(nkey)
                if ndata is None:
                    continue
                n_oi = self._parse_int(ndata.get("open_interest") or ndata.get("oi")) or 0
                n_vol = self._parse_int(ndata.get("volume")) or 0
                neighbors.append(NeighborStrike(
                    strike=stk,
                    option_type=opt_type,
                    oi=n_oi,
                    volume=n_vol,
                    is_ghost=False,  # refined in conviction engine with historical comparison
                ))

        # 3. Adjacent expiries: signal's strike on ±1 expiry dates
        all_expiries = sorted({exp for (exp, _, _) in contracts})
        try:
            exp_idx = all_expiries.index(signal.expiry)
        except ValueError:
            exp_idx = -1

        adjacent: list[AdjacentExpiryOI] = []
        exp_radius = self._cfg.neighbor_expiry_radius
        if exp_idx >= 0:
            for offset in range(-exp_radius, exp_radius + 1):
                adj_idx = exp_idx + offset
                if adj_idx < 0 or adj_idx >= len(all_expiries) or offset == 0:
                    continue
                adj_exp = all_expiries[adj_idx]
                adj_key = (adj_exp, signal.strike, signal.option_type)
                adj_data = contracts.get(adj_key)
                if adj_data:
                    adjacent.append(AdjacentExpiryOI(
                        expiry=adj_exp,
                        oi=self._parse_int(adj_data.get("open_interest") or adj_data.get("oi")) or 0,
                        volume=self._parse_int(adj_data.get("volume")) or 0,
                    ))

        return ChainPollResult(
            ticker=signal.ticker,
            polled_at=now,
            contract_oi=contract_oi,
            contract_volume=contract_vol,
            contract_bid=contract_bid,
            contract_ask=contract_ask,
            contract_last_price=contract_last,
            contract_iv=contract_iv,
            spot_price=spot,
            neighbor_strikes=neighbors,
            adjacent_expiry_oi=adjacent,
            contract_found=True,
        )

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

    @staticmethod
    def _normalize_option_type(v: str) -> str:
        v = v.strip().lower()
        if v in ("call", "calls", "c"):
            return "call"
        if v in ("put", "puts", "p"):
            return "put"
        return v
