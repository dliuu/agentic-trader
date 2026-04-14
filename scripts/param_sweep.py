"""Run replay + outcome measurement across YAML-driven parameter sweeps.

Usage:
    python scripts/param_sweep.py \\
      --data-dir data/backfill/ \\
      --base-config config/rules.yaml \\
      --output data/sweep_results/ \\
      --sweeps scripts/sweep_config.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import csv
import json
import sys
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

load_dotenv()

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "src"))


def set_nested(config: dict[str, Any], dotted_path: str, value: Any) -> dict[str, Any]:
    """Deep-copy ``config`` and set ``dotted_path`` (e.g. ``grader.score_threshold``)."""
    c = copy.deepcopy(config)
    keys = dotted_path.split(".")
    d: dict[str, Any] = c
    for key in keys[:-1]:
        if key not in d or not isinstance(d[key], dict):
            d[key] = {}
        d = d[key]
    d[keys[-1]] = value
    return c


def _safe_name(v: Any) -> str:
    s = str(v).replace("/", "_").replace(" ", "_")
    return f"value_{s}"


async def _run_one_value(
    *,
    base_config: dict[str, Any],
    data_dir: Path,
    value_dir: Path,
    parameter: str,
    value: Any,
) -> dict[str, Any]:
    from replay.measure import measure_outcomes_to_json
    from replay.runner import run_replay_pipeline

    cfg = set_nested(base_config, parameter, value)

    replay_out = value_dir
    replay_out.mkdir(parents=True, exist_ok=True)
    r = await run_replay_pipeline(
        data_dir=data_dir,
        output_dir=replay_out,
        config=cfg,
        mock_llm=True,
    )
    signals_created = int(r.get("signals_created", 0))
    outcomes_path = replay_out / "outcomes.json"
    try:
        rep = await measure_outcomes_to_json(
            replay_out / "replay.db",
            outcomes_path,
            tp_threshold=2.0,
            fn_threshold=5.0,
        )
    except Exception as e:
        return {
            "parameter_value": value,
            "signals_created": signals_created,
            "signals_actionable": 0,
            "precision": 0.0,
            "recall": 0.0,
            "f1": 0.0,
            "avg_tp_move": 0.0,
            "avg_days_to_actionable": 0.0,
            "error": str(e),
        }

    m = rep.get("metrics") or {}
    meta = rep.get("meta") or {}
    return {
        "parameter_value": value,
        "signals_created": meta.get("total_signals_created", signals_created),
        "signals_actionable": meta.get("signals_reached_actionable", 0),
        "precision": float(m.get("precision", 0.0)),
        "recall": float(m.get("recall", 0.0)),
        "f1": float(m.get("f1", 0.0)),
        "avg_tp_move": float(m.get("avg_tp_move_pct", 0.0)),
        "avg_days_to_actionable": float(m.get("avg_days_to_actionable", 0.0)),
        "error": "",
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description="Parameter sweep: replay + outcomes per value.")
    parser.add_argument("--data-dir", required=True, type=str)
    parser.add_argument("--base-config", required=True, type=str)
    parser.add_argument("--output", required=True, type=str)
    parser.add_argument("--sweeps", required=True, type=str, help="YAML sweep definitions")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    out_root = Path(args.output)
    out_root.mkdir(parents=True, exist_ok=True)

    base_path = Path(args.base_config)
    from shared.config import load_config

    base_config = load_config(base_path)
    sweep_doc = yaml.safe_load(Path(args.sweeps).read_text()) or {}
    sweeps = sweep_doc.get("sweeps") or []

    summary_sweeps: list[dict[str, Any]] = []
    recommended: dict[str, Any] = {}

    for sweep in sweeps:
        name = str(sweep.get("name", "unnamed"))
        param = str(sweep["parameter"])
        values = sweep.get("values") or []
        sweep_dir = out_root / name
        sweep_dir.mkdir(parents=True, exist_ok=True)
        rows: list[dict[str, Any]] = []

        best_f1 = -1.0
        best_f1_val: Any = None
        best_prec = -1.0
        best_prec_val: Any = None
        best_rec = -1.0
        best_rec_val: Any = None

        for val in values:
            vdir = sweep_dir / _safe_name(val)
            row = await _run_one_value(
                base_config=base_config,
                data_dir=data_dir,
                value_dir=vdir,
                parameter=param,
                value=val,
            )
            rows.append(row)
            f1 = row["f1"]
            if f1 > best_f1:
                best_f1 = f1
                best_f1_val = val
            if row["precision"] > best_prec:
                best_prec = row["precision"]
                best_prec_val = val
            if row["recall"] > best_rec:
                best_rec = row["recall"]
                best_rec_val = val

        if rows:
            fields = list(rows[0].keys())
            with (sweep_dir / "comparison.csv").open("w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=fields)
                w.writeheader()
                w.writerows(rows)

        summary_sweeps.append(
            {
                "name": name,
                "parameter": param,
                "best_f1_value": best_f1_val,
                "best_f1": round(best_f1, 4) if best_f1_val is not None else 0.0,
                "best_precision_value": best_prec_val,
                "best_precision": round(best_prec, 4) if best_prec_val is not None else 0.0,
                "best_recall_value": best_rec_val,
                "best_recall": round(best_rec, 4) if best_rec_val is not None else 0.0,
            }
        )
        if best_f1_val is not None:
            recommended[param] = best_f1_val

    (out_root / "sweep_summary.json").write_text(
        json.dumps({"sweeps": summary_sweeps, "recommended_config": recommended}, indent=2)
    )
    print(f"Sweep complete -> {out_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
