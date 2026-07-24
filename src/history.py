"""Optional local history storage, isolated from Codex-owned databases."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from collections import deque
from collections import defaultdict
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator

from models import (
    HistoryWindowStats,
    HistoryPersistenceStatus,
    LifecycleState,
    InstanceSnapshot,
    MonitorSnapshot,
    NormalizedEvent,
    SessionHealth,
)
from utils import PrivateFileHandle, open_private_regular_file


KEY_EVENT_TYPES = frozenset(
    {
        "STARTED",
        "TURN_STARTED",
        "REQUEST_SENT",
        "RESPONSE_STARTED",
        "TOOL_RUNNING",
        "TOOL_COMPLETED",
        "TURN_COMPLETED",
        "TURN_FAILED",
        "TURN_ABORTED",
        "OPERATION_ERROR",
        "SUBAGENT_ERROR",
        "WARNING",
        "SUSPECT",
        "RECONNECTING",
        "TRANSPORT_FALLBACK",
        "RECOVERED",
        "COMPACTING",
        "COMPACT_REQUESTED",
        "COMPACT_COMPLETED",
        "COMPACT_FAILED",
        "COMPACT_ABORTED",
        "PROCESS_EXITED",
        "PROCESS_RESUMED",
        "SESSION_EXITED",
        "ACTION_REQUIRED",
        "ACTION_RESOLVED",
        "UNPARSED_PAYLOAD",
    }
)


@dataclass(frozen=True)
class HistoryWriteResult:
    events_inserted: int
    session_buckets_updated: int
    instance_buckets_updated: int


class IdentityCollisionError(RuntimeError):
    pass


HISTORY_WINDOWS = (("15m", 900), ("1h", 3600), ("24h", 86400))
HISTORY_NUMERIC_METADATA_FIELDS = frozenset(
    {
        "context_tokens",
        "context_tokens_after",
        "context_window",
        "duration_seconds",
        "exit_code",
        "retry_count",
        "tool_count",
    }
)
HISTORY_ENUM_METADATA_FIELDS = frozenset(
    {"attention_state", "edge", "from", "phase", "semantic_scope", "status", "to", "trigger"}
)


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _timestamp(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return datetime.now(timezone.utc).timestamp()


def _event_category(event: NormalizedEvent) -> str:
    if event.failure and event.failure.category:
        return event.failure.category
    kind = event.kind.upper()
    if "FAIL" in kind or "ERROR" in kind:
        return "failure"
    if "RECOVER" in kind or "RECONNECT" in kind or "FALLBACK" in kind:
        return "recovery"
    if "COMPACT" in kind:
        return "compact"
    if kind == "WARNING":
        return "warning"
    return "state"


def _event_key(instance_id: str, session_id: str, event: NormalizedEvent) -> str:
    identity = json.dumps(
        [
            instance_id,
            session_id,
            event.timestamp,
            event.kind,
            event.source,
            event.source_id,
            event.turn_id,
            event.summary,
            event.detail,
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _history_event_projection(event: NormalizedEvent) -> tuple[str, str, dict[str, object]]:
    """Persist structured event facts and omit arbitrary user/tool text."""

    summary = event.kind.upper()
    if event.kind == "LIFECYCLE_TRANSITION":
        candidate = event.summary[:128]
        if all(character.isalnum() or character in " _->" for character in candidate):
            summary = candidate
    metadata: dict[str, object] = {
        "privacy_projection": "structured_allowlist_v1",
        "text_redaction": "best_effort_known_formats",
        "source_complete": event.complete,
        "source_confidence": event.confidence.value,
        "clock_domain": event.clock_domain,
        "clock_trust": event.clock_trust.value,
        "clock_uncertain": event.clock_uncertain,
        "clock_reason": event.clock_reason,
        "source_timestamp": event.source_timestamp,
        "observed_at": event.observed_at,
        "adjudicated_at": event.decision_timestamp,
    }
    for name in HISTORY_NUMERIC_METADATA_FIELDS:
        value = event.metadata.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            metadata[name] = value
    for name in HISTORY_ENUM_METADATA_FIELDS:
        value = event.metadata.get(name)
        if (
            isinstance(value, str)
            and 0 < len(value) <= 64
            and all(character.isalnum() or character in "_.:-" for character in value)
        ):
            metadata[name] = value
    if event.unparsed is not None:
        metadata["unparsed"] = {
            "source_type": event.unparsed.source_type[:128],
            "length": event.unparsed.length,
            "sha256": event.unparsed.sha256,
            "truncated": event.unparsed.truncated,
        }
    return summary, "", metadata


class HistoryStore:
    """Write bounded monitor history to a dedicated SQLite database.

    The caller chooses the path explicitly. The store never opens any path from
    ``snapshot.instances[*].paths`` and therefore cannot mutate Codex state.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        max_days: int | None = 7,
        max_bytes: int | None = 128 * 1024 * 1024,
        busy_timeout_seconds: float = 0.1,
    ) -> None:
        if max_days is not None and max_days < 1:
            raise ValueError("max_days must be positive or None")
        if max_bytes is not None and max_bytes < 1:
            raise ValueError("max_bytes must be positive or None")
        private_file = open_private_regular_file(path, os.O_CREAT | os.O_RDWR)
        self.path = private_file.path
        self._private_file: PrivateFileHandle | None = private_file
        self.max_days = max_days
        self.max_bytes = max_bytes
        was_empty = os.fstat(private_file.descriptor).st_size == 0
        try:
            self.connection = sqlite3.connect(self.path, timeout=busy_timeout_seconds)
            private_file.verify_path()
        except Exception:
            connection = getattr(self, "connection", None)
            if connection is not None:
                connection.close()
            private_file.close()
            self._private_file = None
            raise
        if was_empty:
            self.connection.execute("PRAGMA page_size = 1024")
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout_seconds * 1000)}")
        self._seen_event_keys: dict[tuple[str, str], set[str]] = {}
        self._turn_signatures: dict[str, tuple[object, ...]] = {}
        self._compact_signatures: dict[str, tuple[object, ...]] = {}
        self._last_prune_at: float | None = None
        self._initialize()
        self._verify_private_storage()

    @contextmanager
    def deadline(self, seconds: float) -> Iterator[None]:
        """Interrupt long SQLite VM work after a short best-effort deadline."""

        expires_at = time.monotonic() + max(0.001, seconds)
        self.connection.set_progress_handler(
            lambda: int(time.monotonic() >= expires_at),
            1000,
        )
        try:
            yield
        finally:
            self.connection.set_progress_handler(None, 0)

    def __enter__(self) -> "HistoryStore":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._verify_private_storage()
        finally:
            try:
                self.connection.close()
            finally:
                try:
                    self._verify_private_storage()
                finally:
                    if self._private_file is not None:
                        self._private_file.close()
                        self._private_file = None

    def _verify_private_storage(self) -> None:
        if self._private_file is None:
            return
        self._private_file.verify_path()
        for suffix in ("-journal", "-wal", "-shm"):
            sidecar_path = Path(f"{self.path}{suffix}")
            if not os.path.lexists(sidecar_path):
                continue
            sidecar = open_private_regular_file(
                sidecar_path,
                os.O_RDWR,
                create_parent=False,
            )
            sidecar.close()

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS history_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO history_meta(key, value) VALUES ('schema_version', '5')
            ON CONFLICT(key) DO UPDATE SET value = excluded.value;

            CREATE TABLE IF NOT EXISTS instance_identities (
                storage_id TEXT PRIMARY KEY,
                codex_home TEXT NOT NULL,
                sqlite_home TEXT NOT NULL,
                legacy_storage_id TEXT NOT NULL,
                first_seen_at REAL NOT NULL,
                UNIQUE(codex_home, sqlite_home)
            );
            CREATE INDEX IF NOT EXISTS instance_identities_legacy_idx
                ON instance_identities(legacy_storage_id);

            CREATE TABLE IF NOT EXISTS events (
                event_key TEXT PRIMARY KEY,
                timestamp REAL NOT NULL,
                instance_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                category TEXT NOT NULL,
                summary TEXT NOT NULL,
                detail TEXT NOT NULL,
                source TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_timestamp_idx ON events(timestamp);

            CREATE TABLE IF NOT EXISTS session_buckets (
                bucket_start INTEGER NOT NULL,
                instance_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                samples INTEGER NOT NULL,
                active_samples INTEGER NOT NULL,
                failure_samples INTEGER NOT NULL,
                alert_samples INTEGER NOT NULL,
                event_count INTEGER NOT NULL,
                lifecycle TEXT NOT NULL,
                recovery TEXT NOT NULL,
                network_state TEXT NOT NULL,
                token_total INTEGER,
                turn_count INTEGER NOT NULL,
                tool_count INTEGER NOT NULL,
                PRIMARY KEY(bucket_start, instance_id, session_id)
            );
            CREATE INDEX IF NOT EXISTS session_buckets_time_idx
                ON session_buckets(bucket_start);

            CREATE TABLE IF NOT EXISTS instance_buckets (
                bucket_start INTEGER NOT NULL,
                instance_id TEXT NOT NULL,
                samples INTEGER NOT NULL,
                session_samples INTEGER NOT NULL,
                active_session_samples INTEGER NOT NULL,
                failure_samples INTEGER NOT NULL,
                alert_samples INTEGER NOT NULL,
                stalled_samples INTEGER NOT NULL,
                diagnostic_samples INTEGER NOT NULL,
                collection_seconds_sum REAL NOT NULL,
                PRIMARY KEY(bucket_start, instance_id)
            );
            CREATE INDEX IF NOT EXISTS instance_buckets_time_idx
                ON instance_buckets(bucket_start);

            CREATE TABLE IF NOT EXISTS turn_metrics (
                metric_key TEXT PRIMARY KEY,
                timestamp REAL NOT NULL,
                instance_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                status TEXT NOT NULL,
                ttft_seconds REAL,
                tool_seconds REAL,
                recovery_seconds REAL
            );
            CREATE INDEX IF NOT EXISTS turn_metrics_time_idx ON turn_metrics(timestamp);
            CREATE INDEX IF NOT EXISTS turn_metrics_instance_time_idx
                ON turn_metrics(instance_id, timestamp);

            CREATE TABLE IF NOT EXISTS session_state (
                instance_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                lifecycle TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY(instance_id, session_id)
            );

            CREATE TABLE IF NOT EXISTS silence_samples (
                bucket_start INTEGER NOT NULL,
                instance_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                workspace TEXT NOT NULL,
                phase TEXT NOT NULL,
                model TEXT NOT NULL,
                tool_category TEXT NOT NULL,
                assessment TEXT NOT NULL,
                silence_seconds REAL NOT NULL,
                evidence_age_seconds REAL,
                PRIMARY KEY(bucket_start, instance_id, session_id)
            );
            CREATE INDEX IF NOT EXISTS silence_samples_time_idx
                ON silence_samples(bucket_start);

            CREATE TABLE IF NOT EXISTS compact_metrics (
                operation_key TEXT PRIMARY KEY,
                timestamp REAL NOT NULL,
                instance_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                trigger TEXT NOT NULL,
                status TEXT NOT NULL,
                duration_seconds REAL,
                retry_count INTEGER NOT NULL,
                context_before INTEGER,
                context_after INTEGER
            );
            CREATE INDEX IF NOT EXISTS compact_metrics_time_idx
                ON compact_metrics(timestamp);

            CREATE TABLE IF NOT EXISTS operational_durations (
                bucket_start INTEGER NOT NULL,
                instance_id TEXT NOT NULL,
                metric TEXT NOT NULL,
                seconds_sum REAL NOT NULL,
                occurrences INTEGER NOT NULL,
                PRIMARY KEY(bucket_start, instance_id, metric)
            );
            CREATE INDEX IF NOT EXISTS operational_durations_time_idx
                ON operational_durations(bucket_start);
            """
        )
        self.connection.commit()

    @staticmethod
    def _important_events(events: Iterable[NormalizedEvent]) -> Iterable[NormalizedEvent]:
        return (event for event in events if event.kind.upper() in KEY_EVENT_TYPES)

    def _register_instance_identity(self, instance: InstanceSnapshot, now: float) -> None:
        identity = instance.instance_identity
        storage_id = str(instance.instance_id)
        canonical = identity.canonical_key
        existing = self.connection.execute(
            "SELECT codex_home, sqlite_home FROM instance_identities WHERE storage_id = ?",
            (storage_id,),
        ).fetchone()
        if existing is not None and tuple(map(str, existing)) != canonical:
            raise IdentityCollisionError("instance_storage_id_collision")
        self.connection.execute(
            """
            INSERT OR IGNORE INTO instance_identities(
                storage_id, codex_home, sqlite_home, legacy_storage_id, first_seen_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                storage_id,
                canonical[0],
                canonical[1],
                identity.legacy_storage_key,
                now,
            ),
        )

    def _insert_event(self, instance_id: str, session_id: str, event: NormalizedEvent) -> int:
        event_key = _event_key(instance_id, session_id, event)
        cache_key = (instance_id, session_id)
        seen = self._seen_event_keys.setdefault(cache_key, set())
        if event_key in seen:
            return 0
        summary, detail, metadata = _history_event_projection(event)
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO events(
                event_key, timestamp, instance_id, session_id, event_type,
                category, summary, detail, source, turn_id, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_key,
                event.timestamp,
                instance_id,
                session_id,
                event.kind.upper(),
                _event_category(event),
                summary,
                detail,
                event.source,
                event.turn_id,
                json.dumps(
                    metadata,
                    ensure_ascii=True,
                    sort_keys=True,
                    default=str,
                ),
            ),
        )
        seen.add(event_key)
        if len(seen) > 1000:
            seen.clear()
            seen.add(event_key)
        return max(cursor.rowcount, 0)

    def _record_transition(self, session: SessionHealth, now: float) -> int:
        previous = self.connection.execute(
            "SELECT lifecycle FROM session_state WHERE instance_id = ? AND session_id = ?",
            (session.instance_id, session.session_id),
        ).fetchone()
        current = session.lifecycle.value
        self.connection.execute(
            """
            INSERT INTO session_state(instance_id, session_id, lifecycle, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(instance_id, session_id) DO UPDATE SET
                lifecycle = excluded.lifecycle,
                updated_at = excluded.updated_at
            """,
            (session.instance_id, session.session_id, current, now),
        )
        if previous is None or previous[0] == current:
            return 0
        old = str(previous[0])
        transition = NormalizedEvent(
            now,
            "LIFECYCLE_TRANSITION",
            f"{old} -> {current}",
            source="codexdeck-history",
            source_id=f"{old}:{current}:{now}",
            derived=True,
            metadata={"from": old, "to": current},
        )
        return self._insert_event(session.instance_id, session.session_id, transition)

    def _record_turn_metrics(
        self,
        session: SessionHealth,
        now: float,
        active_keys: set[str],
    ) -> None:
        for turn in session.turns:
            timestamp = turn.completed_at or turn.started_at or now
            identity = json.dumps(
                [session.instance_id, session.session_id, turn.turn_id, turn.started_at],
                separators=(",", ":"),
            )
            metric_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
            active_keys.add(metric_key)
            signature = (
                timestamp,
                turn.status,
                turn.time_to_first_token_seconds,
                turn.tool_duration_seconds,
                turn.recovery_duration_seconds,
            )
            if self._turn_signatures.get(metric_key) == signature:
                continue
            self.connection.execute(
                """
                INSERT INTO turn_metrics(
                    metric_key, timestamp, instance_id, session_id, turn_id, status,
                    ttft_seconds, tool_seconds, recovery_seconds
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(metric_key) DO UPDATE SET
                    timestamp = excluded.timestamp,
                    status = excluded.status,
                    ttft_seconds = excluded.ttft_seconds,
                    tool_seconds = excluded.tool_seconds,
                    recovery_seconds = excluded.recovery_seconds
                """,
                (
                    metric_key,
                    timestamp,
                    session.instance_id,
                    session.session_id,
                    turn.turn_id,
                    turn.status,
                    turn.time_to_first_token_seconds,
                    turn.tool_duration_seconds,
                    turn.recovery_duration_seconds,
                ),
            )
            self._turn_signatures[metric_key] = signature

    def _record_session_bucket(self, session: SessionHealth, bucket: int) -> None:
        active = int(
            session.lifecycle
            in {
                LifecycleState.STARTING,
                LifecycleState.WAITING_RESPONSE,
                LifecycleState.GENERATING,
                LifecycleState.RUNNING_TOOL,
                LifecycleState.COMPACTING,
            }
        )
        token_total = session.cumulative_token_usage or session.token_usage
        total = token_total.total_tokens if token_total else None
        self.connection.execute(
            """
            INSERT INTO session_buckets(
                bucket_start, instance_id, session_id, samples, active_samples,
                failure_samples, alert_samples, event_count, lifecycle, recovery,
                network_state, token_total, turn_count, tool_count
            ) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(bucket_start, instance_id, session_id) DO UPDATE SET
                samples = samples + 1,
                active_samples = active_samples + excluded.active_samples,
                failure_samples = failure_samples + excluded.failure_samples,
                alert_samples = alert_samples + excluded.alert_samples,
                event_count = excluded.event_count,
                lifecycle = excluded.lifecycle,
                recovery = excluded.recovery,
                network_state = excluded.network_state,
                token_total = excluded.token_total,
                turn_count = excluded.turn_count,
                tool_count = excluded.tool_count
            """,
            (
                bucket,
                session.instance_id,
                session.session_id,
                active,
                int(session.lifecycle == LifecycleState.FAILED),
                int(bool(session.alert)),
                len(session.events),
                session.lifecycle.value,
                session.recovery.value,
                session.network.state.value,
                total,
                len(session.turns),
                len(session.tool_executions),
            ),
        )

    def _record_silence_sample(
        self,
        session: SessionHealth,
        bucket: int,
        now: float,
    ) -> None:
        semantic_at = session.observation.last_semantic_at or session.phase_since or now
        evidence_at = session.observation.last_evidence_at
        self.connection.execute(
            """
            INSERT INTO silence_samples(
                bucket_start, instance_id, session_id, workspace, phase, model,
                tool_category, assessment, silence_seconds, evidence_age_seconds
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(bucket_start, instance_id, session_id) DO UPDATE SET
                workspace = excluded.workspace,
                phase = excluded.phase,
                model = excluded.model,
                tool_category = excluded.tool_category,
                assessment = excluded.assessment,
                silence_seconds = excluded.silence_seconds,
                evidence_age_seconds = excluded.evidence_age_seconds
            """,
            (
                bucket,
                session.instance_id,
                session.session_id,
                session.process.cwd,
                session.lifecycle.value,
                session.process.model,
                session.current_operation.category,
                session.silence.state.value,
                max(0.0, now - semantic_at),
                max(0.0, now - evidence_at) if evidence_at is not None else None,
            ),
        )

    def _record_compact_metrics(
        self,
        session: SessionHealth,
        now: float,
        active_keys: set[str],
    ) -> None:
        for compact in session.compactions:
            timestamp = compact.terminal_at or compact.started_at or compact.requested_at or now
            operation_identity = [
                session.instance_id,
                session.session_id,
                compact.operation_id,
                compact.turn_id,
                compact.requested_at,
                compact.started_at,
            ]
            operation_key = hashlib.sha256(
                json.dumps(operation_identity, separators=(",", ":")).encode()
            ).hexdigest()
            active_keys.add(operation_key)
            signature = (
                timestamp,
                compact.trigger or "unknown",
                compact.status,
                compact.duration_seconds,
                compact.retry_count,
                compact.context_tokens,
                compact.context_tokens_after,
            )
            if self._compact_signatures.get(operation_key) == signature:
                continue
            self.connection.execute(
                """
                INSERT INTO compact_metrics(
                    operation_key, timestamp, instance_id, session_id, trigger,
                    status, duration_seconds, retry_count, context_before, context_after
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(operation_key) DO UPDATE SET
                    timestamp = excluded.timestamp,
                    trigger = excluded.trigger,
                    status = excluded.status,
                    duration_seconds = excluded.duration_seconds,
                    retry_count = excluded.retry_count,
                    context_before = excluded.context_before,
                    context_after = excluded.context_after
                """,
                (
                    operation_key,
                    timestamp,
                    session.instance_id,
                    session.session_id,
                    compact.trigger or "unknown",
                    compact.status,
                    compact.duration_seconds,
                    compact.retry_count,
                    compact.context_tokens,
                    compact.context_tokens_after,
                ),
            )
            self._compact_signatures[operation_key] = signature

    def _record_instance_bucket(
        self, snapshot: MonitorSnapshot, instance_index: int, bucket: int
    ) -> None:
        instance = snapshot.instances[instance_index]
        sessions = instance.sessions
        active = sum(
            session.lifecycle
            in {
                LifecycleState.STARTING,
                LifecycleState.WAITING_RESPONSE,
                LifecycleState.GENERATING,
                LifecycleState.RUNNING_TOOL,
                LifecycleState.COMPACTING,
            }
            for session in sessions
        )
        self.connection.execute(
            """
            INSERT INTO instance_buckets(
                bucket_start, instance_id, samples, session_samples,
                active_session_samples, failure_samples, alert_samples,
                stalled_samples, diagnostic_samples, collection_seconds_sum
            ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(bucket_start, instance_id) DO UPDATE SET
                samples = samples + 1,
                session_samples = session_samples + excluded.session_samples,
                active_session_samples = active_session_samples + excluded.active_session_samples,
                failure_samples = failure_samples + excluded.failure_samples,
                alert_samples = alert_samples + excluded.alert_samples,
                stalled_samples = stalled_samples + excluded.stalled_samples,
                diagnostic_samples = diagnostic_samples + excluded.diagnostic_samples,
                collection_seconds_sum = collection_seconds_sum + excluded.collection_seconds_sum
            """,
            (
                bucket,
                instance.instance_id,
                len(sessions),
                active,
                sum(session.lifecycle == LifecycleState.FAILED for session in sessions),
                sum(bool(session.alert) for session in sessions),
                sum(session.network.state.value == "STALLED" for session in sessions),
                int(bool(instance.diagnostics)),
                snapshot.collection_duration_seconds,
            ),
        )

    def _record_operational_durations(
        self,
        snapshot: MonitorSnapshot,
        instance_index: int,
        bucket: int,
    ) -> None:
        instance = snapshot.instances[instance_index]
        metrics: dict[str, tuple[float, int]] = defaultdict(lambda: (0.0, 0))

        def add(metric: str, seconds: float, occurrences: int = 1) -> None:
            previous_seconds, previous_occurrences = metrics[metric]
            metrics[metric] = (
                previous_seconds + seconds,
                previous_occurrences + occurrences,
            )

        for session in instance.sessions:
            add(f"phase:{session.lifecycle.value}", snapshot.interval_seconds)
            if session.silence.state.value == "WAITING_UPSTREAM":
                add("waiting_upstream", snapshot.interval_seconds)
            if session.attention_request is not None:
                add("attention_wait", snapshot.interval_seconds)
            if session.silence.state.value == "OBSERVER_BLIND":
                add("observer_blind", snapshot.interval_seconds)

        protocol_degraded = bool(instance.unknown_event_types) or any(
            item.error or item.stale_age_seconds is not None
            for item in instance.collector_health
            if item.name.startswith(("rollout", "tui_session_log", "hook_events"))
        )
        if protocol_degraded:
            add("protocol_degraded", snapshot.interval_seconds)

        for metric, (seconds_sum, occurrences) in metrics.items():
            self.connection.execute(
                """
                INSERT INTO operational_durations(
                    bucket_start, instance_id, metric, seconds_sum, occurrences
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(bucket_start, instance_id, metric) DO UPDATE SET
                    seconds_sum = seconds_sum + excluded.seconds_sum,
                    occurrences = occurrences + excluded.occurrences
                """,
                (bucket, instance.instance_id, metric, seconds_sum, occurrences),
            )

    def record_snapshot(
        self,
        snapshot: MonitorSnapshot,
        *,
        maintain: bool = True,
    ) -> HistoryWriteResult:
        """Record important events and bounded aggregate buckets from a snapshot."""

        self._verify_private_storage()
        now = _timestamp(snapshot.generated_at)
        session_bucket = int(now // 10 * 10)
        instance_bucket = int(now // 60 * 60)
        inserted = 0
        active_sessions = {
            (session.instance_id, session.session_id) for session in snapshot.sessions
        }
        self._seen_event_keys = {
            key: value for key, value in self._seen_event_keys.items() if key in active_sessions
        }
        active_turn_keys: set[str] = set()
        active_compact_keys: set[str] = set()
        with self.connection:
            for instance in snapshot.instances:
                self._register_instance_identity(instance, now)
            for session in snapshot.sessions:
                inserted += self._record_transition(session, now)
                for event in self._important_events(session.events):
                    inserted += self._insert_event(session.instance_id, session.session_id, event)
                self._record_session_bucket(session, session_bucket)
                self._record_silence_sample(session, session_bucket, now)
                self._record_turn_metrics(session, now, active_turn_keys)
                self._record_compact_metrics(session, now, active_compact_keys)
            for index in range(len(snapshot.instances)):
                self._record_instance_bucket(snapshot, index, instance_bucket)
                self._record_operational_durations(snapshot, index, session_bucket)
        self._turn_signatures = {
            key: value for key, value in self._turn_signatures.items() if key in active_turn_keys
        }
        self._compact_signatures = {
            key: value
            for key, value in self._compact_signatures.items()
            if key in active_compact_keys
        }
        size_exceeded = self.max_bytes is not None and self._database_bytes() > self.max_bytes
        if maintain and (
            self._last_prune_at is None or now - self._last_prune_at >= 60 or size_exceeded
        ):
            self.prune(now=now)
            self._last_prune_at = now
        self._verify_private_storage()
        return HistoryWriteResult(
            inserted,
            len(snapshot.sessions),
            len(snapshot.instances),
        )

    def maintenance_due(self, *, now: float) -> bool:
        size_exceeded = self.max_bytes is not None and self._database_bytes() > self.max_bytes
        return self._last_prune_at is None or now - self._last_prune_at >= 60 or size_exceeded

    def maintain(self, *, now: float) -> None:
        self.prune(now=now)
        self._last_prune_at = now

    def window_stats(
        self,
        *,
        now: float,
        instance_id: str | None = None,
    ) -> list[HistoryWindowStats]:
        """Return explicitly windowed, sample-counted operational statistics."""

        self._verify_private_storage()
        stats: list[HistoryWindowStats] = []
        for label, window_seconds in HISTORY_WINDOWS:
            cutoff = now - window_seconds
            instance_clause = " AND instance_id = ?" if instance_id else ""
            parameters: tuple[object, ...] = (cutoff, instance_id) if instance_id else (cutoff,)
            rows = self.connection.execute(
                "SELECT status, ttft_seconds, tool_seconds, recovery_seconds "
                f"FROM turn_metrics WHERE timestamp >= ?{instance_clause}",
                parameters,
            ).fetchall()
            sample_row = self.connection.execute(
                "SELECT COALESCE(SUM(samples), 0) FROM instance_buckets "
                f"WHERE bucket_start >= ?{instance_clause}",
                parameters,
            ).fetchone()
            event_rows = self.connection.execute(
                "SELECT event_type, COUNT(*) FROM events WHERE timestamp >= ?"
                f"{instance_clause} AND event_type IN "
                "('RECONNECTING', 'TRANSPORT_FALLBACK', 'COMPACT_COMPLETED') "
                "GROUP BY event_type",
                parameters,
            ).fetchall()
            counts = {str(kind): int(count) for kind, count in event_rows}
            silence_rows = self.connection.execute(
                "SELECT silence_seconds FROM silence_samples WHERE bucket_start >= ?"
                f"{instance_clause}",
                parameters,
            ).fetchall()
            compact_rows = self.connection.execute(
                "SELECT trigger, status, duration_seconds, retry_count, "
                "context_before, context_after FROM compact_metrics "
                f"WHERE timestamp >= ?{instance_clause}",
                parameters,
            ).fetchall()
            duration_rows = self.connection.execute(
                "SELECT metric, SUM(seconds_sum), SUM(occurrences) "
                "FROM operational_durations WHERE bucket_start >= ?"
                f"{instance_clause} GROUP BY metric",
                parameters,
            ).fetchall()
            durations = {
                str(metric): (float(seconds_sum or 0.0), int(occurrences or 0))
                for metric, seconds_sum, occurrences in duration_rows
            }
            observation_samples = sum(
                occurrences
                for metric, (_, occurrences) in durations.items()
                if metric.startswith("phase:")
            )
            ttfts = [float(row[1]) for row in rows if row[1] is not None]
            tools = [float(row[2]) for row in rows if row[2] is not None]
            recoveries = [float(row[3]) for row in rows if row[3] is not None]
            failures = sum(str(row[0]).lower() == "failed" for row in rows)
            silences = [float(row[0]) for row in silence_rows]
            compact_durations = [float(row[2]) for row in compact_rows if row[2] is not None]
            compact_contexts = [
                (float(row[4]), float(row[5]))
                for row in compact_rows
                if row[4] is not None and row[5] is not None
            ]
            stats.append(
                HistoryWindowStats(
                    label=label,
                    window_seconds=window_seconds,
                    sample_count=int(sample_row[0] or 0),
                    turn_count=len(rows),
                    failure_count=failures,
                    failure_rate=failures / len(rows) if rows else None,
                    ttft_samples=len(ttfts),
                    ttft_p50_seconds=_percentile(ttfts, 0.50),
                    ttft_p95_seconds=_percentile(ttfts, 0.95),
                    tool_samples=len(tools),
                    tool_p50_seconds=_percentile(tools, 0.50),
                    tool_p95_seconds=_percentile(tools, 0.95),
                    reconnect_count=counts.get("RECONNECTING", 0),
                    fallback_count=counts.get("TRANSPORT_FALLBACK", 0),
                    recovery_samples=len(recoveries),
                    recovery_average_seconds=(
                        sum(recoveries) / len(recoveries) if recoveries else None
                    ),
                    compact_count=counts.get("COMPACT_COMPLETED", 0),
                    compact_per_hour=(counts.get("COMPACT_COMPLETED", 0) * 3600 / window_seconds),
                    silence_samples=len(silences),
                    silence_p50_seconds=_percentile(silences, 0.50),
                    silence_p95_seconds=_percentile(silences, 0.95),
                    compact_manual_count=sum(str(row[0]) == "manual" for row in compact_rows),
                    compact_auto_count=sum(str(row[0]) == "auto" for row in compact_rows),
                    compact_failure_count=sum(str(row[1]) == "failed" for row in compact_rows),
                    compact_retry_count=sum(int(row[3] or 0) for row in compact_rows),
                    compact_duration_samples=len(compact_durations),
                    compact_duration_p50_seconds=_percentile(compact_durations, 0.50),
                    compact_duration_p95_seconds=_percentile(compact_durations, 0.95),
                    compact_context_samples=len(compact_contexts),
                    compact_context_before_average=(
                        sum(before for before, _ in compact_contexts) / len(compact_contexts)
                        if compact_contexts
                        else None
                    ),
                    compact_context_after_average=(
                        sum(after for _, after in compact_contexts) / len(compact_contexts)
                        if compact_contexts
                        else None
                    ),
                    phase_duration_seconds=tuple(
                        sorted(
                            (metric.removeprefix("phase:"), seconds_sum)
                            for metric, (seconds_sum, _) in durations.items()
                            if metric.startswith("phase:")
                        )
                    ),
                    waiting_upstream_seconds=durations.get("waiting_upstream", (0.0, 0))[0],
                    attention_wait_seconds=durations.get("attention_wait", (0.0, 0))[0],
                    observer_blind_samples=durations.get("observer_blind", (0.0, 0))[1],
                    observer_blind_frequency=(
                        durations.get("observer_blind", (0.0, 0))[1] / observation_samples
                        if observation_samples
                        else None
                    ),
                    protocol_degraded_samples=durations.get("protocol_degraded", (0.0, 0))[1],
                    protocol_degraded_frequency=(
                        durations.get("protocol_degraded", (0.0, 0))[1]
                        / max(int(sample_row[0] or 0), 1)
                        if int(sample_row[0] or 0)
                        else None
                    ),
                )
            )
        self._verify_private_storage()
        return stats

    def silence_baseline(
        self,
        *,
        now: float,
        instance_id: str,
        workspace: str,
        phase: str,
        model: str,
        tool_category: str,
        window_seconds: int = 86400,
    ) -> tuple[int, float | None, float | None]:
        """Return context-matched silence sample count, p50, and p95."""

        self._verify_private_storage()
        rows = self.connection.execute(
            """
            SELECT silence_seconds FROM silence_samples
            WHERE bucket_start >= ? AND instance_id = ? AND workspace = ?
              AND phase = ? AND model = ? AND tool_category = ?
            """,
            (
                now - window_seconds,
                instance_id,
                workspace,
                phase,
                model,
                tool_category,
            ),
        ).fetchall()
        values = [float(row[0]) for row in rows]
        result = len(values), _percentile(values, 0.50), _percentile(values, 0.95)
        self._verify_private_storage()
        return result

    def prune(self, *, now: float | None = None) -> None:
        """Apply age and file-size limits; size pruning removes oldest buckets first."""

        self._verify_private_storage()
        current = now if now is not None else datetime.now(timezone.utc).timestamp()
        deleted = False
        if self.max_days is not None:
            cutoff = current - self.max_days * 86400
            before = self.connection.total_changes
            with self.connection:
                self.connection.execute("DELETE FROM events WHERE timestamp < ?", (cutoff,))
                self.connection.execute(
                    "DELETE FROM session_buckets WHERE bucket_start < ?", (cutoff,)
                )
                self.connection.execute(
                    "DELETE FROM instance_buckets WHERE bucket_start < ?", (cutoff,)
                )
                self.connection.execute("DELETE FROM turn_metrics WHERE timestamp < ?", (cutoff,))
                self.connection.execute(
                    "DELETE FROM silence_samples WHERE bucket_start < ?", (cutoff,)
                )
                self.connection.execute(
                    "DELETE FROM compact_metrics WHERE timestamp < ?", (cutoff,)
                )
                self.connection.execute(
                    "DELETE FROM operational_durations WHERE bucket_start < ?", (cutoff,)
                )
                self.connection.execute("DELETE FROM session_state WHERE updated_at < ?", (cutoff,))
            deleted = self.connection.total_changes > before

        if self.max_bytes is None:
            self._verify_private_storage()
            return
        while self._database_used_bytes() > self.max_bytes:
            candidates = [
                ("events", "timestamp"),
                ("session_buckets", "bucket_start"),
                ("instance_buckets", "bucket_start"),
                ("turn_metrics", "timestamp"),
                ("silence_samples", "bucket_start"),
                ("compact_metrics", "timestamp"),
                ("operational_durations", "bucket_start"),
            ]
            counts = []
            for table, column in candidates:
                row = self.connection.execute(
                    f"SELECT MIN({column}), COUNT(*) FROM {table}"
                ).fetchone()
                if row and row[0] is not None:
                    counts.append((float(row[0]), table, column, int(row[1])))
            if not counts:
                break
            _, table, column, count = min(counts)
            batch = max(1, min(1000, count // 10 or 1))
            with self.connection:
                self.connection.execute(
                    f"DELETE FROM {table} WHERE rowid IN "
                    f"(SELECT rowid FROM {table} ORDER BY {column} LIMIT ?)",
                    (batch,),
                )
            deleted = True
        if deleted and self._database_bytes() > self.max_bytes:
            self.connection.execute("VACUUM")
        self._verify_private_storage()

    def _database_bytes(self) -> int:
        if self._private_file is None:
            return 0
        self._private_file.verify_path()
        return os.fstat(self._private_file.descriptor).st_size

    def _database_used_bytes(self) -> int:
        page_size = int(self.connection.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(self.connection.execute("PRAGMA page_count").fetchone()[0])
        free_pages = int(self.connection.execute("PRAGMA freelist_count").fetchone()[0])
        return max(0, page_count - free_pages) * page_size


SilenceBaselineKey = tuple[str, str, str, str, str]


@dataclass(frozen=True)
class _HistoryTask:
    snapshot: MonitorSnapshot
    important: bool
    enqueued_at: float


class AsyncHistoryWriter:
    """Own one HistoryStore on a daemon worker and expose only cached reads."""

    def __init__(
        self,
        path: str | Path,
        *,
        max_days: int | None = 7,
        max_bytes: int | None = 128 * 1024 * 1024,
        queue_capacity: int = 4,
        operation_deadline_seconds: float = 0.5,
        maintenance_deadline_seconds: float = 0.25,
        store_factory: Callable[..., HistoryStore] = HistoryStore,
    ) -> None:
        if queue_capacity < 1:
            raise ValueError("queue_capacity must be positive")
        self.path = Path(path)
        self.max_days = max_days
        self.max_bytes = max_bytes
        self.queue_capacity = queue_capacity
        self.operation_deadline_seconds = operation_deadline_seconds
        self.maintenance_deadline_seconds = maintenance_deadline_seconds
        self.store_factory = store_factory
        self._condition = threading.Condition()
        self._queue: deque[_HistoryTask] = deque()
        self._windows: dict[str, list[HistoryWindowStats]] = {}
        self._baselines: dict[SilenceBaselineKey, tuple[int, float | None, float | None]] = {}
        self._enqueued_samples = 0
        self._persisted_samples = 0
        self._dropped_samples = 0
        self._coalesced_samples = 0
        self._last_persisted_sample_at = ""
        self._last_success_at: float | None = None
        self._stats_generated_at: float | None = None
        self._writer_lag_seconds: float | None = None
        self._consecutive_failures = 0
        self._error = ""
        self._maintenance_error = ""
        self._stopping = False
        self._closed = False
        self._shutdown_timed_out = False
        self._thread = threading.Thread(
            target=self._run,
            name="codexdeck-history",
            daemon=True,
        )
        self._thread.start()

    @staticmethod
    def _important(snapshot: MonitorSnapshot) -> bool:
        return any(
            session.current_failure is not None
            or session.attention_request is not None
            or bool(session.alert)
            or any(
                event.kind.upper()
                in {
                    "LIFECYCLE_TRANSITION",
                    "TURN_FAILED",
                    "TURN_ABORTED",
                    "OPERATION_ERROR",
                    "ACTION_REQUIRED",
                    "ACTION_RESOLVED",
                    "RECOVERED",
                }
                for event in session.events
            )
            for session in snapshot.sessions
        )

    def enqueue(self, snapshot: MonitorSnapshot) -> bool:
        task = _HistoryTask(
            snapshot=deepcopy(snapshot),
            important=self._important(snapshot),
            enqueued_at=time.time(),
        )
        with self._condition:
            if self._stopping:
                self._dropped_samples += 1
                return False
            self._enqueued_samples += 1
            if len(self._queue) >= self.queue_capacity:
                if not task.important and self._queue and not self._queue[-1].important:
                    self._queue[-1] = task
                    self._coalesced_samples += 1
                    self._condition.notify()
                    return True
                removable = next(
                    (index for index, queued in enumerate(self._queue) if not queued.important),
                    None,
                )
                if removable is None:
                    self._dropped_samples += 1
                    return False
                del self._queue[removable]
                self._dropped_samples += 1
            self._queue.append(task)
            self._condition.notify()
            return True

    def cached_windows(self, instance_id: str) -> list[HistoryWindowStats]:
        with self._condition:
            return list(self._windows.get(instance_id, ()))

    def cached_silence_baseline(
        self,
        *,
        instance_id: str,
        workspace: str,
        phase: str,
        model: str,
        tool_category: str,
    ) -> tuple[int, float | None, float | None]:
        key = (instance_id, workspace, phase, model, tool_category)
        with self._condition:
            return self._baselines.get(key, (0, None, None))

    def status(self, *, now: float | None = None) -> HistoryPersistenceStatus:
        current = time.time() if now is None else now
        with self._condition:
            return HistoryPersistenceStatus(
                enabled=True,
                queue_depth=len(self._queue),
                queue_capacity=self.queue_capacity,
                enqueued_samples=self._enqueued_samples,
                persisted_samples=self._persisted_samples,
                dropped_samples=self._dropped_samples,
                coalesced_samples=self._coalesced_samples,
                last_persisted_sample_at=self._last_persisted_sample_at,
                last_success_at=self._last_success_at,
                stats_generated_at=self._stats_generated_at,
                stats_age_seconds=(
                    max(0.0, current - self._stats_generated_at)
                    if self._stats_generated_at is not None
                    else None
                ),
                writer_lag_seconds=self._writer_lag_seconds,
                consecutive_failures=self._consecutive_failures,
                error=self._error,
                maintenance_error=self._maintenance_error,
                shutdown_timed_out=self._shutdown_timed_out,
            )

    def wait_until_idle(self, timeout_seconds: float = 1.0) -> bool:
        expires_at = time.monotonic() + max(0.0, timeout_seconds)
        with self._condition:
            target = self._enqueued_samples - self._dropped_samples - self._coalesced_samples
            while self._persisted_samples < target and time.monotonic() < expires_at:
                self._condition.wait(expires_at - time.monotonic())
            return self._persisted_samples >= target

    def close(self, *, flush_timeout_seconds: float = 1.0) -> None:
        with self._condition:
            if self._closed:
                return
            self._stopping = True
            self._condition.notify_all()
        self._thread.join(max(0.0, flush_timeout_seconds))
        with self._condition:
            if self._thread.is_alive():
                self._shutdown_timed_out = True
                self._dropped_samples += len(self._queue)
                self._queue.clear()
            self._closed = True

    def _next_task(self) -> _HistoryTask | None:
        with self._condition:
            while not self._queue and not self._stopping:
                self._condition.wait()
            if self._queue:
                return self._queue.popleft()
            return None

    def _run(self) -> None:
        store: HistoryStore | None = None
        try:
            while True:
                if store is None:
                    try:
                        store = self.store_factory(
                            self.path,
                            max_days=self.max_days,
                            max_bytes=self.max_bytes,
                            busy_timeout_seconds=0.1,
                        )
                    except Exception as exc:
                        self._record_failure(exc)
                        with self._condition:
                            if self._stopping and not self._queue:
                                break
                            self._condition.wait(0.1)
                        continue
                task = self._next_task()
                if task is None:
                    break
                self._process(store, task)
        except Exception as exc:
            self._record_failure(exc)
        finally:
            if store is not None:
                try:
                    store.close()
                except Exception as exc:
                    self._record_failure(exc)

    def _process(self, store: HistoryStore, task: _HistoryTask) -> None:
        try:
            sample_time = _timestamp(task.snapshot.generated_at)
            with store.deadline(self.operation_deadline_seconds):
                store.record_snapshot(task.snapshot, maintain=False)
                windows = {
                    instance.instance_id: store.window_stats(
                        now=sample_time,
                        instance_id=instance.instance_id,
                    )
                    for instance in task.snapshot.instances
                }
                baselines = {
                    (
                        session.instance_id,
                        session.process.cwd,
                        session.lifecycle.value,
                        session.process.model,
                        session.current_operation.category,
                    ): store.silence_baseline(
                        now=sample_time,
                        instance_id=session.instance_id,
                        workspace=session.process.cwd,
                        phase=session.lifecycle.value,
                        model=session.process.model,
                        tool_category=session.current_operation.category,
                    )
                    for session in task.snapshot.sessions
                }
            if store.maintenance_due(now=sample_time):
                try:
                    with store.deadline(self.maintenance_deadline_seconds):
                        store.maintain(now=sample_time)
                    with self._condition:
                        self._maintenance_error = ""
                except Exception as exc:
                    with self._condition:
                        self._maintenance_error = f"{type(exc).__name__}: {exc}"[:256]
            completed_at = time.time()
            with self._condition:
                self._windows = windows
                self._baselines = baselines
                self._persisted_samples += 1
                self._last_persisted_sample_at = task.snapshot.generated_at
                self._last_success_at = completed_at
                self._stats_generated_at = completed_at
                self._writer_lag_seconds = max(0.0, completed_at - task.enqueued_at)
                self._consecutive_failures = 0
                self._error = ""
                self._condition.notify_all()
        except Exception as exc:
            self._record_failure(exc)

    def _record_failure(self, error: BaseException) -> None:
        with self._condition:
            self._consecutive_failures += 1
            self._error = f"{type(error).__name__}: {error}"[:256]
            self._condition.notify_all()
