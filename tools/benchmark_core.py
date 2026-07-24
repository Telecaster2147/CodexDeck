#!/usr/bin/env python3
"""Local, non-gating benchmarks for rollout and terminal boundedness."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import tracemalloc
from pathlib import Path
from typing import Callable, TypeVar

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from codex.rollout import BoundedFamilyCounter, RolloutReader  # noqa: E402
from codex.processes import ProcessDiscovery  # noqa: E402
from codex.terminal import (  # noqa: E402
    MAX_GLOBAL_TERMINAL_BYTES,
    MAX_TERMINAL_ALIASES_PER_TERMINAL,
    MAX_TERMINAL_SOURCE_IDS_PER_SCOPE,
    MAX_TERMINALS_PER_SESSION,
    TerminalStore,
    TerminalUpdate,
)
from network.sockets import SocketCollector  # noqa: E402
from models import TerminalCapability  # noqa: E402
from models import NormalizedEvent  # noqa: E402
from state_machine import SessionStateMachine  # noqa: E402
from utils import CommandError  # noqa: E402

T = TypeVar("T")


def measured(callable_: Callable[[], T], *, trace_memory: bool) -> tuple[float, int | None, T]:
    if trace_memory:
        tracemalloc.start()
    started = time.perf_counter()
    result = callable_()
    elapsed = time.perf_counter() - started
    peak = None
    if trace_memory:
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    return elapsed, peak, result


def _rollout_record(index: int) -> str:
    return json.dumps(
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "type": "event_msg",
            "payload": {"type": "turn_started", "turn_id": f"TURN_{index}"},
        },
        separators=(",", ":"),
    ) + "\n"


def _write_rollout(path: Path, lines: int, *, start: int = 0) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for index in range(start, start + lines):
            handle.write(_rollout_record(index))


def _append_rollout(path: Path, lines: int, *, start: int) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for index in range(start, start + lines):
            handle.write(_rollout_record(index))


def _consume(reader: RolloutReader, path: Path) -> dict[str, int | bool]:
    machine = SessionStateMachine(lookback_seconds=3_600)
    totals = {
        "actual_bytes_read": 0,
        "parsed_records": 0,
        "ignored_records": 0,
        "normalized_events": 0,
        "ingress_ticks": 0,
    }
    while True:
        result = reader.read_with_activity(path)
        totals["actual_bytes_read"] += result.activity.bytes_read
        totals["parsed_records"] += result.activity.complete_record_count
        totals["ignored_records"] += result.activity.ignored_record_count
        totals["normalized_events"] += len(result.events)
        totals["ingress_ticks"] += 1
        machine.ingest("benchmark", result.events)
        if not result.activity.backlog_bytes:
            break
    return {
        **totals,
        "retained_events": len(machine.retained_events("benchmark")),
        "bootstrap_truncated": reader.has_truncated_context({str(path)}),
    }


def _measurement(
    name: str,
    source_bytes: int,
    run: Callable[[], dict[str, object]],
    *,
    deterministic: bool = True,
) -> dict[str, object]:
    elapsed, _, runtime = measured(run, trace_memory=False)
    traced_elapsed, peak, traced = measured(run, trace_memory=True)
    actual_bytes = int(runtime.get("actual_bytes_read", 0))
    repeatable = runtime == traced if deterministic else None
    return {
        "measurement": name,
        "source_bytes": source_bytes,
        **runtime,
        "read_amplification": actual_bytes / source_bytes if source_bytes else None,
        "runtime_seconds": elapsed,
        "actual_read_mib_per_second": actual_bytes / max(elapsed, 1e-9) / (1024 * 1024),
        "tracemalloc_seconds": traced_elapsed,
        "tracemalloc_peak_mib": (peak or 0) / (1024 * 1024),
        "tracemalloc_actual_bytes_read": int(traced.get("actual_bytes_read", 0)),
        "tracemalloc_parsed_records": int(traced.get("parsed_records", 0)),
        "repeatable_result": repeatable,
        "visible_consequence": {
            "snapshot_age_seconds": elapsed,
            "updates_omitted": 0,
        },
    }


def rollout_full_small_benchmark(root: Path, lines: int = 1_000) -> dict[str, object]:
    path = root / "rollout-full-small.jsonl"
    _write_rollout(path, lines)

    def run() -> dict[str, object]:
        return _consume(RolloutReader(), path)

    result = _measurement("rollout_full_small", path.stat().st_size, run)
    assert not result["bootstrap_truncated"]
    return result


def rollout_cold_tail_benchmark(root: Path, lines: int) -> dict[str, object]:
    path = root / f"rollout-cold-tail-{lines}.jsonl"
    _write_rollout(path, lines)

    def run() -> dict[str, object]:
        return _consume(RolloutReader(), path)

    result = _measurement("rollout_cold_start_tail", path.stat().st_size, run)
    return result


def rollout_append_benchmark(root: Path, append_lines: int = 200) -> dict[str, object]:
    base = root / "rollout-append-base.jsonl"
    _write_rollout(base, 1_000)
    append_payload_bytes = sum(
        len(_rollout_record(index).encode()) for index in range(1_000, 1_000 + append_lines)
    )

    def run() -> dict[str, object]:
        path = root / f"rollout-append-{time.perf_counter_ns()}.jsonl"
        path.write_bytes(base.read_bytes())
        reader = RolloutReader()
        _consume(reader, path)
        _append_rollout(path, append_lines, start=1_000)
        return _consume(reader, path)

    result = _measurement("rollout_incremental_append", append_payload_bytes, run)
    assert result["actual_bytes_read"] == append_payload_bytes
    return result


def rollout_copy_truncate_benchmark(root: Path) -> dict[str, object]:
    base = root / "rollout-copy-base.jsonl"
    replacement = root / "rollout-copy-replacement.jsonl"
    _write_rollout(base, 2_000)
    _write_rollout(replacement, 200, start=10_000)

    def run() -> dict[str, object]:
        path = root / f"rollout-copy-{time.perf_counter_ns()}.jsonl"
        path.write_bytes(base.read_bytes())
        reader = RolloutReader()
        _consume(reader, path)
        path.write_bytes(replacement.read_bytes())
        return _consume(reader, path)

    result = _measurement("rollout_copy_truncate", replacement.stat().st_size, run)
    assert result["actual_bytes_read"] == replacement.stat().st_size
    return result


def multi_rollout_burst_benchmark(root: Path, count: int = 8) -> dict[str, object]:
    paths = [root / f"burst-{index}.jsonl" for index in range(count)]
    for path in paths:
        _write_rollout(path, 200)

    def run() -> dict[str, object]:
        reader = RolloutReader()
        actual_bytes = 0
        records = 0
        for path in paths:
            result = _consume(reader, path)
            actual_bytes += int(result["actual_bytes_read"])
            records += int(result["parsed_records"])
        return {
            "actual_bytes_read": actual_bytes,
            "parsed_records": records,
            "rollout_count": count,
        }

    return _measurement("multi_rollout_burst", sum(path.stat().st_size for path in paths), run)


def sqlite_benchmark(root: Path) -> dict[str, object]:
    path = root / "benchmark.sqlite"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE records(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
    connection.executemany(
        "INSERT INTO records(value) VALUES (?)",
        ((f"record-{index}",) for index in range(10_000)),
    )
    connection.commit()
    connection.close()

    def run() -> dict[str, object]:
        read = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        rows = read.execute("SELECT id, value FROM records ORDER BY id DESC LIMIT 500").fetchall()
        read.close()
        return {"rows_read": len(rows), "actual_bytes_read": sum(len(row[1]) for row in rows)}

    return _measurement("sqlite_bounded_page", path.stat().st_size, run)


def host_command_benchmark() -> dict[str, object]:
    def run() -> dict[str, object]:
        process_error = ""
        try:
            process = ProcessDiscovery().discover(selected_pids={os.getpid()}).command_result
        except CommandError as error:
            process = error.result
            process_error = error.reason
        sockets = SocketCollector()
        socket_error = ""
        try:
            sockets.snapshot({os.getpid()})
            socket = sockets.last_command_result
        except CommandError as error:
            socket = error.result
            socket_error = error.reason
        return {
            "ps_bytes_read": process.stdout_bytes_read if process else 0,
            "ps_records_retained": process.records_retained if process else 0,
            "ss_bytes_read": socket.stdout_bytes_read if socket else 0,
            "ss_records_retained": socket.records_retained if socket else 0,
            "actual_bytes_read": (process.stdout_bytes_read if process else 0)
            + (socket.stdout_bytes_read if socket else 0),
            "host_status": "degraded" if process_error or socket_error else "complete",
            "ps_error_code": process_error,
            "ss_error_code": socket_error,
        }

    return _measurement("host_ps_ss_bounded", 0, run, deterministic=False)


def filesystem_contention_benchmark(root: Path) -> dict[str, object]:
    path = root / "rollout-contention.jsonl"
    path.touch()

    def run() -> dict[str, object]:
        local = root / f"contention-{time.perf_counter_ns()}.jsonl"
        local.touch()
        done = threading.Event()

        def writer() -> None:
            for index in range(2_000):
                _append_rollout(local, 1, start=index)
            done.set()

        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        reader = RolloutReader()
        actual_bytes = 0
        records = 0
        while True:
            result = reader.read_with_activity(local)
            actual_bytes += result.activity.bytes_read
            records += result.activity.complete_record_count
            if done.is_set() and not result.activity.backlog_bytes and not result.activity.changed:
                break
            if not result.activity.changed:
                time.sleep(0.0005)
        thread.join()
        final = _consume(reader, local)
        actual_bytes += int(final["actual_bytes_read"])
        records += int(final["parsed_records"])
        return {"actual_bytes_read": actual_bytes, "parsed_records": records}

    return _measurement("filesystem_append_contention", 0, run)


def terminal_benchmark() -> dict[str, object]:
    def run() -> dict[str, object]:
        store = TerminalStore()
        session_key = "benchmark-session"
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
                        scope=f"benchmark-{terminal_index}",
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
                            scope=f"benchmark-{terminal_index}",
                        ),
                    ),
                )
        summaries = store.summaries(session_key)
        retained = sum(item.retained_bytes for item in summaries)
        private = store.private_state_summary(session_key)
        assert len(summaries) <= MAX_TERMINALS_PER_SESSION
        assert retained <= MAX_GLOBAL_TERMINAL_BYTES
        return {
            "terminal_count": len(summaries),
            "retained_bytes": retained,
            "dropped_bytes": sum(item.dropped_bytes for item in summaries),
            "within_global_limit": retained <= MAX_GLOBAL_TERMINAL_BYTES,
            "private_state": private,
            "private_state_evictions": store.association_summary(
                session_key
            ).private_state_evictions,
        }

    elapsed, _, result = measured(run, trace_memory=False)
    traced_elapsed, peak, traced = measured(run, trace_memory=True)
    assert result == traced
    return {
        **result,
        "runtime_seconds": elapsed,
        "tracemalloc_seconds": traced_elapsed,
        "tracemalloc_peak_mib": (peak or 0) / (1024 * 1024),
    }


def terminal_identity_churn_benchmark() -> dict[str, object]:
    def run() -> dict[str, object]:
        store = TerminalStore()
        session_key = "identity-churn-session"
        for index in range(MAX_TERMINAL_SOURCE_IDS_PER_SCOPE + 1_024):
            store.apply(
                session_key,
                (
                    TerminalUpdate(
                        source_id=f"source-{index}",
                        observed_at=float(index),
                        call_id=f"call-{index}",
                        process_id="PROCESS_ID",
                        terminal_candidate=True,
                        scope="churn-scope",
                    ),
                    ),
                )
        private = store.private_state_summary(session_key)
        association = store.association_summary(session_key)
        assert private["source_entries"] <= MAX_TERMINAL_SOURCE_IDS_PER_SCOPE
        assert private["call_entries"] <= MAX_TERMINAL_ALIASES_PER_TERMINAL
        return {
            "input_source_ids": MAX_TERMINAL_SOURCE_IDS_PER_SCOPE + 1_024,
            "private_state": private,
            "evictions": association.private_state_evictions,
            "dropped": association.private_state_dropped,
            "reasons": dict(association.private_state_reasons),
        }

    elapsed, _, result = measured(run, trace_memory=False)
    traced_elapsed, peak, traced = measured(run, trace_memory=True)
    assert result == traced
    return {
        **result,
        "runtime_seconds": elapsed,
        "tracemalloc_seconds": traced_elapsed,
        "tracemalloc_peak_mib": (peak or 0) / (1024 * 1024),
    }


def protocol_family_churn_benchmark() -> dict[str, object]:
    def run() -> dict[str, object]:
        counter = BoundedFamilyCounter()
        for index in range(100_000):
            counter.add(f"family-{index}")
        snapshot = counter.snapshot()
        return {
            "input_families": 100_000,
            "retained_candidates": len(counter.counts),
            "reported_total": sum(snapshot.values()),
            "other": snapshot.get("__other__", 0),
            "dropped_family_count": counter.dropped_family_count,
        }

    elapsed, _, result = measured(run, trace_memory=False)
    traced_elapsed, peak, traced = measured(run, trace_memory=True)
    assert result == traced
    return {
        **result,
        "runtime_seconds": elapsed,
        "tracemalloc_seconds": traced_elapsed,
        "tracemalloc_peak_mib": (peak or 0) / (1024 * 1024),
    }


def state_churn_benchmark() -> dict[str, object]:
    active_key = "active-session"
    event_count = 24 * 60 * 60
    retired_count = 1_024

    def run() -> dict[str, object]:
        machine = SessionStateMachine(lookback_seconds=3_600)
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
        retained_events = sum(len(events) for events in machine.events.values())
        seen_entries = sum(len(entries) for entries in machine.seen.values())
        dedupe_capacity_bytes = sum(entries.bit_count // 8 for entries in machine.seen.values())
        clock_entries = sum(len(entries) for entries in machine.clock_state.values())
        return {
            "equivalent_hours": 24,
            "input_events": event_count + retired_count,
            "session_cache_count": len(machine.events),
            "retained_events": retained_events,
            "seen_entries": seen_entries,
            "dedupe_capacity_bytes": dedupe_capacity_bytes,
            "clock_domain_entries": clock_entries,
            "retired_sessions_pruned": all(
                not str(key).startswith("retired-") for key in machine.events
            ),
        }

    elapsed, _, result = measured(run, trace_memory=False)
    traced_elapsed, peak, traced = measured(run, trace_memory=True)
    assert result == traced
    return {
        **result,
        "runtime_seconds": elapsed,
        "tracemalloc_seconds": traced_elapsed,
        "tracemalloc_peak_mib": (peak or 0) / (1024 * 1024),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--million",
        action="store_true",
        help="also run the 1,000,000-line rollout case",
    )
    args = parser.parse_args()
    cold_lines = 1_000_000 if args.million else 100_000
    with tempfile.TemporaryDirectory(prefix="codexdeck-benchmark-") as directory:
        root = Path(directory)
        payload = {
            "rollout": [
                rollout_full_small_benchmark(root),
                rollout_cold_tail_benchmark(root, cold_lines),
                rollout_append_benchmark(root),
                rollout_copy_truncate_benchmark(root),
                multi_rollout_burst_benchmark(root),
            ],
            "sqlite": sqlite_benchmark(root),
            "host_commands": host_command_benchmark(),
            "filesystem_contention": filesystem_contention_benchmark(root),
            "terminal": terminal_benchmark(),
            "terminal_identity_churn": terminal_identity_churn_benchmark(),
            "protocol_family_churn": protocol_family_churn_benchmark(),
            "state_churn": state_churn_benchmark(),
        }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
