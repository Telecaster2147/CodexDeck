"""Optional local history storage, isolated from Codex-owned databases."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from models import LifecycleState, MonitorSnapshot, NormalizedEvent, SessionHealth


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
        "COMPACT_COMPLETED",
        "PROCESS_EXITED",
        "PROCESS_RESUMED",
        "SESSION_EXITED",
    }
)


@dataclass(frozen=True)
class HistoryWriteResult:
    events_inserted: int
    session_buckets_updated: int
    instance_buckets_updated: int


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


class HistoryStore:
    """Write bounded monitor history to a dedicated SQLite database.

    The caller chooses the path explicitly. The store never opens any path from
    ``snapshot.instances[*].paths`` and therefore cannot mutate Codex state.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        max_days: int | None = 30,
        max_bytes: int | None = 128 * 1024 * 1024,
    ) -> None:
        if max_days is not None and max_days < 1:
            raise ValueError("max_days must be positive or None")
        if max_bytes is not None and max_bytes < 1:
            raise ValueError("max_bytes must be positive or None")
        self.path = Path(path).expanduser()
        self.max_days = max_days
        self.max_bytes = max_bytes
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=5.0)
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self._initialize()

    def __enter__(self) -> "HistoryStore":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS history_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT OR IGNORE INTO history_meta(key, value) VALUES ('schema_version', '1');

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

            CREATE TABLE IF NOT EXISTS session_state (
                instance_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                lifecycle TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY(instance_id, session_id)
            );
            """
        )
        self.connection.commit()

    @staticmethod
    def _important_events(events: Iterable[NormalizedEvent]) -> Iterable[NormalizedEvent]:
        return (event for event in events if event.kind.upper() in KEY_EVENT_TYPES)

    def _insert_event(
        self, instance_id: str, session_id: str, event: NormalizedEvent
    ) -> int:
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO events(
                event_key, timestamp, instance_id, session_id, event_type,
                category, summary, detail, source, turn_id, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _event_key(instance_id, session_id, event),
                event.timestamp,
                instance_id,
                session_id,
                event.kind.upper(),
                _event_category(event),
                event.summary,
                event.detail,
                event.source,
                event.turn_id,
                json.dumps(event.metadata, ensure_ascii=True, sort_keys=True, default=str),
            ),
        )
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
            source="codexnet-history",
            source_id=f"{old}:{current}:{now}",
            derived=True,
            metadata={"from": old, "to": current},
        )
        return self._insert_event(session.instance_id, session.session_id, transition)

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

    def record_snapshot(self, snapshot: MonitorSnapshot) -> HistoryWriteResult:
        """Record important events and bounded aggregate buckets from a snapshot."""

        now = _timestamp(snapshot.generated_at)
        session_bucket = int(now // 10 * 10)
        instance_bucket = int(now // 60 * 60)
        inserted = 0
        with self.connection:
            for session in snapshot.sessions:
                inserted += self._record_transition(session, now)
                for event in self._important_events(session.events):
                    inserted += self._insert_event(
                        session.instance_id, session.session_id, event
                    )
                self._record_session_bucket(session, session_bucket)
            for index in range(len(snapshot.instances)):
                self._record_instance_bucket(snapshot, index, instance_bucket)
        self.prune(now=now)
        return HistoryWriteResult(
            inserted,
            len(snapshot.sessions),
            len(snapshot.instances),
        )

    def prune(self, *, now: float | None = None) -> None:
        """Apply age and file-size limits; size pruning removes oldest buckets first."""

        current = now if now is not None else datetime.now(timezone.utc).timestamp()
        if self.max_days is not None:
            cutoff = current - self.max_days * 86400
            with self.connection:
                self.connection.execute("DELETE FROM events WHERE timestamp < ?", (cutoff,))
                self.connection.execute(
                    "DELETE FROM session_buckets WHERE bucket_start < ?", (cutoff,)
                )
                self.connection.execute(
                    "DELETE FROM instance_buckets WHERE bucket_start < ?", (cutoff,)
                )
                self.connection.execute("DELETE FROM session_state WHERE updated_at < ?", (cutoff,))

        if self.max_bytes is None:
            return
        while self._database_bytes() > self.max_bytes:
            candidates = [
                ("events", "timestamp"),
                ("session_buckets", "bucket_start"),
                ("instance_buckets", "bucket_start"),
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
            self.connection.execute("VACUUM")

    def _database_bytes(self) -> int:
        try:
            return self.path.stat().st_size
        except FileNotFoundError:
            return 0
