"""Snapshot publication and optional history persistence."""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import datetime

from diagnostics import CollectorTracker
from history import HistoryStore
from models import HistoryWindowStats, InstanceSnapshot, MonitorSnapshot


class SnapshotPublisher:
    """Publish immutable-facing snapshots after a completed full collection."""

    def __init__(
        self,
        interval: float,
        collectors: CollectorTracker,
        history: HistoryStore | None,
    ) -> None:
        self.interval = interval
        self.collectors = collectors
        self.history = history
        self.history_windows_cache: dict[str, tuple[int, list[HistoryWindowStats]]] = {}

    def publish(
        self,
        *,
        instances: list[InstanceSnapshot],
        started: float,
        now_monotonic: float,
        diagnostics: list[str],
        discovery_stale_since: float | None,
        socket_stale_since: float | None,
    ) -> MonitorSnapshot:
        snapshot = MonitorSnapshot(
            generated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            interval_seconds=self.interval,
            instances=sorted(instances, key=lambda item: item.display_codex_home),
            collection_duration_seconds=time.monotonic() - started,
            diagnostics=diagnostics,
            process_data_stale_age_seconds=(
                now_monotonic - discovery_stale_since
                if discovery_stale_since is not None
                else None
            ),
            socket_data_stale_age_seconds=(
                now_monotonic - socket_stale_since
                if socket_stale_since is not None
                else None
            ),
            collector_health=self.collectors.snapshot(),
        )
        if self.history is not None:
            snapshot = self._record_history(snapshot, started)
        return snapshot

    def _record_history(
        self,
        snapshot: MonitorSnapshot,
        started: float,
    ) -> MonitorSnapshot:
        history_started = time.monotonic()
        instances = snapshot.instances
        diagnostics = snapshot.diagnostics
        try:
            self.history.record_snapshot(snapshot)
            history_now = datetime.fromisoformat(snapshot.generated_at).timestamp()
            refreshed_instances: list[InstanceSnapshot] = []
            for instance in snapshot.instances:
                stats_bucket = int(history_now // 10)
                cached = self.history_windows_cache.get(instance.instance_id)
                if cached is None or cached[0] != stats_bucket:
                    stats = self.history.window_stats(
                        now=history_now,
                        instance_id=instance.instance_id,
                    )
                    self.history_windows_cache[instance.instance_id] = (stats_bucket, stats)
                else:
                    stats = cached[1]
                refreshed_instances.append(replace(instance, history_windows=list(stats)))
            instances = refreshed_instances
            active_instances = {instance.instance_id for instance in snapshot.instances}
            self.history_windows_cache = {
                key: value
                for key, value in self.history_windows_cache.items()
                if key in active_instances
            }
            self.collectors.record("history", history_started)
        except Exception as exc:
            self.collectors.record("history", history_started, exc)
            diagnostics = [*diagnostics, f"历史库写入失败：{exc}"]
        return replace(
            snapshot,
            instances=instances,
            diagnostics=diagnostics,
            collection_duration_seconds=time.monotonic() - started,
            collector_health=self.collectors.snapshot(),
        )
