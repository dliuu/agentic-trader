"""Milestone-triggered LLM re-grading for monitored signals (Gate 3 agents + synthesis)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timezone

import httpx
import structlog
from pydantic import BaseModel, Field, field_validator

from grader.agents.insider_tracker import InsiderTracker
from grader.agents.sector_analyst import SectorAnalyst
from grader.agents.sentiment_analyst import SentimentAnalyst
from grader.context.sentiment_ctx import SentimentContextBuilder
from grader.llm_client import LLMClient, LLMResponse
from grader.parser import ParseError, normalize_verdict, parse_llm_response
from shared.db import get_db
from shared.models import Candidate
from tracker.enrichment_config import RegraderConfig
from tracker.models import (
    ChainPollResult,
    FlowWatchResult,
    LedgerAggregate,
    NewsWatchResult,
    RegradeResult,
    Signal,
    TERMINAL_STATES,
)
from tracker.news_watcher import NewsWatcher
from tracker.signal_store import SignalStore

log = structlog.get_logger()


class EnrichedLLMClient:
    """Appends enrichment context to every LLM user message (token accounting)."""

    def __init__(self, inner: LLMClient, enrichment_block: str):
        self._inner = inner
        self._enrichment = enrichment_block
        self.input_tokens = 0
        self.output_tokens = 0
        self.latency_ms = 0

    async def complete(
        self, system: str, user: str, max_tokens: int | None = None
    ) -> LLMResponse:
        enriched_user = user + "\n" + self._enrichment
        r = await self._inner.complete(system, enriched_user, max_tokens=max_tokens)
        self.input_tokens += r.input_tokens
        self.output_tokens += r.output_tokens
        self.latency_ms += r.latency_ms
        return r


class RegradeSynthesisLLMOutput(BaseModel):
    """JSON returned by re-grade synthesis."""

    model_config = {"extra": "ignore"}

    score: int
    verdict: str
    rationale: str = ""
    key_development: str = ""
    thesis_status: str = "unchanged"

    @field_validator("score")
    @classmethod
    def clamp_score(cls, v: int) -> int:
        return max(1, min(100, int(v)))

    @field_validator("verdict", mode="before")
    @classmethod
    def verdict_valid(cls, v: str) -> str:
        return normalize_verdict(str(v))


def build_regrade_synthesis_system_prompt(days_monitored: int) -> str:
    return f"""You are re-evaluating a previously flagged options flow signal.

The original discovery happened approximately {days_monitored} day(s) ago (calendar). Since then, the system has
been monitoring this ticker continuously. You now have accumulated evidence: flow timeline,
OI evolution, news events, and SEC filings.

Your job: produce ONE final re-grade score from 1-100 that reflects whether the
accumulated evidence STRENGTHENS or WEAKENS the original informed-positioning thesis.

SCORING GUIDE:
- 85-100: Accumulated evidence strongly confirms informed positioning. Multi-day
  accumulation, OI building, catalyst headlines, insider alignment.
- 70-84: Evidence is supportive. Pattern is developing as expected.
- 50-69: Mixed. Some confirming, some concerning. The thesis is neither proven nor dead.
- 30-49: Evidence weakening. Flow dried up, OI flat, or news contradicts the thesis.
- 1-29: Thesis is dead. Pattern broke, catalyst didn't materialize, or public
  explanation emerged.

RESPONSE FORMAT — respond with ONLY a JSON object:
{{
  "score": <integer 1-100>,
  "verdict": "pass" or "fail",
  "rationale": "<2-4 sentences explaining how accumulated evidence changed the picture>",
  "key_development": "<the single most important new piece of evidence>",
  "thesis_status": "strengthening" | "unchanged" | "weakening" | "invalidated"
}}
"""


def _signal_to_candidate(signal: Signal) -> Candidate:
    try:
        exp = date.fromisoformat(signal.expiry)
        dte = max(0, (exp - datetime.now(timezone.utc).date()).days)
    except (ValueError, TypeError):
        dte = 0
    return Candidate(
        id=f"regrade-{signal.id}",
        source="regrader",
        ticker=signal.ticker.upper(),
        direction=signal.direction,
        strike=signal.strike,
        expiry=signal.expiry,
        premium_usd=float(signal.initial_premium),
        underlying_price=None,
        implied_volatility=None,
        execution_type="sweep",
        dte=dte,
        signals=[],
        confluence_score=0.0,
        volume=int(signal.initial_volume),
        open_interest=int(signal.initial_oi),
        raw_alert_id=f"regrade-{signal.id}",
    )


def check_milestone_triggers(
    signal: Signal,
    chain: ChainPollResult,
    news: NewsWatchResult | None,
    cfg: RegraderConfig,
) -> str | None:
    """Return milestone key if newly satisfied, else None."""
    already = set(signal.milestones_fired)

    if (
        "premium_2x" not in already
        and signal.initial_premium > 0
        and signal.cumulative_premium >= cfg.premium_multiple_trigger * signal.initial_premium
    ):
        return "premium_2x"

    if (
        "oi_3x" not in already
        and chain.contract_oi is not None
        and signal.initial_oi > 0
        and chain.contract_oi >= cfg.oi_multiple_trigger * signal.initial_oi
    ):
        return "oi_3x"

    if (
        "confirming_flows_3" not in already
        and signal.confirming_flows >= cfg.confirming_flows_trigger
    ):
        return "confirming_flows_3"

    if (
        news is not None
        and news.regrade_recommended
        and "catalyst_headline" not in already
    ):
        return "catalyst_headline"

    if news is not None and news.filing_detected and "sec_filing" not in already:
        return "sec_filing"

    return None


class Regrader:
    """Milestone-triggered LLM re-grading with Gate 3 agents + re-grade synthesis."""

    def __init__(
        self,
        llm_client: LLMClient,
        uw_client: httpx.AsyncClient,
        api_token: str,
        finnhub_api_key: str,
        store: SignalStore,
        config: RegraderConfig | None = None,
        news_watcher: NewsWatcher | None = None,
    ):
        self._llm = llm_client
        self._uw = uw_client
        self._api_token = api_token
        self._finnhub_key = finnhub_api_key
        self._cfg = config or RegraderConfig()
        self._news_watcher = news_watcher
        self._store = store

    async def maybe_regrade(
        self,
        signal: Signal,
        chain: ChainPollResult,
        flow: FlowWatchResult,
        news: NewsWatchResult | None,
        ledger_agg: LedgerAggregate | None,
        deterministic_conviction: float,
        *,
        signal_for_milestones: Signal | None = None,
    ) -> RegradeResult:
        """Evaluate guards and milestones; run re-grade when warranted."""
        _ = flow
        if signal.state in TERMINAL_STATES:
            return RegradeResult(
                signal_id=signal.id,
                triggered=False,
                skipped_reason="terminal_signal",
            )

        if signal.regrade_count >= self._cfg.max_regrades_per_signal:
            return RegradeResult(
                signal_id=signal.id,
                triggered=False,
                skipped_reason="regrade_budget_exhausted",
            )

        if signal.last_regraded_at:
            elapsed = (
                datetime.now(timezone.utc) - signal.last_regraded_at
            ).total_seconds()
            if elapsed < self._cfg.min_interval_seconds:
                return RegradeResult(
                    signal_id=signal.id,
                    triggered=False,
                    skipped_reason="min_interval_not_elapsed",
                )

        ms_signal = signal_for_milestones or signal
        trigger = check_milestone_triggers(ms_signal, chain, news, self._cfg)
        if trigger is None:
            return RegradeResult(
                signal_id=signal.id,
                triggered=False,
                skipped_reason="no_milestone_met",
            )

        return await self._run_regrade(
            signal, chain, ledger_agg, news, trigger, deterministic_conviction
        )

    async def _build_enrichment_block(
        self,
        signal: Signal,
        chain: ChainPollResult,
        ledger_agg: LedgerAggregate | None,
        trigger: str,
    ) -> str:
        lines: list[str] = []
        lines.append("")
        lines.append("=" * 60)
        lines.append("ACCUMULATED EVIDENCE SINCE SIGNAL CREATION")
        lines.append("=" * 60)

        now = datetime.now(timezone.utc)
        days_monitored = max(0, (now - signal.created_at).days)
        lines.append("\nSIGNAL STATUS:")
        lines.append(f"- Ticker: {signal.ticker}")
        lines.append(
            f"- Contract: {signal.strike} {signal.option_type} exp {signal.expiry}"
        )
        lines.append(f"- State: {signal.state.value}")
        lines.append(f"- Days monitored: {days_monitored}")
        lines.append(f"- Current conviction: {signal.conviction_score:.1f}")
        lines.append(
            f"- Re-grade #{signal.regrade_count + 1} of {self._cfg.max_regrades_per_signal}"
        )
        lines.append(f"- Trigger: {trigger}")

        if ledger_agg and ledger_agg.total_entries > 0:
            lines.append("\nFLOW ACCUMULATION:")
            lines.append(f"- Total flow events: {ledger_agg.total_entries}")
            lines.append(f"- Total premium: ${ledger_agg.total_premium:,.0f}")
            lines.append(f"- Distinct trading days: {ledger_agg.distinct_days}")
            lines.append(f"- Same contract hits: {ledger_agg.same_contract_count}")
            lines.append(
                f"- Same expiry, different strikes: {ledger_agg.same_expiry_count}"
            )
            lines.append(f"- Different expiries: {ledger_agg.different_expiry_count}")
            lines.append(f"- Distinct strikes: {ledger_agg.distinct_strikes}")
            lines.append(
                f"- Sweeps: {ledger_agg.sweep_count}, Blocks: {ledger_agg.block_count}"
            )
            if ledger_agg.latest_entry_at:
                ago = now - ledger_agg.latest_entry_at
                hours = max(0, int(ago.total_seconds() // 3600))
                lines.append(f"- Latest flow: {hours} hours ago")
        else:
            lines.append("\nFLOW ACCUMULATION: (no flow ledger data)")

        lines.append("\nOI HISTORY:")
        lines.append(f"- Initial OI: {signal.initial_oi}")
        if chain.contract_oi is not None and signal.initial_oi > 0:
            ratio = chain.contract_oi / signal.initial_oi
            lines.append(f"- Current OI: {chain.contract_oi} ({ratio:.1f}x initial)")
        lines.append(f"- Peak OI: {signal.oi_high_water}")

        snapshots = await self._store.get_snapshots(
            signal.id, limit=self._cfg.max_snapshots_in_prompt
        )
        if snapshots:
            oi_values = [
                s.contract_oi
                for s in reversed(snapshots)
                if s.contract_oi is not None
            ]
            if len(oi_values) >= 3:
                if oi_values[-1] > oi_values[0] * 1.2:
                    trend = "steadily increasing"
                elif oi_values[-1] < oi_values[0] * 0.8:
                    trend = "declining"
                else:
                    trend = "stable"
                lines.append(f"- OI trend: {trend} over {len(oi_values)} snapshots")

        if self._news_watcher:
            news_events = await self._news_watcher.get_events_for_signal(signal.id)
            cap = self._cfg.max_news_events_in_prompt
            if news_events:
                lines.append(f"\nNEWS EVENTS ({len(news_events)}):")
                for evt in news_events[:cap]:
                    date_str = evt.published_at.strftime("%Y-%m-%d")
                    et = evt.event_type.value.upper()
                    cat = (
                        f" (catalyst: {', '.join(evt.catalyst_keywords)})"
                        if evt.catalyst_matched
                        else ""
                    )
                    title = (evt.title or "")[:120]
                    lines.append(f"- [{date_str}] {et}: {title}{cat}")
            else:
                lines.append("\nNEWS EVENTS: (none detected)")
        else:
            lines.append("\nNEWS EVENTS: (news watcher not available)")

        lines.append("")
        lines.append("Use this accumulated evidence to re-assess the original thesis.")
        lines.append("Has the information environment strengthened or weakened the case?")
        lines.append("=" * 60)
        return "\n".join(lines)

    async def _run_agents(
        self,
        candidate: Candidate,
        enrichment: str,
    ) -> tuple[int, int, int, int, int, int]:
        """Sentiment + insider (LLM with enrichment); sector deterministic."""
        enriched = EnrichedLLMClient(self._llm, enrichment)
        sentiment_builder = SentimentContextBuilder(
            self._uw, self._api_token, self._finnhub_key
        )
        sentiment_agent = SentimentAnalyst(sentiment_builder, enriched)
        insider_agent = InsiderTracker(
            self._uw, self._api_token, self._finnhub_key, enriched
        )
        sector_agent = SectorAnalyst(self._uw, self._api_token)

        results = await asyncio.gather(
            sentiment_agent.score(candidate),
            insider_agent.score(candidate),
            sector_agent.score(candidate),
            return_exceptions=True,
        )

        scores: list[int] = []
        for result in results:
            if isinstance(result, Exception):
                log.warning("regrader.agent_failed", error=str(result))
                scores.append(50)
            else:
                scores.append(int(result.score))

        lat = enriched.latency_ms
        return scores[0], scores[1], scores[2], enriched.input_tokens, enriched.output_tokens, lat

    async def _run_synthesis(
        self,
        candidate: Candidate,
        signal: Signal,
        sentiment_score: int,
        insider_score: int,
        sector_score: int,
        enrichment: str,
        deterministic_conviction: float,
    ) -> tuple[int, str, int, int, int]:
        """Re-grade synthesis LLM call."""
        now = datetime.now(timezone.utc)
        days_ago = max(0, (now - signal.created_at).days)
        system = build_regrade_synthesis_system_prompt(days_ago)

        orig = int(round(signal.initial_score))
        user_parts = [
            "ORIGINAL DISCOVERY (deterministic + synthesis at intake; sub-scores not stored separately):",
            f"- Final Gate 3 synthesis score at discovery: {orig}",
            f"- Treat flow / volatility / risk analyst legs as consistent with that discovery score.",
            "",
            "REFRESHED LLM / AGENT SCORES (this re-grade cycle):",
            f"- Sentiment analyst: {sentiment_score}",
            f"- Insider tracker: {insider_score}",
            f"- Sector analyst (deterministic): {sector_score}",
            "",
            f"CURRENT DETERMINISTIC CONVICTION (before blend): {deterministic_conviction:.1f}",
            "",
            "CANDIDATE:",
            f"- Ticker: {candidate.ticker}, direction: {candidate.direction}, "
            f"strike {candidate.strike}, expiry {candidate.expiry}, premium ${candidate.premium_usd:,.0f}",
            "",
            enrichment,
        ]
        user = "\n".join(user_parts)

        resp = await self._llm.complete(system, user, max_tokens=self._cfg.max_tokens)
        try:
            parsed = parse_llm_response(resp.text, RegradeSynthesisLLMOutput)
        except (ParseError, Exception) as e:
            log.warning("regrader.synthesis_parse_failed", error=str(e))
            raise

        return (
            int(parsed.score),
            str(parsed.rationale or ""),
            resp.input_tokens,
            resp.output_tokens,
            resp.latency_ms,
        )

    async def _persist_regrade(self, result: RegradeResult) -> None:
        if not result.triggered or result.regraded_at is None:
            return
        db = await get_db()
        try:
            await db.execute(
                """INSERT INTO regrades
                   (id, signal_id, trigger_reason, sentiment_score, insider_score, sector_score,
                    synthesis_score, synthesis_rationale, deterministic_conviction, blended_conviction,
                    input_tokens, output_tokens, latency_ms, regraded_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(uuid.uuid4()),
                    result.signal_id,
                    result.trigger_reason or "",
                    result.sentiment_score,
                    result.insider_score,
                    result.sector_score,
                    result.synthesis_score,
                    result.synthesis_rationale,
                    result.deterministic_conviction,
                    result.blended_conviction,
                    result.total_input_tokens,
                    result.total_output_tokens,
                    result.total_latency_ms,
                    result.regraded_at.isoformat(),
                ),
            )
            await db.commit()
        finally:
            await db.close()

    async def _run_regrade(
        self,
        signal: Signal,
        chain: ChainPollResult,
        ledger_agg: LedgerAggregate | None,
        news: NewsWatchResult | None,
        trigger: str,
        deterministic_conviction: float,
    ) -> RegradeResult:
        now = datetime.now(timezone.utc)
        enrichment = await self._build_enrichment_block(signal, chain, ledger_agg, trigger)
        candidate = _signal_to_candidate(signal)

        s_score, i_score, sec_score, ain, aout, alat = await self._run_agents(
            candidate, enrichment
        )

        syn_score, syn_rationale, sin, sout, slat = await self._run_synthesis(
            candidate,
            signal,
            s_score,
            i_score,
            sec_score,
            enrichment,
            deterministic_conviction,
        )

        total_in = ain + sin
        total_out = aout + sout
        total_lat = alat + slat

        det_pct = self._cfg.score_blend_deterministic_pct / 100.0
        llm_pct = self._cfg.score_blend_llm_pct / 100.0
        s_norm = det_pct + llm_pct
        if s_norm <= 0:
            det_pct, llm_pct = 0.55, 0.45
        else:
            det_pct /= s_norm
            llm_pct /= s_norm

        blended = det_pct * deterministic_conviction + llm_pct * float(syn_score)
        blended = max(0.0, min(100.0, blended))

        result = RegradeResult(
            signal_id=signal.id,
            triggered=True,
            trigger_reason=trigger,
            sentiment_score=s_score,
            insider_score=i_score,
            sector_score=sec_score,
            synthesis_score=syn_score,
            synthesis_rationale=syn_rationale,
            deterministic_conviction=deterministic_conviction,
            blended_conviction=blended,
            total_input_tokens=total_in,
            total_output_tokens=total_out,
            total_latency_ms=total_lat,
            regraded_at=now,
        )
        await self._persist_regrade(result)

        log.info(
            "regrader.complete",
            signal_id=signal.id,
            ticker=signal.ticker,
            trigger=trigger,
            regrade_num=signal.regrade_count + 1,
            sentiment=s_score,
            insider=i_score,
            sector=sec_score,
            synthesis=syn_score,
            deterministic=round(deterministic_conviction, 1),
            blended=round(blended, 1),
            tokens=total_in + total_out,
        )
        return result
