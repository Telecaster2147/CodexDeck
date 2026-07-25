"""Snapshot publication."""

from __future__ import annotations

import time
from datetime import datetime

from diagnostics import CollectorTracker
from models import DiscoverySummary, InstanceSnapshot, MonitorSnapshot, ObserverHealth
from temporal import apply_temporal_completeness, build_temporal_cut


class SnapshotPublisher:
    """Publish immutable-facing snapshots after a completed full collection."""

    def __init__(
        self,
        interval: float,
        collectors: CollectorTracker,
    ) -> None:
        self.interval = interval
        self.collectors = collectors
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
        return snapshot
