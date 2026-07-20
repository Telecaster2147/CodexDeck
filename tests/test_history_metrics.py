from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from history import HistoryStore  # noqa: E402
from models import (  # noqa: E402
    CodexPaths,
    CollectorHealth,
    CompactionSummary,
    FailureInfo,
    InstanceSnapshot,
    LifecycleState,
    MonitorSnapshot,
    NetworkEvidence,
    NetworkState,
    NormalizedEvent,
    ObservationPulse,
    ProcessIdentity,
    ProcessInfo,
    SessionHealth,
    SilenceAssessment,
    SilenceState,
    TokenUsageSummary,
    TurnSummary,
)
from presentation.metrics import render_prometheus  # noqa: E402


def make_snapshot(root: Path, generated_at: str = "2026-07-16T00:00:01+00:00") -> MonitorSnapshot:
    codex_db = root / "codex-state.sqlite"
    sqlite3.connect(codex_db).close()
    paths = CodexPaths(root, root, codex_db, root / "logs.sqlite", root / "index", root)
    process = ProcessInfo(
        ProcessIdentity(321, 7),
        1,
        "codex",
        10,
        1.0,
        "S",
        "wait",
        "codex",
        "session",
        instance_id='home"one',
        session_id="secret-session",
    )
    failure = FailureInfo("server_overloaded", "sensitive error")
    session = SessionHealth(
        'home"one',
        "secret-session",
        process,
        LifecycleState.GENERATING,
        network=NetworkEvidence(NetworkState.ACTIVE),
        alert="slow response",
        alert_level="warning",
        observation=ObservationPulse(
            last_semantic_at=1784159950.0,
            last_evidence_at=1784159999.0,
            last_evidence_source="network",
        ),
        silence=SilenceAssessment(SilenceState.QUIET_UNKNOWN, "quiet"),
        cumulative_token_usage=TokenUsageSummary(input_tokens=10, output_tokens=5, total_tokens=15),
        events=[
            NormalizedEvent(1784160000.0, "TURN_FAILED", "failed", "secret", failure=failure),
            NormalizedEvent(1784160001.0, "MODEL_PROGRESS", "token"),
            NormalizedEvent(1784160002.0, "RECOVERED", "recovered"),
        ],
    )
    instance = InstanceSnapshot(
        'home"one',
        paths,
        str(root),
        str(root),
        "environment",
        collector_health=[CollectorHealth('state_db:home"one')],
        sessions=[session],
    )
    return MonitorSnapshot(generated_at, 2.0, [instance], collection_duration_seconds=0.125)


class HistoryTests(unittest.TestCase):
    def test_window_stats_use_explicit_windows_samples_and_percentiles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = make_snapshot(root)
            snapshot.sessions[0].turns = [
                TurnSummary(
                    f"turn-{index}",
                    started_at=1784160000.0 + index,
                    completed_at=1784160001.0 + index,
                    status="failed" if index == 3 else "completed",
                    time_to_first_token_seconds=float(index + 1),
                    tool_duration_seconds=float((index + 1) * 2),
                    recovery_duration_seconds=4.0 if index == 2 else None,
                )
                for index in range(4)
            ]
            snapshot.sessions[0].events = [
                NormalizedEvent(1784160000.0, "RECONNECTING", "retry", source_id="r1"),
                NormalizedEvent(
                    1784160001.0, "COMPACT_COMPLETED", "compact", source_id="c1"
                ),
            ]
            snapshot.sessions[0].compactions = [
                CompactionSummary(
                    operation_id="compact-operation",
                    status="completed",
                    started_at=1784160001.0,
                    completed_at=1784160006.0,
                    trigger="manual",
                    retry_count=2,
                    context_tokens=240_000,
                    context_tokens_after=60_000,
                )
            ]
            with HistoryStore(root / "history.sqlite", max_days=None, max_bytes=None) as store:
                store.record_snapshot(snapshot)
                stats = store.window_stats(
                    now=1784160010.0,
                    instance_id=snapshot.instances[0].instance_id,
                )
                baseline = store.silence_baseline(
                    now=1784160010.0,
                    instance_id=snapshot.instances[0].instance_id,
                    workspace=snapshot.sessions[0].process.cwd,
                    phase=snapshot.sessions[0].lifecycle.value,
                    model=snapshot.sessions[0].process.model,
                    tool_category=snapshot.sessions[0].current_operation.category,
                )

            self.assertEqual([item.label for item in stats], ["15m", "1h", "24h"])
            for window in stats:
                self.assertEqual(window.turn_count, 4)
                self.assertEqual(window.failure_count, 1)
                self.assertEqual(window.failure_rate, 0.25)
                self.assertEqual(window.ttft_p50_seconds, 2.5)
                self.assertAlmostEqual(window.ttft_p95_seconds, 3.85)
                self.assertEqual(window.tool_p50_seconds, 5.0)
                self.assertEqual(window.reconnect_count, 1)
                self.assertEqual(window.fallback_count, 0)
                self.assertEqual(window.compact_count, 1)
                self.assertAlmostEqual(
                    window.compact_per_hour,
                    3600 / window.window_seconds,
                )
                self.assertEqual(window.recovery_average_seconds, 4.0)
                self.assertEqual(window.silence_samples, 1)
                self.assertEqual(window.compact_manual_count, 1)
                self.assertEqual(window.compact_retry_count, 2)
                self.assertEqual(window.compact_duration_p50_seconds, 5.0)
                self.assertEqual(window.compact_context_samples, 1)
                self.assertEqual(window.compact_context_before_average, 240_000)
                self.assertEqual(window.compact_context_after_average, 60_000)
            self.assertEqual(baseline, (1, 51.0, 51.0))

    def test_records_key_events_buckets_and_deduplicates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = make_snapshot(root)
            codex_db = snapshot.instances[0].paths.state_db
            before = os.stat(codex_db).st_mtime_ns
            with HistoryStore(root / "history.sqlite", max_days=None, max_bytes=None) as store:
                first = store.record_snapshot(snapshot)
                second = store.record_snapshot(snapshot)
                self.assertEqual(first.events_inserted, 2)
                self.assertEqual(second.events_inserted, 0)
                events = store.connection.execute(
                    "SELECT event_type, category FROM events ORDER BY timestamp"
                ).fetchall()
                bucket = store.connection.execute(
                    "SELECT bucket_start, samples, active_samples FROM session_buckets"
                ).fetchone()
                instance = store.connection.execute(
                    "SELECT samples, session_samples FROM instance_buckets"
                ).fetchone()
            self.assertEqual(events, [("TURN_FAILED", "server_overloaded"), ("RECOVERED", "recovery")])
            self.assertEqual(bucket, (1784160000, 2, 2))
            self.assertEqual(instance, (2, 2))
            self.assertEqual(os.stat(codex_db).st_mtime_ns, before)

    def test_unchanged_turn_and_compact_metrics_skip_redundant_upserts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = make_snapshot(root)
            snapshot.sessions[0].turns = [
                TurnSummary(
                    "turn-1",
                    started_at=1784160000.0,
                    status="running",
                )
            ]
            snapshot.sessions[0].compactions = [
                CompactionSummary(
                    operation_id="compact-1",
                    status="running",
                    started_at=1784160000.0,
                )
            ]
            statements: list[str] = []
            with HistoryStore(root / "history.sqlite", max_days=None, max_bytes=None) as store:
                store.connection.set_trace_callback(statements.append)
                store.record_snapshot(snapshot)
                store.record_snapshot(snapshot)
                turn_writes = sum("INSERT INTO turn_metrics" in item for item in statements)
                compact_writes = sum("INSERT INTO compact_metrics" in item for item in statements)

                snapshot.sessions[0].turns[0] = replace(
                    snapshot.sessions[0].turns[0], status="completed"
                )
                snapshot.sessions[0].compactions[0] = replace(
                    snapshot.sessions[0].compactions[0], status="completed"
                )
                store.record_snapshot(snapshot)

            self.assertEqual(turn_writes, 1)
            self.assertEqual(compact_writes, 1)
            self.assertEqual(
                sum("INSERT INTO turn_metrics" in item for item in statements),
                2,
            )
            self.assertEqual(
                sum("INSERT INTO compact_metrics" in item for item in statements),
                2,
            )

    def test_history_removes_transcript_bodies_from_event_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = make_snapshot(root)
            transcript = "HISTORY_TRANSCRIPT_SENTINEL_92014"
            snapshot.sessions[0].events = [
                NormalizedEvent(
                    1784160000.0,
                    "TOOL_COMPLETED",
                    "shell",
                    source_id="tool-output",
                    metadata={
                        "output": transcript,
                        "nested": {"stdout": transcript, "stderr": transcript},
                    },
                )
            ]
            with HistoryStore(root / "history.sqlite", max_days=None, max_bytes=None) as store:
                store.record_snapshot(snapshot)
                metadata = store.connection.execute(
                    "SELECT metadata_json FROM events WHERE event_type = 'TOOL_COMPLETED'"
                ).fetchone()[0]

            self.assertNotIn(transcript, metadata)
            self.assertEqual(
                json.loads(metadata),
                {"nested": {"stderr": "", "stdout": ""}, "output": ""},
            )

    def test_records_lifecycle_transition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = make_snapshot(root)
            with HistoryStore(root / "history.sqlite", max_days=None, max_bytes=None) as store:
                store.record_snapshot(snapshot)
                snapshot.sessions[0].lifecycle = LifecycleState.FAILED
                snapshot.generated_at = "2026-07-16T00:00:03+00:00"
                result = store.record_snapshot(snapshot)
                row = store.connection.execute(
                    "SELECT summary, metadata_json FROM events "
                    "WHERE event_type = 'LIFECYCLE_TRANSITION'"
                ).fetchone()
            self.assertEqual(result.events_inserted, 1)
            self.assertEqual(row[0], "GENERATING -> FAILED")
            self.assertIn('"to": "FAILED"', row[1])

    def test_age_pruning_removes_old_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = make_snapshot(root, "2026-07-01T00:00:01+00:00")
            old.sessions[0].events = [NormalizedEvent(1782864000.0, "WARNING", "old")]
            current = make_snapshot(root, "2026-07-16T00:00:01+00:00")
            with HistoryStore(root / "history.sqlite", max_days=7, max_bytes=None) as store:
                store.record_snapshot(old)
                store.record_snapshot(current)
                timestamps = store.connection.execute(
                    "SELECT timestamp FROM events ORDER BY timestamp"
                ).fetchall()
                buckets = store.connection.execute(
                    "SELECT bucket_start FROM session_buckets ORDER BY bucket_start"
                ).fetchall()
            self.assertTrue(all(row[0] >= 1784160000 for row in timestamps))
            self.assertEqual(buckets, [(1784160000,)])

    def test_size_pruning_keeps_database_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = make_snapshot(root)
            with HistoryStore(root / "history.sqlite", max_days=None, max_bytes=73728) as store:
                for index in range(80):
                    event_time = 1784160000.0 + index
                    snapshot.generated_at = f"2026-07-16T00:01:{index % 60:02d}+00:00"
                    snapshot.sessions[0].events = [
                        NormalizedEvent(
                            event_time,
                            "WARNING",
                            f"warning-{index}",
                            "x" * 1024,
                            source_id=str(index),
                        )
                    ]
                    store.record_snapshot(snapshot)
                event_count = store.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                size = (root / "history.sqlite").stat().st_size
            self.assertLess(event_count, 80)
            self.assertLessEqual(size, 73728)


class MetricsTests(unittest.TestCase):
    def test_prometheus_output_aggregates_and_has_only_low_cardinality_labels(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot = make_snapshot(Path(directory))
            snapshot.sessions[0].compactions = [
                CompactionSummary(
                    operation_id="compact-operation",
                    status="completed",
                    started_at=1.0,
                    completed_at=3.0,
                    trigger="manual",
                )
            ]
            output = render_prometheus(snapshot)
        self.assertIn('codexnet_sessions{instance="home\\"one",state="GENERATING"} 1', output)
        self.assertIn('codexnet_network_sessions{instance="home\\"one",state="ACTIVE"} 1', output)
        self.assertIn('codexnet_alerts{category="warning",instance="home\\"one"} 1', output)
        self.assertIn('codexnet_snapshot_events{event_type="TURN_FAILED",instance="home\\"one"} 1', output)
        self.assertIn('codexnet_tokens{category="total",instance="home\\"one"} 15', output)
        self.assertIn(
            'codexnet_collector_healthy{category="state_db",instance="home\\"one"} 1',
            output,
        )
        self.assertIn(
            'codexnet_silence_sessions{instance="home\\"one",state="QUIET_UNKNOWN"} 1',
            output,
        )
        self.assertIn(
            'codexnet_compact_total{instance="home\\"one",status="completed",trigger="manual"} 1',
            output,
        )
        self.assertNotIn("state_db:home", output)
        for forbidden in ("secret-session", "321", "sensitive error", "peer="):
            self.assertNotIn(forbidden, output)
        label_names = {
            item.split("=", 1)[0]
            for line in output.splitlines()
            if "{" in line
            for item in line.split("{", 1)[1].split("}", 1)[0].split(",")
        }
        self.assertLessEqual(
            label_names,
            {"instance", "state", "category", "event_type", "trigger", "status"},
        )


if __name__ == "__main__":
    unittest.main()
