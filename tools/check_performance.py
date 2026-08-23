#!/usr/bin/env python3
"""Run deterministic performance scenarios and enforce generous regression ceilings."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from tools.benchmark_core import (  # noqa: E402
    fast_refresh_benchmark,
    multi_rollout_burst_benchmark,
    regular_file_tail_benchmark,
    session_scale_benchmark,
    terminal_identity_churn_benchmark,
)


def evaluate(
    results: dict[str, dict[str, object]],
    thresholds: dict[str, dict[str, float]],
) -> list[str]:
    failures = []
    for scenario, limits in thresholds.items():
        measurement = results[scenario]
        for metric, limit in limits.items():
            actual = float(measurement[metric])
            if actual > limit:
                failures.append(f"{scenario}.{metric}: {actual:.6f} > {limit:.6f}")
    rollout = results.get("rollout_burst")
    if rollout is not None:
        if rollout.get("repeatable_result") is not True:
            failures.append("rollout_burst.repeatable_result: semantic output changed between runs")
        visible = rollout.get("visible_consequence", {})
        if not isinstance(visible, dict) or visible.get("updates_omitted") != 0:
            failures.append("rollout_burst.visible_consequence: updates were omitted")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--thresholds",
        type=Path,
        default=PROJECT_ROOT / "tools" / "performance_thresholds.json",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    thresholds = json.loads(args.thresholds.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="codexdeck-performance-") as directory:
        root = Path(directory)
        results = {
            "full_20": session_scale_benchmark(root, 20),
            "full_50": session_scale_benchmark(root, 50),
            "fast_refresh": fast_refresh_benchmark(root),
            "regular_file_tail": regular_file_tail_benchmark(root),
            "rollout_burst": multi_rollout_burst_benchmark(root, 50),
            "terminal_identity_churn": terminal_identity_churn_benchmark(),
        }
    failures = evaluate(results, thresholds)
    payload = {
        "schema_version": 1,
        "status": "FAIL" if failures else "PASS",
        "thresholds": thresholds,
        "measurements": results,
        "failures": failures,
    }
    rendered = json.dumps(payload, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
