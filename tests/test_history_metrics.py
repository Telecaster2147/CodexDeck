from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from history import HistoryStore  # noqa: E402
from models import (  # noqa: E402
    CodexPaths,
    CollectorHealth,
    FailureInfo,
    InstanceSnapshot,
    LifecycleState,
    MonitorSnapshot,
    NetworkEvidence,
    NetworkState,
    NormalizedEvent,
    ProcessIdentity,
    ProcessInfo,
    SessionHealth,
    TokenUsageSummary,
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
            output = render_prometheus(make_snapshot(Path(directory)))
        self.assertIn('codexnet_sessions{instance="home\\"one",state="GENERATING"} 1', output)
        self.assertIn('codexnet_network_sessions{instance="home\\"one",state="ACTIVE"} 1', output)
        self.assertIn('codexnet_alerts{category="warning",instance="home\\"one"} 1', output)
        self.assertIn('codexnet_snapshot_events{event_type="TURN_FAILED",instance="home\\"one"} 1', output)
        self.assertIn('codexnet_tokens{category="total",instance="home\\"one"} 15', output)
        self.assertIn(
            'codexnet_collector_healthy{category="state_db",instance="home\\"one"} 1',
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
        self.assertLessEqual(label_names, {"instance", "state", "category", "event_type"})


if __name__ == "__main__":
    unittest.main()
