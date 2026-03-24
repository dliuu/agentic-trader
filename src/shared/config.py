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
    config["uw_api_token"] = os.environ.get("UW_API_TOKEN", "")
    config["anthropic_api_key"] = os.environ.get("ANTHROPIC_API_KEY", "")
    return config
