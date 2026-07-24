"""Snapshot publication and optional history persistence."""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import datetime

from diagnostics import CollectorTracker
from history import AsyncHistoryWriter
from models import CollectorHealth, DiscoverySummary, InstanceSnapshot, MonitorSnapshot, ObserverHealth
from temporal import apply_temporal_completeness, build_temporal_cut


class SnapshotPublisher:
    """Publish immutable-facing snapshots after a completed full collection."""

    def __init__(
        self,
        interval: float,
        collectors: CollectorTracker,
        history: AsyncHistoryWriter | None,
    ) -> None:
        self.interval = interval
        self.collectors = collectors
        self.history = history
        self.generation = 0

    def publish(
        self,
        *,
        instances: list[InstanceSnapshot],
        started: float,
        now_monotonic: float,
        diagnostics: list[str],
        discovery: DiscoverySummary,
        discovery_stale_since: float | None,
        socket_stale_since: float | None,
    ) -> MonitorSnapshot:
        duration = time.monotonic() - started
        completed_at = time.time()
        self.generation += 1
        collector_health = self.collectors.snapshot()
        temporal = build_temporal_cut(
            instances,
            collector_health,
            now=completed_at,
            interval=self.interval,
            generation=self.generation,
        )
        instances = apply_temporal_completeness(instances, temporal)
        snapshot = MonitorSnapshot(
            generated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            interval_seconds=self.interval,
            instances=sorted(instances, key=lambda item: item.display_codex_home),
            collection_duration_seconds=duration,
            diagnostics=diagnostics,
            discovery=discovery,
            process_data_stale_age_seconds=(
                now_monotonic - discovery_stale_since if discovery_stale_since is not None else None
            ),
            socket_data_stale_age_seconds=(
                now_monotonic - socket_stale_since if socket_stale_since is not None else None
            ),
            collector_health=collector_health,
            observer=ObserverHealth(
                sample_kind="full",
                scheduled_at=completed_at - duration,
                started_at=completed_at - duration,
                completed_at=completed_at,
                duration_seconds=duration,
                last_success_at=completed_at,
            ),
            temporal=temporal,
        )
        return self._attach_history(snapshot)

    def _attach_history(self, snapshot: MonitorSnapshot) -> MonitorSnapshot:
        if self.history is None:
            return snapshot
        instances = [
            replace(
                instance,
                history_windows=self.history.cached_windows(instance.instance_id),
            )
            for instance in snapshot.instances
        ]
        self.history.enqueue(snapshot)
        status = self.history.status()
        collector = CollectorHealth(
            name="history",
            duration_seconds=0.0,
            last_success_at=status.last_success_at,
            stale_age_seconds=status.stats_age_seconds if status.consecutive_failures else None,
            consecutive_failures=status.consecutive_failures,
            error=status.error,
            budget_exceeded=False,
        )
        collector_health = [item for item in snapshot.collector_health if item.name != "history"]
        collector_health.append(collector)
        diagnostics = list(snapshot.diagnostics)
        if status.error:
            diagnostics.append(f"历史写入器降级：{status.error}")
        if status.dropped_samples:
            diagnostics.append(
                "历史写入队列已丢弃样本："
                f"dropped={status.dropped_samples}, coalesced={status.coalesced_samples}"
            )
        return replace(
            snapshot,
            instances=instances,
            diagnostics=diagnostics,
            collector_health=collector_health,
            history=status,
        )
