"""Replay saved API responses through the rule engine.

Usage:
    python scripts/replay.py tests/fixtures/flow_alerts_sample.json

Prints which alerts would have been flagged and their scores.
Useful for tuning config/rules.yaml without burning API calls.
"""
import argparse
import json
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "src"))

from scanner.models.flow_alert import FlowAlert
from scanner.rules.engine import RuleEngine
import yaml


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("fixture", type=str, help="Path to flow_alerts JSON file")
    parser.add_argument("--config", type=str, default=None, help="Path to rules.yaml")
    args = parser.parse_args()

    fixture_path = Path(args.fixture)
    if not fixture_path.exists():
        print(f"File not found: {fixture_path}")
        return 1

    config_path = Path(args.config) if args.config else project_root / "config" / "rules.yaml"
    config = yaml.safe_load(config_path.read_text())

    raw = json.loads(fixture_path.read_text())
    data = raw.get("data", raw) if isinstance(raw, dict) else raw
    if not isinstance(data, list):
        data = [data]

    alerts = []
    for item in data:
        try:
            alerts.append(FlowAlert.model_validate(item))
        except Exception as e:
            print(f"Skipped: {e}")

    engine = RuleEngine(config)
    candidates = engine.evaluate_batch(alerts)

    print(f"Alerts: {len(alerts)}, Candidates: {len(candidates)}")
    for c in candidates:
        print(f"  {c.ticker} {c.direction} — score {c.confluence_score:.1f} — {[s.rule_name for s in c.signals]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
