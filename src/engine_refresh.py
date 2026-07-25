"""Fast rollout-only snapshot refresh path."""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from codex.rollout import RolloutActivity
from models import InstanceSnapshot, MonitorSnapshot, NormalizedEvent, ProcessInfo, SessionHealth
from temporal import apply_temporal_completeness, build_temporal_cut


class FastRefreshMixin:
    """Refresh known rollout streams without running full host collectors."""

    def refresh_events(self, snapshot: MonitorSnapshot) -> MonitorSnapshot:
        """Refresh active rollout events without resampling processes or sockets."""

        started = time.monotonic()
        refreshed_instances: list[InstanceSnapshot] = []
        changed = False
        for instance in snapshot.instances:
            refreshed_sessions: list[SessionHealth] = []
            refreshed_processes: dict[str, ProcessInfo] = {}
            rollout_paths: set[str] = set()
            rollout_activity_values: list[dict[str, object]] = []
            for session in instance.sessions:
                process = session.process
                if session.process_exited:
                    refreshed_sessions.append(session)
                    continue
                rollout_activity = RolloutActivity(process.rollout_path, time.time())
                rollout_events: tuple[NormalizedEvent, ...] = ()
                terminal_changed = False
                key = session.session_identity
                if process.rollout_path:
                    rollout_paths.add(process.rollout_path)
                    rollout_result = self.rollouts.read_with_activity(
                        Path(process.rollout_path),
                        allow_terminal_metadata_backfill=False,
                    )
                    rollout_activity = rollout_result.activity
                    rollout_events = rollout_result.events
                    terminal_changed = self.terminals.apply(key, rollout_result.terminal_updates)
                rollout_activity_values.append(self._rollout_activity_value(rollout_activity))
                incoming = list(rollout_events)
                if incoming or rollout_activity.changed or terminal_changed:
                    changed = True
                    incoming = self._with_compact_config(
                        incoming,
                        instance.auto_compact_token_limit,
                        instance.auto_compact_token_limit_scope,
                    )
                    self.machine.update_coverage(
                        key,
                        self._evidence_coverage(
                            [rollout_activity],
                            bootstrap_truncated=self.rollouts.has_truncated_context(
                                {process.rollout_path} if process.rollout_path else set()
                            ),
                        ),
                    )
                    self.machine.ingest(key, incoming)
                    observation = self._observation_pulse(
                        session,
                        process,
                        incoming,
                        rollout_activity,
                        session.network,
                        None,
                        full_sample=False,
                    )
                    session = self.machine.derive(
                        key,
                        process,
                        session.network,
                        observation=observation,
                    )
                    if (
                        session.observation.last_evidence_at is not None
                        and session.observation.last_evidence_at
                        > (self.live_sessions.get(key, session).observation.last_evidence_at or 0.0)
                    ):
                        self.machine.observe_compaction(
                            key,
                            timestamp=session.observation.last_evidence_at,
                            source=session.observation.last_evidence_source or "observation",
                            detail=session.observation.last_evidence_detail,
                        )
                        session = self.machine.derive(
                            key,
                            process,
                            session.network,
                            observation=observation,
                        )
                    self.live_sessions[key] = session
                if incoming or rollout_activity.changed or terminal_changed:
                    session = self._attach_terminal_snapshot(session, key)
                    session = self._attach_ingress_diagnosis(session, rollout_activity)
                refreshed_sessions.append(session)
                refreshed_processes[process.stable_key] = session.process

            processes = [
                refreshed_processes.get(process.stable_key, process)
                for process in instance.processes
            ]
            refreshed_instances.append(
                replace(
                    instance,
                    protocol_capabilities=self._merge_protocol_capabilities(refreshed_sessions),
                    unknown_event_types=self.rollouts.unknown_counts(rollout_paths),
                    protocol_shape_families=self.rollouts.shape_counts(rollout_paths),
                    protocol_family_counters=self.rollouts.family_counter_summary(rollout_paths),
                    rollout_context_truncated=(self.rollouts.has_truncated_context(rollout_paths)),
                    rollout_activity=rollout_activity_values,
                    processes=processes,
                    sessions=refreshed_sessions,
                )
            )

        diagnostics = list(snapshot.diagnostics)
        if not changed:
            return snapshot
        completed_at = time.time()
        temporal = build_temporal_cut(
            refreshed_instances,
            snapshot.collector_health,
            now=completed_at,
            interval=self.interval,
            generation=snapshot.temporal.sample_generation + 1,
            previous=snapshot.temporal,
            fast=True,
        )
        refreshed_instances = apply_temporal_completeness(refreshed_instances, temporal)
        return replace(
            snapshot,
            generated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            instances=refreshed_instances,
            collection_duration_seconds=time.monotonic() - started,
            diagnostics=diagnostics,
            temporal=temporal,
        )
