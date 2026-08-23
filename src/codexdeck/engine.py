"""Persistent multi-instance sampling engine."""

from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import fields, replace
from pathlib import Path

from codexdeck.codex.config_reader import CodexConfigReader
from codexdeck.codex.file_tail import RegularFileTailCollector
from codexdeck.codex.paths import ProcReader, open_rollout_paths
from codexdeck.codex.process_activity import ProcessActivityCollector
from codexdeck.codex.processes import DiscoveryResult, ProcessDiscovery
from codexdeck.codex.rollout import (
    RolloutActivity,
    RolloutReader,
    latest_user_task,
    rollout_identity,
)
from codexdeck.codex.state_store import StateStore
from codexdeck.codex.terminal import TerminalStore
from codexdeck.diagnostics import CollectorTracker
from codexdeck.engine_collectors import (
    CollectorStagesMixin,
)
from codexdeck.engine_refresh import FastRefreshMixin
from codexdeck.engine_sampling import InstanceSamplingMixin
from codexdeck.models import (
    AxisCompleteness,
    CodexPaths,
    Confidence,
    DiagnosisFinding,
    EvidenceCoverage,
    InstanceIdentity,
    InstanceIdentityRegistry,
    InstanceSnapshot,
    MonitorSnapshot,
    NetworkEvidence,
    NetworkState,
    NormalizedEvent,
    ObservationPulse,
    ProcessInfo,
    ProtocolCapabilities,
    RolloutIdentity,
    SessionHealth,
    SessionIdentity,
)
from codexdeck.network.sockets import SocketCollector
from codexdeck.snapshot_publisher import SnapshotPublisher
from codexdeck.state_machine import SessionStateMachine
from codexdeck.utils import CommandError, one_line


class MonitorEngine(FastRefreshMixin, CollectorStagesMixin, InstanceSamplingMixin):
    INITIAL_BACKLOG_DRAIN_PASSES = 128

    def __init__(
        self,
        interval: float,
        idle_threshold: float,
        event_lookback: int,
        selected_pids: set[int] | None = None,
        selected_homes: set[Path] | None = None,
        discovery: ProcessDiscovery | None = None,
        sockets: SocketCollector | None = None,
        proc: ProcReader | None = None,
        process_activity: ProcessActivityCollector | None = None,
    ) -> None:
        self.interval = interval
        self.idle_threshold = idle_threshold
        self.selected_pids = selected_pids
        self.selected_homes = selected_homes
        self.proc = proc or ProcReader()
        self.discovery = discovery or ProcessDiscovery(proc=self.proc)
        self.sockets = sockets or SocketCollector()
        self.rollouts = RolloutReader()
        self.process_activity = process_activity or ProcessActivityCollector(
            getattr(self.proc, "root", Path("/proc"))
        )
        self.codex_configs = CodexConfigReader()
        self.machine = SessionStateMachine(event_lookback)
        self.previous_sockets: dict[str, list] = {}
        self.stall_windows: dict[str, int] = defaultdict(int)
        self.log_cursors: dict[InstanceIdentity, int] = defaultdict(int)
        self.log_process_keys: dict[InstanceIdentity, set[str]] = {}
        self.session_index_cache: dict[InstanceIdentity, tuple[int, int, dict[str, str]]] = {}
        self.task_cache: dict[str, tuple[int, int, int, str]] = {}
        self.rollout_path_cache: dict[str, tuple[Path | None, str]] = {}
        self.store_cache: dict[InstanceIdentity, StateStore] = {}
        self.live_sessions: dict[SessionIdentity, SessionHealth] = {}
        self.terminals = TerminalStore()
        self.terminal_files = RegularFileTailCollector(getattr(self.proc, "root", Path("/proc")))
        self.retired_sessions: dict[SessionIdentity, tuple[SessionHealth, float]] = {}
        self.instance_templates: dict[InstanceIdentity, InstanceSnapshot] = {}
        self.identity_registry = InstanceIdentityRegistry()
        self.pinned_session_key: SessionIdentity | None = None
        self.last_discovery: DiscoveryResult | None = None
        self.last_socket_by_pid: dict[int, list] = {}
        self.discovery_stale_since: float | None = None
        self.socket_stale_since: float | None = None
        self.collectors = CollectorTracker(interval)
        self.snapshot_publisher = SnapshotPublisher(interval, self.collectors)

    def baseline(self) -> None:
        """Capture a cheap first socket baseline before a completed sample window."""

        try:
            discovery = self.discovery.discover(self.selected_pids, self.selected_homes)
            current = self.sockets.snapshot({process.pid for process in discovery.processes})
        except CommandError:
            return
        self.last_discovery = discovery
        self.last_socket_by_pid = current
        self.previous_sockets = {
            process.stable_key: current.get(process.pid, []) for process in discovery.processes
        }

    def sample(self) -> MonitorSnapshot:
        """Run the full collector pipeline and publish one coherent snapshot."""

        return self._collect_full_sample()

    def prepare_initial_snapshot(self) -> MonitorSnapshot:
        """Collect and catch up the bounded rollout tail before first publication."""

        self.baseline()
        snapshot = self.sample()
        for _ in range(self.INITIAL_BACKLOG_DRAIN_PASSES):
            pending = any(
                self.machine.coverage_backlog.get(session.session_identity, False)
                for session in snapshot.sessions
            )
            if not pending:
                break
            previous_offsets = tuple(
                sorted((path, cursor.offset) for path, cursor in self.rollouts.cursors.items())
            )
            snapshot = self.refresh_events(snapshot)
            current_offsets = tuple(
                sorted((path, cursor.offset) for path, cursor in self.rollouts.cursors.items())
            )
            if current_offsets == previous_offsets:
                break
        return snapshot

    def _collect_full_sample(self) -> MonitorSnapshot:
        started = time.monotonic()
        now_monotonic = started
        diagnostics: list[str] = []
        discovery_stage = self._collect_discovery_stage(now_monotonic, diagnostics)
        socket_stage = self._collect_socket_stage(
            discovery_stage.result,
            now_monotonic,
            diagnostics,
        )
        instance_snapshots: list[InstanceSnapshot] = []
        active_session_keys: set[SessionIdentity] = set()
        active_rollouts: set[str] = set()
        for instance_identity, processes in discovery_stage.by_instance.items():
            instance, instance_sessions, instance_rollouts = self._collect_instance_snapshot(
                instance_identity=instance_identity,
                processes=processes,
                discovery_stage=discovery_stage,
                socket_stage=socket_stage,
                now_monotonic=now_monotonic,
            )
            instance_snapshots.append(instance)
            active_session_keys.update(instance_sessions)
            active_rollouts.update(instance_rollouts)

        self._retain_exited_sessions(instance_snapshots, active_session_keys)
        self._prune_full_sample_state(
            by_instance=set(discovery_stage.by_instance),
            active_process_keys=discovery_stage.active_process_keys,
            active_session_keys=active_session_keys,
            active_rollouts=active_rollouts,
        )
        return self.snapshot_publisher.publish(
            instances=instance_snapshots,
            started=started,
            now_monotonic=now_monotonic,
            diagnostics=diagnostics,
            discovery=discovery_stage.result.summary,
            discovery_stale_since=self.discovery_stale_since,
            socket_stale_since=self.socket_stale_since,
        )

    def _prune_full_sample_state(
        self,
        *,
        by_instance: set[InstanceIdentity],
        active_process_keys: set[str],
        active_session_keys: set[SessionIdentity],
        active_rollouts: set[str],
    ) -> None:
        """Drop mutable collector state that no longer belongs to an active sample."""

        self.previous_sockets = {
            key: value for key, value in self.previous_sockets.items() if key in active_process_keys
        }
        self.stall_windows = defaultdict(
            int,
            {key: value for key, value in self.stall_windows.items() if key in active_process_keys},
        )
        self.log_cursors = defaultdict(
            int, {key: value for key, value in self.log_cursors.items() if key in by_instance}
        )
        self.log_process_keys = {
            key: value for key, value in self.log_process_keys.items() if key in by_instance
        }
        self.session_index_cache = {
            key: value for key, value in self.session_index_cache.items() if key in by_instance
        }
        for instance_id in set(self.store_cache) - by_instance:
            self.store_cache.pop(instance_id).close()
        self.rollouts.prune(active_rollouts)
        active_scopes: set[str | RolloutIdentity] = {""}
        for path, cursor in self.rollouts.cursors.items():
            active_scopes.add(
                RolloutIdentity(Path(path), cursor.device, cursor.inode, cursor.generation)
            )
        self.process_activity.prune(active_process_keys)
        self.task_cache = {
            path: value for path, value in self.task_cache.items() if path in active_rollouts
        }
        retained_keys = set(self.retired_sessions)
        active_or_retained: set[str | SessionIdentity] = set(active_session_keys)
        active_or_retained.update(retained_keys)
        self.machine.prune(active_or_retained)
        self.terminals.prune(active_or_retained)
        self.terminal_files.prune(set(active_session_keys))
        active_scopes.update(self.terminal_files.active_scopes())
        self.terminals.prune_scopes(active_scopes)
        self.rollout_path_cache = {
            key: value
            for key, value in self.rollout_path_cache.items()
            if key in active_process_keys
        }

    @staticmethod
    def _rollout_activity_value(activity: RolloutActivity) -> dict[str, object]:
        return {
            "path": activity.path,
            "observed_at": activity.observed_at,
            "available": activity.available,
            "stat_size": activity.stat_size,
            "mtime_ns": activity.mtime_ns,
            "bytes_read": activity.bytes_read,
            "complete_record_count": activity.complete_record_count,
            "record_count": activity.record_count,
            "ignored_record_count": activity.ignored_record_count,
            "normalized_count": activity.normalized_count,
            "partial_bytes": activity.partial_bytes,
            "last_growth_at": activity.last_growth_at,
            "replaced": activity.replaced,
            "truncated": activity.truncated,
            "copy_truncated": activity.copy_truncated,
            "consumed_bytes": activity.consumed_bytes,
            "backlog_bytes": activity.backlog_bytes,
            "backlog_records_lower_bound": activity.backlog_records_lower_bound,
            "backlog_age_seconds": activity.backlog_age_seconds,
            "budget_exceeded": activity.budget_exceeded,
            "oversize_record_count": activity.oversize_record_count,
            "skipped_bytes": activity.skipped_bytes,
            "gap_count": activity.gap_count,
            "gap_reason": activity.gap_reason,
            "gap_hash": activity.gap_hash,
            "parse_duration_seconds": activity.parse_duration_seconds,
            "metadata_backfill_dropped": activity.metadata_backfill_dropped,
            "metadata_backfill_reason": activity.metadata_backfill_reason,
            "terminal_parser_evictions": activity.terminal_parser_evictions,
            "terminal_parser_eviction_reason": activity.terminal_parser_eviction_reason,
            "device": activity.device,
            "inode": activity.inode,
            "generation": activity.generation,
            "anchor_hash": activity.anchor_hash,
            "stream_uncertain": activity.stream_uncertain,
            "stream_uncertainty_count": activity.stream_uncertainty_count,
            "stream_uncertainty_reason": activity.stream_uncertainty_reason,
        }

    @staticmethod
    def _evidence_coverage(
        activities: list[RolloutActivity],
        *,
        bootstrap_truncated: bool,
        track_source: bool = True,
        terminal_probe_complete: bool | None = None,
        network_probe_complete: bool | None = None,
        silence_probe_complete: bool | None = None,
    ) -> EvidenceCoverage:
        observed_at = max((item.observed_at for item in activities), default=time.time())
        source_epoch = ""
        if track_source:
            source_epoch = "|".join(
                sorted(
                    f"{item.device}:{item.inode}:{item.generation}"
                    for item in activities
                    if item.inode
                )
            )
        gap_count = sum(item.gap_count for item in activities) if track_source else 0
        if bootstrap_truncated and gap_count:
            # Starting a bounded tail in the middle of the first retained JSONL
            # record is an expected history boundary, not a runtime ingress gap.
            gap_count -= 1
        return EvidenceCoverage(
            observed_at=observed_at,
            source_epoch=source_epoch,
            bootstrap_truncated=(bootstrap_truncated if track_source and source_epoch else False),
            gap_count=gap_count,
            generation_changed=track_source and any(item.replaced for item in activities),
            copy_truncated=track_source
            and any(item.copy_truncated or item.truncated for item in activities),
            stream_uncertainty_count=(
                sum(item.stream_uncertainty_count for item in activities) if track_source else 0
            ),
            backlog_pending=any(item.backlog_bytes for item in activities),
            terminal_probe_complete=terminal_probe_complete,
            network_probe_complete=network_probe_complete,
            silence_probe_complete=silence_probe_complete,
        )

    @staticmethod
    def _observation_pulse(
        previous: SessionHealth | None,
        process: ProcessInfo,
        incoming: list[NormalizedEvent],
        rollout: RolloutActivity,
        network: NetworkEvidence,
        log_activity_at: float | None,
        *,
        full_sample: bool,
        process_stale: bool = False,
        network_stale: bool = False,
    ) -> ObservationPulse:
        now = rollout.observed_at
        base = previous.observation if previous else ObservationPulse()
        semantic = next(
            (
                event
                for event in reversed(incoming)
                if event.kind
                not in {
                    "KEEPALIVE",
                    "TOKEN_USAGE",
                    "RATE_LIMIT",
                    "MODEL_CONFIG",
                    "UNPARSED_PAYLOAD",
                }
            ),
            None,
        )
        rollout_growth_at = (
            rollout.last_growth_at if rollout.changed else base.last_rollout_growth_at
        )
        process_activity_at = base.last_process_activity_at
        if full_sample and process.activity.active:
            process_activity_at = process.activity.sampled_at or now
        network_delta = 0
        if full_sample:
            network_delta = sum(
                item.sent_delta + item.received_delta + item.acked_delta
                for item in network.connections
            )
        network_progress_at = base.last_network_progress_at
        if network_delta:
            network_progress_at = now
        last_log_activity_at = log_activity_at or base.last_log_activity_at
        direct_activity = bool(
            semantic
            or rollout.changed
            or (full_sample and process.activity.active)
            or network_delta
            or log_activity_at
        )
        quiet_samples = base.quiet_full_samples
        if direct_activity:
            quiet_samples = 0
        elif full_sample:
            quiet_samples += 1
        process_probe = (
            process.activity.sampled_at
            if full_sample and process.activity.available
            else base.process_probe_at
        )
        network_probe = now if full_sample and not network_stale else base.network_probe_at
        log_probe = now if full_sample else base.log_probe_at
        rollout_probe = rollout.observed_at if rollout.available else base.rollout_probe_at
        probes = [
            value
            for value in (rollout_probe, process_probe, network_probe, log_probe)
            if value is not None
        ]
        stale_sources = []
        if process_stale or (full_sample and not process.activity.available):
            stale_sources.append("process")
        if network_stale:
            stale_sources.append("network")
        if process.rollout_path and not rollout.available:
            stale_sources.append("rollout")
        return replace(
            base,
            sampled_at=now,
            last_semantic_at=(semantic.timestamp if semantic else base.last_semantic_at),
            last_semantic_kind=(semantic.kind if semantic else base.last_semantic_kind),
            last_semantic_source=(semantic.source if semantic else base.last_semantic_source),
            last_rollout_growth_at=rollout_growth_at,
            last_process_activity_at=process_activity_at,
            last_network_progress_at=network_progress_at,
            last_log_activity_at=last_log_activity_at,
            last_probe_at=max(probes, default=base.last_probe_at),
            rollout_probe_at=rollout_probe,
            process_probe_at=process_probe,
            network_probe_at=network_probe,
            log_probe_at=log_probe,
            rollout_partial_bytes=rollout.partial_bytes,
            rollout_bytes_delta=rollout.bytes_read if rollout.changed else 0,
            process_activity=process.activity,
            network_bytes_delta=network_delta,
            quiet_full_samples=quiet_samples,
            collector_stale=bool(stale_sources),
            collector_stale_reason=(
                f"监测证据不足：{', '.join(stale_sources)} collector 数据不可用"
                if stale_sources
                else ""
            ),
        )

    @staticmethod
    def _with_compact_config(
        events: list[NormalizedEvent],
        auto_compact_token_limit: int | None,
        auto_compact_token_limit_scope: str = "",
    ) -> list[NormalizedEvent]:
        if auto_compact_token_limit is None:
            return events
        return [
            replace(
                event,
                metadata={
                    **event.metadata,
                    "auto_compact_token_limit": auto_compact_token_limit,
                    "auto_compact_token_limit_scope": auto_compact_token_limit_scope,
                },
            )
            if event.kind
            in {
                "TOKEN_USAGE",
                "COMPACT_REQUESTED",
                "COMPACT_CANDIDATE",
                "COMPACTING",
                "COMPACT_COMPLETED",
                "COMPACT_FAILED",
                "COMPACT_ABORTED",
            }
            else event
            for event in events
        ]

    @staticmethod
    def _merge_protocol_capabilities(
        sessions: list[SessionHealth],
    ) -> ProtocolCapabilities:
        rank = {"unavailable": 0, "derived": 1, "direct": 2}
        merged = {}
        defaults = ProtocolCapabilities()
        for descriptor in fields(ProtocolCapabilities):
            statuses = [
                getattr(session.protocol_capabilities, descriptor.name) for session in sessions
            ]
            merged[descriptor.name] = max(
                statuses,
                key=lambda status: rank[status.mode.value],
                default=getattr(defaults, descriptor.name),
            )
        return ProtocolCapabilities(**merged)

    def _fallback_rollout(
        self,
        process: ProcessInfo,
        sessions_dir: Path,
    ) -> tuple[Path | None, str]:
        # A long-lived Codex TUI keeps the same PID when `/new` replaces the
        # conversation. Re-check its open rollout descriptors on every full
        # sample; a path-only cache would pin the process to the old session.
        candidates: list[tuple[bool, int, int, Path, str]] = []
        for path in open_rollout_paths(process.pid, sessions_dir, self.proc):
            session_id, is_subagent = rollout_identity(path)
            try:
                stat = path.stat()
            except OSError:
                modified_at = 0
                size = 0
            else:
                modified_at = stat.st_mtime_ns
                size = stat.st_size
            candidates.append((not is_subagent, modified_at, size, path, session_id))
        if not candidates:
            cached = self.rollout_path_cache.get(process.stable_key)
            if cached and cached[0] is not None and cached[0].exists():
                return cached
            result: tuple[Path | None, str] = (None, "")
            self.rollout_path_cache[process.stable_key] = result
            return result
        _, _, _, path, session_id = max(
            candidates,
            key=lambda item: (item[0], item[1], item[2], str(item[3])),
        )
        result = (path, session_id)
        self.rollout_path_cache[process.stable_key] = result
        return result

    def _store_for(self, identity: InstanceIdentity, paths: CodexPaths) -> StateStore:
        store = self.store_cache.get(identity)
        if store and store.paths == paths and store.is_current():
            return store
        if store:
            store.close()
            self.log_cursors[identity] = 0
        store = StateStore(paths)
        self.store_cache[identity] = store
        return store

    def _latest_task(self, path: Path) -> str:
        key = str(path)
        try:
            stat = path.stat()
        except OSError:
            return ""
        signature = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
        cached = self.task_cache.get(key)
        if cached and cached[:3] == signature:
            return cached[3]
        task = latest_user_task(path)
        self.task_cache[key] = (*signature, task)
        return task

    def _retain_exited_sessions(
        self,
        snapshots: list[InstanceSnapshot],
        active_keys: set[SessionIdentity],
    ) -> None:
        """Keep recently exited sessions visible without treating them as unhealthy."""

        now = time.time()
        current: dict[SessionIdentity, SessionHealth] = {}
        snapshot_by_instance = {snapshot.instance_identity: snapshot for snapshot in snapshots}
        for snapshot in snapshots:
            self.instance_templates[snapshot.instance_identity] = replace(
                snapshot,
                processes=[],
                sessions=[],
            )
            for session in snapshot.sessions:
                key = session.session_identity
                current[key] = session
                self.retired_sessions.pop(key, None)

        for key, session in self.live_sessions.items():
            if key in active_keys:
                continue
            replacement = next(
                (
                    candidate
                    for candidate in current.values()
                    if candidate.process.stable_key == session.process.stable_key
                    and candidate.session_id != session.session_id
                ),
                None,
            )
            if replacement is not None:
                summary = "会话已由 /new 关闭"
                detail = f"PID {session.process.pid} 已切换到新会话 {replacement.session_id[:8]}"
                network_reason = "当前 Codex 窗口已切换到新会话"
                source_id = (
                    f"session-replaced:{session.process.stable_key}:"
                    f"{session.session_id}:{replacement.session_id}"
                )
            else:
                summary = "进程已退出"
                detail = f"PID {session.process.pid} 已结束"
                network_reason = "Codex 进程已退出"
                source_id = f"process-exited:{session.process.stable_key}"
            exited = NormalizedEvent(
                timestamp=now,
                kind="SESSION_CLOSED" if replacement is not None else "PROCESS_EXITED",
                summary=summary,
                detail=detail,
                source="process",
                confidence=Confidence.HIGH,
                source_id=source_id,
                observed_at=now,
            )
            self.machine.ingest(key, [exited])
            network = NetworkEvidence(
                state=NetworkState.CLOSED,
                reason=network_reason,
            )
            retained = self.machine.derive(
                key,
                session.process,
                network,
                now,
                observation=session.observation,
            )
            self.terminals.mark_stale(key)
            retained = self._attach_terminal_snapshot(retained, key)
            self.retired_sessions[key] = (retained, now)

        expiry = now - self.machine.lookback_seconds
        self.retired_sessions = {
            key: value
            for key, value in self.retired_sessions.items()
            if (value[1] >= expiry or key == self.pinned_session_key) and key not in active_keys
        }
        for retained, _ in self.retired_sessions.values():
            identity = retained.session_identity.instance
            target_snapshot = snapshot_by_instance.get(identity)
            if target_snapshot is None:
                template = self.instance_templates.get(identity)
                if template is None:
                    continue
                target_snapshot = replace(template, sessions=[])
                snapshots.append(target_snapshot)
                snapshot_by_instance[identity] = target_snapshot
            target_snapshot.sessions.append(retained)
        self.live_sessions = current
        retained_instances = {
            session.session_identity.instance for session, _ in self.retired_sessions.values()
        }
        visible_instances = set(snapshot_by_instance) | retained_instances
        self.instance_templates = {
            key: value for key, value in self.instance_templates.items() if key in visible_instances
        }

    def close(self) -> None:
        for store in self.store_cache.values():
            store.close()
        self.store_cache.clear()

    def _attach_terminal_snapshot(
        self,
        session: SessionHealth,
        key: str | SessionIdentity,
    ) -> SessionHealth:
        association = self.terminals.association_summary(key)
        diagnosis = [
            item
            for item in session.diagnosis
            if item.conclusion not in {"Terminal 关联不完整", "进程发现依据为间接证据"}
        ]
        if session.process.discovery_confidence != Confidence.HIGH:
            diagnosis.append(
                DiagnosisFinding(
                    "warning",
                    "进程发现依据为间接证据",
                    (
                        f"method={session.process.discovery_method}; "
                        f"confidence={session.process.discovery_confidence.value}"
                    ),
                    session.process.discovery_evidence,
                )
            )
        if (
            association.ambiguous
            or association.conflicting
            or association.unresolved
            or association.private_state_dropped
        ):
            diagnosis.append(
                DiagnosisFinding(
                    "warning",
                    "Terminal 关联不完整",
                    (
                        f"eligible={association.eligible_operations}; "
                        f"confirmed={association.confirmed}; ambiguous={association.ambiguous}; "
                        f"conflicting={association.conflicting}; "
                        f"unresolved={association.unresolved}; dropped={association.dropped}; "
                        f"private_dropped={association.private_state_dropped}; "
                        f"private_evictions={association.private_state_evictions}; "
                        f"private_recoveries={association.private_state_recoveries}"
                    ),
                    tuple(
                        f"{reason}={count}"
                        for reason, count in (
                            *association.reasons,
                            *association.private_state_reasons,
                        )
                    ),
                )
            )
            completeness = replace(
                session.completeness,
                terminal_ownership=AxisCompleteness(
                    "terminal_ownership",
                    complete=False,
                    confidence=Confidence.LOW,
                    reason="terminal 关联存在 ambiguous/conflicting/unresolved/private-state drop",
                    baseline_kind="terminal_association_incomplete",
                    evidence=tuple(
                        f"{reason}={count}"
                        for reason, count in (
                            *association.reasons,
                            *association.private_state_reasons,
                        )
                    )[:24],
                ),
            )
        else:
            completeness = session.completeness
        return replace(
            session,
            terminal_sessions=self.terminals.current_summaries(key),
            terminal_association=association,
            completeness=completeness,
            diagnosis=diagnosis,
        )

    @staticmethod
    def _attach_ingress_diagnosis(
        session: SessionHealth,
        rollout: RolloutActivity,
    ) -> SessionHealth:
        diagnosis = [
            item for item in session.diagnosis if item.conclusion != "Rollout 入口积压或缺口"
        ]
        if (
            rollout.backlog_bytes
            or rollout.gap_count
            or rollout.metadata_backfill_dropped
            or rollout.terminal_parser_evictions
            or rollout.stream_uncertain
        ):
            diagnosis.append(
                DiagnosisFinding(
                    "warning",
                    "Rollout 入口积压或缺口",
                    (
                        f"backlog_bytes={rollout.backlog_bytes}; "
                        f"backlog_age={rollout.backlog_age_seconds}; "
                        f"budget_exceeded={rollout.budget_exceeded}; "
                        f"gap_count={rollout.gap_count}; skipped_bytes={rollout.skipped_bytes}; "
                        f"metadata_backfill_dropped={rollout.metadata_backfill_dropped}; "
                        f"metadata_reason={rollout.metadata_backfill_reason or '-'}"
                        f"; terminal_parser_evictions={rollout.terminal_parser_evictions}"
                        f"; generation={rollout.generation}; "
                        f"stream_uncertain={rollout.stream_uncertain}"
                    ),
                    tuple(
                        value
                        for value in (
                            f"reason={rollout.gap_reason}" if rollout.gap_reason else "",
                            f"hash={rollout.gap_hash}" if rollout.gap_hash else "",
                            (
                                f"metadata_reason={rollout.metadata_backfill_reason}"
                                if rollout.metadata_backfill_reason
                                else ""
                            ),
                            (
                                f"stream_reason={rollout.stream_uncertainty_reason}"
                                if rollout.stream_uncertainty_reason
                                else ""
                            ),
                            (
                                f"terminal_parser_reason={rollout.terminal_parser_eviction_reason}"
                                if rollout.terminal_parser_eviction_reason
                                else ""
                            ),
                        )
                        if value
                    ),
                )
            )
        return replace(session, diagnosis=diagnosis)

    def pin_session(self, session: SessionHealth | None) -> None:
        """Retain the selected session timeline while the TUI references it."""

        self.pinned_session_key = session.session_identity if session else None

    @staticmethod
    def _bounded(value: str, limit: int) -> str:
        return value if len(value) <= limit else value[: limit - 1] + "…"

    def _session_names(self, identity: InstanceIdentity, path: Path) -> dict[str, str]:
        try:
            stat = path.stat()
        except OSError:
            return {}
        cached = self.session_index_cache.get(identity)
        signature = (stat.st_ino, stat.st_mtime_ns)
        if cached and cached[:2] == signature:
            return cached[2]
        names: dict[str, str] = {}
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    session_id = str(record.get("id") or "")
                    name = one_line(str(record.get("thread_name") or ""))
                    if session_id and name:
                        names[session_id] = name
        except OSError:
            return {}
        self.session_index_cache[identity] = (signature[0], signature[1], names)
        return names
