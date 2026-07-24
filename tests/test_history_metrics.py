from __future__ import annotations

import json
import os
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import unittest
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from history import AsyncHistoryWriter, HistoryStore, IdentityCollisionError  # noqa: E402
from models import (  # noqa: E402
    AttentionRequest,
    AttentionState,
    CodexPaths,
    CollectorHealth,
    CompactionSummary,
    FailureInfo,
    InstanceSnapshot,
    InstanceIdentity,
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
    def test_history_maps_canonical_identity_and_rejects_surrogate_collision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = make_snapshot(root)
            first_identity = InstanceIdentity(
                Path("/CODEX_HOME_A"), Path("/SQLITE_HOME_A"), "COLLISION"
            )
            first.instances[0].instance_id = "COLLISION"
            first.instances[0].identity = first_identity
            for session in first.sessions:
                session.instance_id = "COLLISION"

            with HistoryStore(root / "history.sqlite", max_days=None, max_bytes=None) as store:
                store.record_snapshot(first)
                schema = store.connection.execute(
                    "SELECT value FROM history_meta WHERE key = 'schema_version'"
                ).fetchone()
                mapping = store.connection.execute(
                    """
                    SELECT storage_id, codex_home, sqlite_home, legacy_storage_id
                    FROM instance_identities
                    """
                ).fetchone()
                self.assertEqual(schema, ("5",))
                self.assertEqual(
                    mapping,
                    (
                        "COLLISION",
                        "/CODEX_HOME_A",
                        "/SQLITE_HOME_A",
                        first_identity.legacy_storage_key,
                    ),
                )

                second = make_snapshot(root)
                second.instances[0].instance_id = "COLLISION"
                second.instances[0].identity = InstanceIdentity(
                    Path("/CODEX_HOME_B"), Path("/SQLITE_HOME_B"), "COLLISION"
                )
                with self.assertRaisesRegex(IdentityCollisionError, "instance_storage_id_collision"):
                    store.record_snapshot(second)
                count = store.connection.execute(
                    "SELECT COUNT(*) FROM instance_identities"
                ).fetchone()
                self.assertEqual(count, (1,))

    def test_history_file_is_private_and_rejects_symlink_or_fifo(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "history.sqlite"
            database.touch(mode=0o644)
            with HistoryStore(database, max_days=None, max_bytes=None):
                self.assertEqual(stat.S_IMODE(database.stat().st_mode), 0o600)

            target = root / "target.sqlite"
            target.touch()
            symlink = root / "symlink.sqlite"
            symlink.symlink_to(target)
            with self.assertRaises(RuntimeError):
                HistoryStore(symlink, max_days=None, max_bytes=None)

            fifo = root / "history.fifo"
            os.mkfifo(fifo)
            with self.assertRaises(RuntimeError):
                HistoryStore(fifo, max_days=None, max_bytes=None)

            with self.assertRaises(RuntimeError):
                HistoryStore(Path("/dev/null"), max_days=None, max_bytes=None)

    def test_history_private_storage_handles_umask_reopen_parent_and_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "history.sqlite"
            previous_umask = os.umask(0o022)
            try:
                with HistoryStore(database, max_days=None, max_bytes=None):
                    self.assertEqual(stat.S_IMODE(database.stat().st_mode), 0o600)
            finally:
                os.umask(previous_umask)

            with HistoryStore(database, max_days=None, max_bytes=None):
                self.assertEqual(stat.S_IMODE(database.stat().st_mode), 0o600)

            unsafe = root / "unsafe"
            unsafe.mkdir()
            unsafe.chmod(0o777)
            with self.assertRaisesRegex(RuntimeError, "group/world"):
                HistoryStore(unsafe / "history.sqlite", max_days=None, max_bytes=None)

            with patch("utils.os.getuid", return_value=os.getuid() + 1):
                with self.assertRaisesRegex(RuntimeError, "不属于当前用户"):
                    HistoryStore(root / "other-owner.sqlite", max_days=None, max_bytes=None)

            replacement = root / "replacement.sqlite"
            real_connect = sqlite3.connect

            def replace_before_connect(path: object, *args: object, **kwargs: object):
                candidate = Path(path)
                candidate.unlink()
                candidate.touch(mode=0o600)
                replacement.write_text(str(candidate), encoding="utf-8")
                return real_connect(path, *args, **kwargs)

            with patch("history.sqlite3.connect", side_effect=replace_before_connect):
                with self.assertRaisesRegex(RuntimeError, "路径在打开后被替换"):
                    HistoryStore(root / "replaced.sqlite", max_days=None, max_bytes=None)

    def test_history_sqlite_sidecars_remain_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "history.sqlite"
            with HistoryStore(database, max_days=None, max_bytes=None) as store:
                store.connection.execute("PRAGMA journal_mode=WAL")
                store.connection.execute(
                    "INSERT OR REPLACE INTO history_meta(key, value) VALUES ('wal-test', '1')"
                )
                store.connection.commit()
                store._verify_private_storage()
                for suffix in ("-wal", "-shm"):
                    sidecar = Path(f"{database}{suffix}")
                    self.assertTrue(sidecar.exists(), suffix)
                    self.assertEqual(stat.S_IMODE(sidecar.stat().st_mode), 0o600)

                store.connection.execute("PRAGMA journal_mode=DELETE")
                store.connection.execute("BEGIN IMMEDIATE")
                store.connection.execute(
                    "UPDATE history_meta SET value = '2' WHERE key = 'wal-test'"
                )
                journal = Path(f"{database}-journal")
                self.assertTrue(journal.exists())
                store._verify_private_storage()
                self.assertEqual(stat.S_IMODE(journal.stat().st_mode), 0o600)
                store.connection.rollback()

    def test_operational_durations_track_attention_blindness_and_protocol_quality(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = make_snapshot(root)
            session = snapshot.sessions[0]
            session.attention_request = AttentionRequest(AttentionState.APPROVAL)
            session.silence = SilenceAssessment(
                SilenceState.WAITING_UPSTREAM, "waiting for upstream"
            )
            snapshot.instances[0].unknown_event_types = {"future_event": 1}

            with HistoryStore(root / "history.sqlite", max_days=None, max_bytes=None) as store:
                store.record_snapshot(snapshot)
                session.attention_request = None
                session.silence = SilenceAssessment(
                    SilenceState.OBSERVER_BLIND, "rollout unavailable"
                )
                snapshot.generated_at = "2026-07-16T00:00:03+00:00"
                store.record_snapshot(snapshot)
                stats = store.window_stats(
                    now=1784160010.0,
                    instance_id=snapshot.instances[0].instance_id,
                )[0]

            self.assertEqual(dict(stats.phase_duration_seconds), {"GENERATING": 4.0})
            self.assertEqual(stats.waiting_upstream_seconds, 2.0)
            self.assertEqual(stats.attention_wait_seconds, 2.0)
            self.assertEqual(stats.observer_blind_samples, 1)
            self.assertEqual(stats.observer_blind_frequency, 0.5)
            self.assertEqual(stats.protocol_degraded_samples, 2)
            self.assertEqual(stats.protocol_degraded_frequency, 1.0)

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
                NormalizedEvent(1784160001.0, "COMPACT_COMPLETED", "compact", source_id="c1"),
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
            self.assertEqual(
                events, [("TURN_FAILED", "server_overloaded"), ("RECOVERED", "recovery")]
            )
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
                {
                    "adjudicated_at": 1784160000.0,
                    "clock_domain": "source_wall_clock",
                    "clock_reason": "",
                    "clock_trust": "high",
                    "clock_uncertain": False,
                    "observed_at": None,
                    "privacy_projection": "structured_allowlist_v1",
                    "source_complete": True,
                    "source_confidence": "high",
                    "source_timestamp": 1784160000.0,
                    "text_redaction": "best_effort_known_formats",
                },
            )

    def test_history_omits_arbitrary_event_text_and_unknown_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = make_snapshot(root)
            secret = "ARBITRARY_HISTORY_TEXT_SENTINEL"
            snapshot.sessions[0].events = [
                NormalizedEvent(
                    1784160000.0,
                    "UNPARSED_PAYLOAD",
                    secret,
                    secret,
                    metadata={"diagnostic_payload": secret, "phase": "future"},
                )
            ]
            with HistoryStore(root / "history.sqlite", max_days=None, max_bytes=None) as store:
                store.record_snapshot(snapshot)
                row = store.connection.execute(
                    "SELECT summary, detail, metadata_json FROM events"
                ).fetchone()

            self.assertEqual(row[0], "UNPARSED_PAYLOAD")
            self.assertEqual(row[1], "")
            self.assertNotIn(secret, row[2])
            self.assertEqual(json.loads(row[2])["phase"], "future")

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
                duration_buckets = store.connection.execute(
                    "SELECT DISTINCT bucket_start FROM operational_durations ORDER BY bucket_start"
                ).fetchall()
            self.assertTrue(all(row[0] >= 1784160000 for row in timestamps))
            self.assertEqual(buckets, [(1784160000,)])
            self.assertEqual(duration_buckets, [(1784160000,)])

    def test_size_pruning_keeps_database_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = make_snapshot(root)
            with HistoryStore(root / "history.sqlite", max_days=None, max_bytes=65536) as store:
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
            self.assertLessEqual(size, 65536)


class AsyncHistoryWriterTests(unittest.TestCase):
    def test_slow_store_does_not_block_enqueue_and_queue_is_bounded(self) -> None:
        started = threading.Event()
        release = threading.Event()

        class SlowStore:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            def deadline(self, _seconds: float):
                return nullcontext()

            def record_snapshot(self, _snapshot, *, maintain: bool = True) -> None:
                started.set()
                release.wait(2.0)

            def window_stats(self, **_kwargs):
                return []

            def silence_baseline(self, **_kwargs):
                return (0, None, None)

            def maintenance_due(self, *, now: float) -> bool:
                return False

            def close(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as directory:
            writer = AsyncHistoryWriter(
                Path(directory) / "history.sqlite",
                queue_capacity=2,
                store_factory=SlowStore,
            )
            snapshot = make_snapshot(Path(directory))
            snapshot.sessions[0].alert = ""
            snapshot.sessions[0].alert_level = ""
            snapshot.sessions[0].events = []
            before = time.monotonic()
            writer.enqueue(snapshot)
            self.assertTrue(started.wait(0.5))
            for _ in range(8):
                writer.enqueue(snapshot)
            elapsed = time.monotonic() - before
            status = writer.status()
            self.assertLess(elapsed, 0.25)
            self.assertLessEqual(status.queue_depth, 2)
            self.assertGreater(status.coalesced_samples, 0)
            release.set()
            writer.close()

    def test_locked_database_failure_recovers_after_lock_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "history.sqlite"
            with HistoryStore(path, max_days=None, max_bytes=None):
                pass
            lock = sqlite3.connect(path, timeout=0.1)
            lock.execute("BEGIN EXCLUSIVE")
            writer = AsyncHistoryWriter(path, max_days=None, max_bytes=None)
            writer.enqueue(make_snapshot(root))
            deadline = time.monotonic() + 1.0
            while writer.status().consecutive_failures == 0 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertGreater(writer.status().consecutive_failures, 0)
            lock.rollback()
            lock.close()
            writer.enqueue(make_snapshot(root, "2026-07-16T00:00:03+00:00"))
            deadline = time.monotonic() + 1.0
            while writer.status().persisted_samples == 0 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertGreaterEqual(writer.status().persisted_samples, 1)
            self.assertEqual(writer.status().consecutive_failures, 0)
            writer.close()

    def test_io_error_is_reported_then_cleared_after_recovery(self) -> None:
        attempts = 0

        class RecoveringStore:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            def deadline(self, _seconds: float):
                return nullcontext()

            def record_snapshot(self, _snapshot, *, maintain: bool = True) -> None:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise OSError("disk full")

            def window_stats(self, **_kwargs):
                return []

            def silence_baseline(self, **_kwargs):
                return (0, None, None)

            def maintenance_due(self, *, now: float) -> bool:
                return False

            def close(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            writer = AsyncHistoryWriter(root / "history.sqlite", store_factory=RecoveringStore)
            writer.enqueue(make_snapshot(root))
            deadline = time.monotonic() + 1.0
            while writer.status().consecutive_failures == 0 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIn("disk full", writer.status().error)
            writer.enqueue(make_snapshot(root, "2026-07-16T00:00:03+00:00"))
            deadline = time.monotonic() + 1.0
            while writer.status().persisted_samples == 0 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(writer.status().error, "")
            writer.close()

    def test_two_writers_are_bounded_and_shared_path_policy_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "history.sqlite"
            first = AsyncHistoryWriter(path, max_days=None, max_bytes=None)
            second = AsyncHistoryWriter(path, max_days=None, max_bytes=None)
            first.enqueue(make_snapshot(root))
            second.enqueue(make_snapshot(root, "2026-07-16T00:00:03+00:00"))
            self.assertTrue(first.wait_until_idle(2.0))
            self.assertTrue(second.wait_until_idle(2.0))
            self.assertEqual(
                first.status().shared_path_policy,
                "unsupported_for_low_latency_writes",
            )
            first.close()
            second.close()

    def test_shutdown_flush_has_hard_timeout(self) -> None:
        started = threading.Event()
        release = threading.Event()

        class BlockingStore:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            def deadline(self, _seconds: float):
                return nullcontext()

            def record_snapshot(self, _snapshot, *, maintain: bool = True) -> None:
                started.set()
                release.wait(2.0)

            def window_stats(self, **_kwargs):
                return []

            def silence_baseline(self, **_kwargs):
                return (0, None, None)

            def maintenance_due(self, *, now: float) -> bool:
                return False

            def close(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            writer = AsyncHistoryWriter(root / "history.sqlite", store_factory=BlockingStore)
            writer.enqueue(make_snapshot(root))
            self.assertTrue(started.wait(0.5))
            before = time.monotonic()
            writer.close(flush_timeout_seconds=0.05)
            self.assertLess(time.monotonic() - before, 0.2)
            self.assertTrue(writer.status().shutdown_timed_out)
            release.set()


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
        self.assertIn('codexdeck_sessions{instance="home\\"one",state="GENERATING"} 1', output)
        self.assertIn('codexdeck_network_sessions{instance="home\\"one",state="ACTIVE"} 1', output)
        self.assertIn('codexdeck_alerts{category="warning",instance="home\\"one"} 1', output)
        self.assertIn(
            'codexdeck_snapshot_events{event_type="TURN_FAILED",instance="home\\"one"} 1', output
        )
        self.assertIn('codexdeck_tokens{category="total",instance="home\\"one"} 15', output)
        self.assertIn(
            'codexdeck_collector_healthy{category="state_db",instance="home\\"one"} 1',
            output,
        )
        self.assertIn(
            'codexdeck_silence_sessions{instance="home\\"one",state="QUIET_UNKNOWN"} 1',
            output,
        )
        self.assertIn(
            'codexdeck_compact_total{instance="home\\"one",status="completed",trigger="manual"} 1',
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
            {
                "instance",
                "state",
                "category",
                "event_type",
                "trigger",
                "status",
                "source",
                "stream",
                "disposition",
                "axis",
            },
        )


if __name__ == "__main__":
    unittest.main()
