"""Integration: replay fixture day through full pipeline (no API keys)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


@pytest.fixture
def replay_rules_yaml(tmp_path: Path) -> Path:
    root = Path(__file__).resolve().parent.parent
    base_cfg = yaml.safe_load((root / "config" / "rules.yaml").read_text())
    base_cfg.setdefault("filters", {}).setdefault("expiry", {})["max_dte"] = 30
    base_cfg.setdefault("grader", {})["score_threshold"] = 40
    p = tmp_path / "rules.yaml"
    p.write_text(yaml.safe_dump(base_cfg))
    return p


def test_replay_pipeline_fixture_day(tmp_path: Path, replay_rules_yaml: Path) -> None:
    root = Path(__file__).resolve().parent.parent
    data_dir = root / "tests" / "fixtures" / "replay_integration"
    out_dir = tmp_path / "replay_results"
    env = {**os.environ, "FLOW_ALERT_DTE_ANCHOR_DATE": "2026-03-20"}
    cmd = [
        sys.executable,
        str(root / "scripts" / "replay.py"),
        "--data-dir",
        str(data_dir),
        "--output",
        str(out_dir),
        "--config",
        str(replay_rules_yaml),
        "--mock-llm",
    ]
    proc = subprocess.run(cmd, cwd=str(root), env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    assert (out_dir / "replay.db").is_file()
    assert (out_dir / "signals_summary.json").is_file()
    assert (out_dir / "replay_log.json").is_file()
    summary = json.loads((out_dir / "signals_summary.json").read_text())
    assert isinstance(summary, list)
    assert len(summary) >= 1
    assert summary[0]["ticker"] == "ACME"
