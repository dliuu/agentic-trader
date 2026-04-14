from __future__ import annotations

import os
from pathlib import Path

import yaml


def load_config(config_path: str | Path | None = None) -> dict:
    """Load the unified YAML config used by scanner/grader agents."""
    if config_path is None:
        config_path = Path(__file__).resolve().parent.parent.parent / "config" / "rules.yaml"
    else:
        config_path = Path(config_path)

    if not config_path.exists():
        fallback = Path("config/rules.yaml")
        if fallback.exists():
            config_path = fallback
        else:
            raise FileNotFoundError(f"Config file not found: {config_path}")

    config = yaml.safe_load(config_path.read_text()) or {}
    # Inject secrets from environment (not stored in yaml)
    config["uw_api_token"] = (
        os.environ.get("UW_API_TOKEN") or os.environ.get("UNUSUAL_WHALES_API_TOKEN") or ""
    ).strip()
    config["anthropic_api_key"] = os.environ.get("ANTHROPIC_API_KEY", "")
    config["finnhub_api_key"] = os.environ.get("FINNHUB_API_KEY", "")
    return config


def gate_thresholds_from_config(config: dict | None):
    """Build ``GateThresholds`` from ``rules.yaml`` ``grader`` keys (YAML overrides code defaults)."""
    from shared.filters import GateThresholds as GT

    if not config:
        return GT()
    g = config.get("grader") or {}
    score_th = int(g.get("score_threshold", 70))
    return GT(
        flow_analyst_min=int(g.get("gate1_min", 40)),
        gate1_5_combined_min=int(g.get("gate1_5_min", 50)),
        gate2_avg_threshold=int(g.get("gate2_min", 45)),
        deterministic_avg_min=int(g.get("gate2_min", 45)),
        final_score_min=score_th,
    )
