"""Enrichment layer settings loaded from rules.yaml `enrichment` section.

This is the integration surface that wires:
- Flow ledger aggregation
- News watcher polling
- LLM re-grader
"""

from __future__ import annotations

from dataclasses import dataclass

from tracker.config import LedgerConfig, NewsWatcherConfig

@dataclass(frozen=True)
class RegraderConfig:
    """LLM re-grading settings."""

    enabled: bool = True
    max_regrades_per_signal: int = 5
    min_interval_seconds: int = 7200

    score_blend_deterministic_pct: float = 55.0
    score_blend_llm_pct: float = 45.0

    model: str = "claude-sonnet-4-20250514"
    max_tokens: int = 512
    timeout_seconds: float = 15.0

    premium_multiple_trigger: float = 2.0
    oi_multiple_trigger: float = 3.0
    confirming_flows_trigger: int = 3

    max_news_events_in_prompt: int = 10
    max_snapshots_in_prompt: int = 20


@dataclass(frozen=True)
class EnrichmentConfig:
    """Top-level enrichment block from rules.yaml."""

    news: NewsWatcherConfig = NewsWatcherConfig()
    ledger: LedgerConfig = LedgerConfig()
    regrader: RegraderConfig = RegraderConfig()


def load_enrichment_config(raw_config: dict) -> EnrichmentConfig | None:
    """Parse `enrichment` from the full config dict (e.g. load_config output)."""
    section = raw_config.get("enrichment") or {}
    if not section:
        return None

    news_raw = section.get("news") or {}
    news = NewsWatcherConfig(
        enabled=bool(news_raw.get("enabled", True)),
        headline_interval_seconds=int(news_raw.get("headline_interval_seconds", 14400)),
        edgar_interval_seconds=int(news_raw.get("edgar_interval_seconds", 14400)),
        headline_limit=int(news_raw.get("headline_limit", 20)),
        edgar_lookback_days=int(news_raw.get("edgar_lookback_days", 7)),
        edgar_user_agent=str(news_raw.get("edgar_user_agent", NewsWatcherConfig.edgar_user_agent)),
    )

    ledger_raw = section.get("ledger") or {}
    ledger = LedgerConfig(
        enabled=bool(ledger_raw.get("enabled", True)),
        purge_terminal_signals=bool(ledger_raw.get("purge_terminal_signals", True)),
        retention_days=int(ledger_raw.get("retention_days", 30)),
    )

    rg = section.get("regrader") or {}
    regrader = RegraderConfig(
        enabled=bool(rg.get("enabled", True)),
        max_regrades_per_signal=int(rg.get("max_regrades_per_signal", 5)),
        min_interval_seconds=int(rg.get("min_interval_seconds", 7200)),
        score_blend_deterministic_pct=float(rg.get("score_blend_deterministic_pct", 55.0)),
        score_blend_llm_pct=float(rg.get("score_blend_llm_pct", 45.0)),
        model=str(rg.get("model", "claude-sonnet-4-20250514")),
        max_tokens=int(rg.get("max_tokens", 512)),
        timeout_seconds=float(rg.get("timeout_seconds", 15.0)),
        premium_multiple_trigger=float(rg.get("premium_multiple_trigger", 2.0)),
        oi_multiple_trigger=float(rg.get("oi_multiple_trigger", 3.0)),
        confirming_flows_trigger=int(rg.get("confirming_flows_trigger", 3)),
        max_news_events_in_prompt=int(rg.get("max_news_events_in_prompt", 10)),
        max_snapshots_in_prompt=int(rg.get("max_snapshots_in_prompt", 20)),
    )
    return EnrichmentConfig(news=news, ledger=ledger, regrader=regrader)
