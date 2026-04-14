"""Enrichment / re-grader settings loaded from rules.yaml `enrichment` section."""

from __future__ import annotations

from dataclasses import dataclass


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

    regrader: RegraderConfig = RegraderConfig()


def load_enrichment_config(raw_config: dict) -> EnrichmentConfig:
    """Parse `enrichment` from the full config dict (e.g. load_config output)."""
    section = raw_config.get("enrichment") or {}
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
    return EnrichmentConfig(regrader=regrader)
