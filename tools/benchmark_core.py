#!/usr/bin/env python3
"""Local, non-gating benchmarks for rollout and terminal boundedness."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path
from typing import Callable, TypeVar

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from codex.rollout import RolloutReader  # noqa: E402
from codex.terminal import (  # noqa: E402
    MAX_GLOBAL_TERMINAL_BYTES,
    MAX_TERMINALS_PER_SESSION,
    TerminalStore,
    TerminalUpdate,
)
from models import TerminalCapability  # noqa: E402
from models import NormalizedEvent  # noqa: E402
from state_machine import SessionStateMachine  # noqa: E402

T = TypeVar("T")


def measured(callable_: Callable[[], T]) -> tuple[float, int, T]:
    tracemalloc.start()
    started = time.perf_counter()
    result = callable_()
    elapsed = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return elapsed, peak, result


def rollout_benchmark(root: Path, lines: int) -> dict[str, object]:
    path = root / f"rollout-{lines}.jsonl"
    record = json.dumps(
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "type": "response_item",
            "payload": {"type": "message", "role": "assistant", "content": []},
        },
        separators=(",", ":"),
    ) + "\n"
    with path.open("w", encoding="utf-8") as handle:
        for _ in range(lines):
            handle.write(record)

    reader = RolloutReader()
    elapsed, peak, result = measured(lambda: reader.read_with_activity(path))
    bytes_read = path.stat().st_size
    return {
        "lines": lines,
        "bytes": bytes_read,
        "seconds": elapsed,
        "mib_per_second": bytes_read / max(elapsed, 1e-9) / (1024 * 1024),
        "peak_mib": peak / (1024 * 1024),
        "events": len(result.events),
        "cursor_count": len(reader.cursors),
    }


def terminal_benchmark() -> dict[str, object]:
    store = TerminalStore()
    session_key = "benchmark-session"

    def populate() -> None:
        for terminal_index in range(MAX_TERMINALS_PER_SESSION):
            call_id = f"call-{terminal_index}"
            store.apply(
                session_key,
                (
                    TerminalUpdate(
                        source_id=f"start-{terminal_index}",
                        observed_at=float(terminal_index),
                        call_id=call_id,
                        command="benchmark",
                        status="running",
                        terminal_candidate=True,
                        capability=TerminalCapability.POLL_TRANSCRIPT,
                        scope="benchmark",
                    ),
                ),
            )
            for chunk_index in range(4_100):
                store.apply(
                    session_key,
                    (
                        TerminalUpdate(
                            source_id=f"chunk-{terminal_index}-{chunk_index}",
                            observed_at=float(chunk_index + 1),
                            call_id=call_id,
                            status="running",
                            output="x" * 512,
                            capability=TerminalCapability.POLL_TRANSCRIPT,
                            scope="benchmark",
                        ),
                    ),
                )

    elapsed, peak, _ = measured(populate)
    summaries = store.summaries(session_key)
    retained = sum(item.retained_bytes for item in summaries)
    assert len(summaries) <= MAX_TERMINALS_PER_SESSION
    assert retained <= MAX_GLOBAL_TERMINAL_BYTES
    return {
        "seconds": elapsed,
        "peak_mib": peak / (1024 * 1024),
        "terminal_count": len(summaries),
        "retained_bytes": retained,
        "dropped_bytes": sum(item.dropped_bytes for item in summaries),
        "within_global_limit": retained <= MAX_GLOBAL_TERMINAL_BYTES,
    }


def state_churn_benchmark() -> dict[str, object]:
    machine = SessionStateMachine(lookback_seconds=3_600)
    active_key = "active-session"
    event_count = 24 * 60 * 60
    retired_count = 1_024

    def populate() -> None:
        for hour in range(24):
            start = hour * 3_600
            machine.ingest(
                active_key,
                [
                    NormalizedEvent(
                        float(index),
                        "MODEL_PROGRESS",
                        "benchmark",
                        source_id=f"active:{index}",
                    )
                    for index in range(start, start + 3_600)
                ],
            )
        for index in range(retired_count):
            key = f"retired-{index}"
            machine.ingest(
                key,
                [
                    NormalizedEvent(
                        float(index),
                        "TURN_COMPLETED",
                        "benchmark",
                        source_id=f"retired:{index}",
                    )
                ],
            )
        machine.prune({active_key}, now=float(event_count + 3_600))

    elapsed, peak, _ = measured(populate)
    retained_events = sum(len(events) for events in machine.events.values())
    seen_entries = sum(len(entries) for entries in machine.seen.values())
    return {
        "equivalent_hours": 24,
        "input_events": event_count + retired_count,
        "seconds": elapsed,
        "peak_mib": peak / (1024 * 1024),
        "session_cache_count": len(machine.events),
        "retained_events": retained_events,
        "seen_entries": seen_entries,
        "retired_sessions_pruned": all(
            not str(key).startswith("retired-") for key in machine.events
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--million",
        action="store_true",
        help="also run the 1,000,000-line rollout case",
    )
    args = parser.parse_args()
    sizes = [10_000, 100_000]
    if args.million:
        sizes.append(1_000_000)
    with tempfile.TemporaryDirectory(prefix="codexdeck-benchmark-") as directory:
        root = Path(directory)
        payload = {
            "rollout": [rollout_benchmark(root, lines) for lines in sizes],
            "terminal": terminal_benchmark(),
            "state_churn": state_churn_benchmark(),
        }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
