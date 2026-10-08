"""Per-instance full-sample collection stages."""

from __future__ import annotations

import re
import time
from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from codexdeck.codex.config_reader import CodexConfigSnapshot
from codexdeck.codex.events import normalize_log
from codexdeck.codex.paths import ResolvedInstance, open_rollout_paths
from codexdeck.codex.rollout import RolloutActivity, rollout_identity
from codexdeck.codex.state_store import StateStore, ThreadRecord
from codexdeck.diagnostics import make_diagnostic
from codexdeck.engine_collectors import DiscoveryStage, SocketStage
from codexdeck.models import (
    AdapterResult,
    AdapterStatus,
    Confidence,
    Diagnostic,
    InstanceIdentity,
    InstanceSnapshot,
    NetworkEvidence,
    NetworkState,
    NormalizedEvent,
    ProcessInfo,
    SessionHealth,
    SessionIdentity,
)
from codexdeck.network.classifier import assess_process_network, confirm_process_stall
from codexdeck.state_machine import PROGRESS_KINDS
from codexdeck.utils import compact_path, one_line


@dataclass
class InstanceIdentityStage:
    store: StateStore
    adapter_results: list[AdapterResult]
    sessions_by_pid: dict[int, str]
    rollout_by_pid: dict[int, Path | None]
    records: dict[str, ThreadRecord]
    names: dict[str, str]


@dataclass
class InstanceProcessStage:
    store: StateStore
    adapter_results: list[AdapterResult]
    processes: list[ProcessInfo]
    rollout_paths: set[str]


@dataclass
class InstanceLogStage:
    events_by_session: dict[str, list[NormalizedEvent]]
    activity_by_session: dict[str, float]


@dataclass
class InstanceSessionStage:
    sessions: list[SessionHealth]
    rollout_activity: list[dict[str, object]]
    active_session_keys: set[SessionIdentity]


@dataclass
class SessionEvidenceStage:
    incoming: list[NormalizedEvent]
    rollout_activities: list[RolloutActivity]
    rollout_activity: RolloutActivity
    process_tree_available: bool


class InstanceSamplingMixin:
    """Collect process, log, rollout, terminal, and state stages for one instance."""

    interval: float
    idle_threshold: float
    codex_configs: Any
    collectors: Any
    log_process_keys: dict[Any, set[str]]
    log_cursors: dict[Any, int]
    live_sessions: dict[Any, SessionHealth]
    process_activity: Any
    machine: Any
    rollouts: Any
    terminals: Any
    terminal_files: Any
    retired_sessions: dict[Any, tuple[SessionHealth, float]]
    previous_sockets: dict[str, list[Any]]
    stall_windows: dict[str, int]
    socket_stale_since: float | None
    discovery_stale_since: float | None
    _store_for: Any
    _fallback_rollout: Any
    _session_names: Any
    _latest_task: Any
    _bounded: Any
    _with_compact_config: Any
    _rollout_activity_value: Any
    _evidence_coverage: Any
    _observation_pulse: Any
    _attach_terminal_snapshot: Any
    _attach_ingress_diagnosis: Any
    _merge_protocol_capabilities: Any
    proc: Any

    def _expand_session_owners(
        self, processes: list[ProcessInfo], sessions_dir: Path,
    ) -> list[ProcessInfo]:
        """Keep every open thread of a shared app-server, not just its newest file."""

        expanded: list[ProcessInfo] = []
        for process in processes:
            if process.role != "app-server":
                expanded.append(process)
                continue
            owners = []
            for path in open_rollout_paths(process.pid, sessions_dir, self.proc):
                session_id, _ = rollout_identity(path)
                if session_id:
                    owners.append(replace(
                        process, session_id=session_id, rollout_path=str(path),
                    ))
            expanded.extend(owners or [process])
        return expanded

    def _resolve_instance_identity(
        self,
        *,
        instance_identity: InstanceIdentity,
        instance_id: str,
        processes: list[ProcessInfo],
        resolved: ResolvedInstance,
        instance_diagnostics: list[Diagnostic | str],
    ) -> InstanceIdentityStage:
        state_started = time.monotonic()
        store = self._store_for(instance_identity, resolved.paths)
        adapter_results: list[AdapterResult] = list(store.initialization_results)
        process_keys = {process.stable_key for process in processes}
        if self.log_process_keys.get(instance_identity) != process_keys:
            self.log_cursors[instance_identity] = 0
            self.log_process_keys[instance_identity] = process_keys
        if not store.capabilities.threads:
            instance_diagnostics.append("state DB 不可用或缺少 threads 表")
        if not store.capabilities.logs:
            instance_diagnostics.append("logs DB 不可用，重试诊断可能不完整")

        sessions_by_pid: dict[int, str] = {}
        rollout_by_pid: dict[int, Path | None] = {}
        for process in processes:
            if process.role != "session":
                continue
            rollout_path, session_id = self._fallback_rollout(process, resolved.paths.sessions_dir)
            rollout_by_pid[process.pid] = rollout_path
            if session_id:
                sessions_by_pid[process.pid] = session_id
        unresolved = [
            process.pid
            for process in processes
            if process.role == "session" and process.pid not in sessions_by_pid
        ]
        active_threads_result = store.active_threads_result(
            unresolved,
            cutoff=int(time.time()) - 21600,
        )
        adapter_results.append(active_threads_result)
        if active_threads_result.status in {
            AdapterStatus.PRESENT,
            AdapterStatus.INCOMPLETE,
        }:
            sessions_by_pid.update(dict(active_threads_result.value or {}))
        threads_result = store.threads_result(sessions_by_pid.values())
        adapter_results.append(threads_result)
        records = (
            dict(threads_result.value or {})
            if threads_result.status
            in {
                AdapterStatus.PRESENT,
                AdapterStatus.ABSENT,
                AdapterStatus.INCOMPLETE,
            }
            else {}
        )
        self.collectors.record(
            f"state_db:{instance_id}",
            state_started,
            (
                threads_result.error_code
                if threads_result.status in {AdapterStatus.FAILED, AdapterStatus.INCOMPLETE}
                else None
                if store.capabilities.threads
                else "sqlite_unsupported"
            ),
        )
        names = self._session_names(instance_identity, resolved.paths.session_index)
        return InstanceIdentityStage(
            store=store,
            adapter_results=adapter_results,
            sessions_by_pid=sessions_by_pid,
            rollout_by_pid=rollout_by_pid,
            records=records,
            names=names,
        )

    def _enrich_instance_processes(
        self,
        *,
        instance_identity: InstanceIdentity,
        processes: list[ProcessInfo],
        resolved: ResolvedInstance,
        identity_stage: InstanceIdentityStage,
    ) -> InstanceProcessStage:
        active_rollouts: set[str] = set()
        store = identity_stage.store
        adapter_results = identity_stage.adapter_results
        sessions_by_pid = identity_stage.sessions_by_pid
        rollout_by_pid = identity_stage.rollout_by_pid
        records = identity_stage.records
        names = identity_stage.names
        enriched: list[ProcessInfo] = []
        for process in self._expand_session_owners(processes, resolved.paths.sessions_dir):
            if process.role == "app-server" and not process.session_id:
                enriched.append(process)
                continue
            session_id = process.session_id or sessions_by_pid.get(process.pid, "")
            record = records.get(session_id)
            rollout_path = (
                Path(process.rollout_path) if process.rollout_path
                else rollout_by_pid.get(process.pid)
            )
            if session_id and record is None:
                thread_result = store.threads_result([session_id])
                adapter_results.append(thread_result)
                record = dict(thread_result.value or {}).get(session_id)
            if not rollout_path and record and record.rollout_path:
                rollout_path = Path(record.rollout_path)
            if not rollout_path:
                rollout_path, fallback_id = self._fallback_rollout(
                    process,
                    resolved.paths.sessions_dir,
                )
                session_id = session_id or fallback_id
                if session_id and record is None:
                    fallback_result = store.threads_result([session_id])
                    adapter_results.append(fallback_result)
                    if fallback_result.status in {
                        AdapterStatus.PRESENT,
                        AdapterStatus.INCOMPLETE,
                    }:
                        record = dict(fallback_result.value or {}).get(session_id)
            title = names.get(session_id, "") or (record.title if record else "")
            fallback_task = (record.preview or record.first_user_message) if record else ""
            task = self._latest_task(rollout_path) if rollout_path else ""
            session_identity = SessionIdentity(instance_identity, session_id)
            previous = self.live_sessions.get(session_identity)
            process = replace(
                process,
                instance_identity=instance_identity,
                cwd=(record.cwd if record and record.cwd else process.cwd),
                session_id=session_id,
                session_title=self._bounded(one_line(title), 120),
                current_task=self._bounded(one_line(task or fallback_task), 240),
                model=(
                    record.model
                    if record and record.model
                    else previous.process.model
                    if previous
                    else ""
                ),
                reasoning_effort=(
                    record.reasoning_effort
                    if record and record.reasoning_effort
                    else previous.process.reasoning_effort
                    if previous
                    else ""
                ),
                rollout_path=str(rollout_path or ""),
                activity=(
                    self.process_activity.snapshot(process.identity)
                    if process.role == "session"
                    else process.activity
                ),
            )
            enriched.append(process)
            if process.rollout_path:
                active_rollouts.add(process.rollout_path)

        return InstanceProcessStage(
            store=store,
            adapter_results=adapter_results,
            processes=enriched,
            rollout_paths=active_rollouts,
        )

    def _prepare_instance_processes(
        self,
        *,
        instance_identity: InstanceIdentity,
        instance_id: str,
        processes: list[ProcessInfo],
        resolved: ResolvedInstance,
        instance_diagnostics: list[Diagnostic | str],
    ) -> InstanceProcessStage:
        identity_stage = self._resolve_instance_identity(
            instance_identity=instance_identity,
            instance_id=instance_id,
            processes=processes,
            resolved=resolved,
            instance_diagnostics=instance_diagnostics,
        )
        return self._enrich_instance_processes(
            instance_identity=instance_identity,
            processes=processes,
            resolved=resolved,
            identity_stage=identity_stage,
        )

    def _collect_instance_logs(
        self,
        *,
        instance_identity: InstanceIdentity,
        instance_id: str,
        store: StateStore,
        processes: list[ProcessInfo],
        adapter_results: list[AdapterResult],
    ) -> InstanceLogStage:
        session_for_pid = {
            process.pid: process.session_id for process in processes if process.session_id
        }
        events_by_session: dict[str, list[NormalizedEvent]] = defaultdict(list)
        log_activity_by_session: dict[str, float] = {}
        cutoff = int(time.time()) - self.machine.lookback_seconds
        log_started = time.monotonic()
        logs_result = store.logs_since_result(
            [process.pid for process in processes], self.log_cursors[instance_identity], cutoff
        )
        adapter_results.append(logs_result)
        logs = (
            list(logs_result.value or [])
            if logs_result.status
            in {
                AdapterStatus.PRESENT,
                AdapterStatus.ABSENT,
                AdapterStatus.INCOMPLETE,
            }
            else []
        )
        self.collectors.record(
            f"log_db:{instance_id}",
            log_started,
            (
                logs_result.error_code
                if logs_result.status in {AdapterStatus.FAILED, AdapterStatus.INCOMPLETE}
                else None
                if store.capabilities.logs
                else "sqlite_unsupported"
            ),
        )
        if logs:
            self.log_cursors[instance_identity] = max(record.log_id for record in logs)
        for record in logs:
            session_id = record.thread_id
            if not session_id:
                match = re.match(r"pid:(\d+):", record.process_uuid)
                session_id = session_for_pid.get(int(match.group(1)), "") if match else ""
            if session_id:
                observed_at = time.time()
                log_activity_by_session[session_id] = observed_at
                events_by_session[session_id].extend(
                    replace(event, observed_at=observed_at) for event in normalize_log(record)
                )

        return InstanceLogStage(
            events_by_session=dict(events_by_session),
            activity_by_session=log_activity_by_session,
        )

    def _collect_session_evidence(
        self,
        *,
        session_key: SessionIdentity,
        process: ProcessInfo,
        candidates: list[ProcessInfo],
        incoming: list[NormalizedEvent],
        instance_diagnostics: list[Diagnostic | str],
    ) -> SessionEvidenceStage:
        rollout_activities: list[RolloutActivity] = []
        seen_rollouts: set[str] = set()
        observed_at = time.time()
        process_children: dict[str, object] = {}
        process_tree_available = False
        process_tree_sampled_at: list[float] = []
        for candidate in candidates:
            if candidate.activity.available:
                process_tree_available = True
                if candidate.activity.sampled_at is not None:
                    process_tree_sampled_at.append(candidate.activity.sampled_at)
                process_children.update(
                    {child.identity.key: child for child in candidate.activity.children}
                )
            if candidate.rollout_path and candidate.rollout_path not in seen_rollouts:
                seen_rollouts.add(candidate.rollout_path)
                rollout_result = self.rollouts.read_with_activity(Path(candidate.rollout_path))
                rollout_activities.append(rollout_result.activity)
                incoming.extend(rollout_result.events)
                self.terminals.apply(
                    session_key,
                    rollout_result.terminal_updates,
                )
            if candidate.activity.available:
                file_updates = self.terminal_files.read(
                    session_key,
                    candidate.cwd,
                    candidate.activity.children,
                    observed_at,
                )
                self.terminals.apply(session_key, file_updates)
                for diagnostic in self.terminal_files.pop_diagnostics(session_key):
                    fds = ",".join(str(fd) for fd in diagnostic.fds)
                    instance_diagnostics.append(
                        "regular-file tail 校验失败："
                        f"reason={diagnostic.reason}; pid={diagnostic.pid}; "
                        f"start_time={diagnostic.start_time}; fd={fds}"
                    )
        if process_tree_available:
            self.terminals.reconcile_children(
                session_key,
                tuple(process_children.values()),
                observed_at,
                evidence_cutoff=(min(process_tree_sampled_at) if process_tree_sampled_at else None),
                workspace=process.cwd,
            )
        elif any(candidate.role == "session" for candidate in candidates):
            self.terminals.mark_process_unavailable(session_key)
        rollout_activity = max(
            rollout_activities,
            key=lambda item: (
                item.last_growth_at or 0.0,
                item.changed,
                item.observed_at,
            ),
            default=RolloutActivity(process.rollout_path, observed_at),
        )
        return SessionEvidenceStage(
            incoming=incoming,
            rollout_activities=rollout_activities,
            rollout_activity=rollout_activity,
            process_tree_available=process_tree_available,
        )

    def _assess_session_network(
        self,
        *,
        process: ProcessInfo,
        candidates: list[ProcessInfo],
        incoming: list[NormalizedEvent],
        socket_stage: SocketStage,
        now_monotonic: float,
    ) -> NetworkEvidence:
        if all(candidate.role == "app-server" for candidate in candidates):
            return NetworkEvidence(
                state=NetworkState.UNKNOWN,
                reason="共享 app-server 的 TCP 连接尚未关联到具体会话",
            )
        socket_by_pid = socket_stage.by_pid
        sockets_stale = socket_stage.stale
        before = [
            socket
            for candidate in candidates
            for socket in self.previous_sockets.get(candidate.stable_key, [])
        ]
        after = [
            socket for candidate in candidates for socket in socket_by_pid.get(candidate.pid, [])
        ]
        network = assess_process_network(before, after, self.idle_threshold)
        if sockets_stale:
            network.stale = True
            network.stale_age_seconds = (
                now_monotonic - self.socket_stale_since
                if self.socket_stale_since is not None
                else 0.0
            )
            network.reason = f"{network.reason}（TCP 数据已过期）"
        recent_progress = any(
            event.kind in PROGRESS_KINDS and event.timestamp >= time.time() - self.interval * 1.5
            for event in incoming
        )
        network, self.stall_windows[process.stable_key] = confirm_process_stall(
            network,
            self.stall_windows[process.stable_key],
            recent_protocol_progress=recent_progress,
        )
        return network

    def _update_session_evidence_state(
        self,
        *,
        session_key: SessionIdentity,
        rollout_activities: list[RolloutActivity],
        process_tree_available: bool,
        incoming: list[NormalizedEvent],
        sockets_stale: bool,
    ) -> None:
        association = self.terminals.association_summary(session_key)
        association_complete = not any(
            (
                association.ambiguous,
                association.conflicting,
                association.unresolved,
                association.private_state_dropped,
            )
        )
        for activity in rollout_activities:
            self.machine.update_coverage(
                session_key,
                self._evidence_coverage(
                    [activity],
                    bootstrap_truncated=self.rollouts.has_truncated_context({activity.path}),
                ),
            )
        self.machine.update_coverage(
            session_key,
            self._evidence_coverage(
                rollout_activities,
                bootstrap_truncated=False,
                track_source=False,
                terminal_probe_complete=(
                    process_tree_available
                    and association_complete
                    and self.discovery_stale_since is None
                ),
                network_probe_complete=not sockets_stale,
                silence_probe_complete=(
                    process_tree_available
                    and self.discovery_stale_since is None
                    and not sockets_stale
                ),
            ),
        )
        self.machine.ingest(session_key, incoming)

    def _derive_session_health(
        self,
        *,
        session_key: SessionIdentity,
        process: ProcessInfo,
        incoming: list[NormalizedEvent],
        rollout_activity: RolloutActivity,
        network: NetworkEvidence,
        log_activity_at: float | None,
        sockets_stale: bool,
    ) -> SessionHealth:
        previous = self.live_sessions.get(session_key)
        observation = self._observation_pulse(
            previous,
            process,
            incoming,
            rollout_activity,
            network,
            log_activity_at,
            full_sample=True,
            process_stale=self.discovery_stale_since is not None,
            network_stale=sockets_stale,
        )
        session = self.machine.derive(
            session_key,
            process,
            network,
            observation=observation,
        )
        if session.observation.last_evidence_at is not None and (
            previous is None
            or session.observation.last_evidence_at > (previous.observation.last_evidence_at or 0.0)
        ):
            self.machine.observe_compaction(
                session_key,
                timestamp=session.observation.last_evidence_at,
                source=session.observation.last_evidence_source or "observation",
                detail=session.observation.last_evidence_detail,
            )
            session = self.machine.derive(
                session_key,
                process,
                network,
                observation=observation,
            )
        recovery_states = {
            "SUSPECT",
            "RECONNECTING",
            "TRANSPORT_FALLBACK",
        }
        was_recovering = session.recovery.value in recovery_states or bool(
            previous and previous.recovery.value in recovery_states
        )
        if network.state == NetworkState.ACTIVE and was_recovering:
            recovered = NormalizedEvent(
                timestamp=time.time(),
                kind="RECOVERED",
                summary="连接已恢复",
                detail="TCP 传输重新出现进展",
                source="detector",
                confidence=Confidence.MEDIUM,
                source_id=(f"network-recovered:{process.stable_key}:{int(time.time() * 1000)}"),
                observed_at=time.time(),
            )
            self.machine.ingest(session_key, [recovered])
            session = self.machine.derive(
                session_key,
                process,
                network,
                observation=observation,
            )
        session = self._attach_terminal_snapshot(session, session_key)
        session = self._attach_ingress_diagnosis(session, rollout_activity)
        return session

    def _collect_instance_sessions(
        self,
        *,
        instance_identity: InstanceIdentity,
        instance_id: str,
        processes: list[ProcessInfo],
        events_by_session: dict[str, list[NormalizedEvent]],
        log_activity_by_session: dict[str, float],
        instance_diagnostics: list[Diagnostic | str],
        codex_config: CodexConfigSnapshot,
        socket_stage: SocketStage,
        now_monotonic: float,
    ) -> InstanceSessionStage:
        active_session_keys: set[SessionIdentity] = set()
        socket_by_pid = socket_stage.by_pid
        sockets_stale = socket_stage.stale
        session_processes: dict[str, list[ProcessInfo]] = defaultdict(list)
        for process in processes:
            if process.role in {"session", "app-server"} and process.session_id:
                session_processes[process.session_id].append(process)

        sessions = []
        instance_rollout_activity: list[dict[str, object]] = []
        rollout_started = time.monotonic()
        for session_id, candidates in session_processes.items():
            process = max(
                candidates,
                key=lambda item: (
                    {True: 2, None: 1, False: 0}[item.foreground_active],
                    item.activity.active,
                    item.activity.sampled_at or 0.0,
                    bool(item.rollout_path),
                    item.identity.start_time,
                    item.pid,
                ),
            )
            distinct_pids = sorted({item.pid for item in candidates})
            if len(distinct_pids) > 1:
                pids = ", ".join(str(pid) for pid in distinct_pids)
                instance_diagnostics.append(
                    f"检测到同一会话由 {len(distinct_pids)} 个 Codex 进程打开；"
                    f"列表已合并，当前显示 PID {process.pid}（进程 {pids}）"
                )

            session_key = SessionIdentity(instance_identity, session_id)
            evidence_stage = self._collect_session_evidence(
                session_key=session_key,
                process=process,
                candidates=candidates,
                incoming=list(events_by_session.get(session_id, [])),
                instance_diagnostics=instance_diagnostics,
            )
            incoming = evidence_stage.incoming
            rollout_activities = evidence_stage.rollout_activities
            rollout_activity = evidence_stage.rollout_activity
            process_tree_available = evidence_stage.process_tree_available
            instance_rollout_activity.extend(
                self._rollout_activity_value(item) for item in rollout_activities
            )
            incoming = self._with_compact_config(
                incoming,
                codex_config.auto_compact_token_limit,
                codex_config.auto_compact_token_limit_scope,
            )
            if session_key in self.retired_sessions:
                incoming.append(
                    NormalizedEvent(
                        timestamp=time.time(),
                        kind="PROCESS_RESUMED",
                        summary="进程已重新启动",
                        detail=f"当前 PID {process.pid}",
                        source="process",
                        confidence=Confidence.HIGH,
                        source_id=f"process-resumed:{process.stable_key}",
                        observed_at=time.time(),
                    )
                )
            active_session_keys.add(session_key)
            network = self._assess_session_network(
                process=process,
                candidates=candidates,
                incoming=incoming,
                socket_stage=socket_stage,
                now_monotonic=now_monotonic,
            )
            self._update_session_evidence_state(
                session_key=session_key,
                rollout_activities=rollout_activities,
                process_tree_available=process_tree_available,
                incoming=incoming,
                sockets_stale=sockets_stale,
            )
            session = self._derive_session_health(
                session_key=session_key,
                process=process,
                incoming=incoming,
                rollout_activity=rollout_activity,
                network=network,
                log_activity_at=log_activity_by_session.get(session_id),
                sockets_stale=sockets_stale,
            )
            sessions.append(session)
            for candidate in candidates:
                self.previous_sockets[candidate.stable_key] = socket_by_pid.get(candidate.pid, [])
        self.collectors.record(f"rollout:{instance_id}", rollout_started)
        return InstanceSessionStage(
            sessions=sessions,
            rollout_activity=instance_rollout_activity,
            active_session_keys=active_session_keys,
        )

    def _collect_instance_snapshot(
        self,
        *,
        instance_identity: InstanceIdentity,
        processes: list[ProcessInfo],
        discovery_stage: DiscoveryStage,
        socket_stage: SocketStage,
        now_monotonic: float,
    ) -> tuple[InstanceSnapshot, set[SessionIdentity], set[str]]:
        resolved = discovery_stage.resolved_by_instance[instance_identity]
        instance_id = processes[0].instance_id
        instance_diagnostics: list[Diagnostic | str] = []
        if instance_identity in discovery_stage.identity_collisions:
            instance_diagnostics.append(
                make_diagnostic(
                    "IDENTITY_COLLISION",
                    severity="fatal",
                    domain="identity",
                    source="process_discovery",
                    message_key="identity_collision",
                )
            )
        codex_config = self.codex_configs.read(resolved.paths.codex_home)
        if codex_config.error:
            instance_diagnostics.append(f"config.toml 读取失败：{codex_config.error}")
        if resolved.method == "unresolved":
            instance_diagnostics.append("进程环境与活动文件不可读，路径按默认值推测")
        process_stage = self._prepare_instance_processes(
            instance_identity=instance_identity,
            instance_id=instance_id,
            processes=processes,
            resolved=resolved,
            instance_diagnostics=instance_diagnostics,
        )
        store = process_stage.store
        adapter_results = process_stage.adapter_results
        enriched = process_stage.processes
        active_rollouts = process_stage.rollout_paths
        instance_rollouts = process_stage.rollout_paths

        log_stage = self._collect_instance_logs(
            instance_identity=instance_identity,
            instance_id=instance_id,
            store=store,
            processes=enriched,
            adapter_results=adapter_results,
        )
        events_by_session = log_stage.events_by_session
        log_activity_by_session = log_stage.activity_by_session

        session_stage = self._collect_instance_sessions(
            instance_identity=instance_identity,
            instance_id=instance_id,
            processes=enriched,
            events_by_session=events_by_session,
            log_activity_by_session=log_activity_by_session,
            instance_diagnostics=instance_diagnostics,
            codex_config=codex_config,
            socket_stage=socket_stage,
            now_monotonic=now_monotonic,
        )
        sessions = session_stage.sessions
        shared_owners: dict[str, list[SessionHealth]] = defaultdict(list)
        for session in sessions:
            if session.process.role == "app-server":
                shared_owners[session.process.stable_key].append(session)
        for owners in shared_owners.values():
            activity = self.process_activity.snapshot(owners[0].process.identity)
            if activity.available:
                owned_children = self.terminals.reconcile_shared_children(
                    tuple(session.session_identity for session in owners),
                    activity.children, activity.sampled_at or time.time(),
                )
                for session in owners:
                    key = session.session_identity
                    updates = self.terminal_files.read(
                        key, session.process.cwd, owned_children[key], time.time(),
                    )
                    self.terminals.apply(key, updates)
            else:
                for session in owners:
                    self.terminals.mark_process_unavailable(session.session_identity)
        sessions = [
            self._attach_terminal_snapshot(session, session.session_identity)
            if session.process.role == "app-server" else session
            for session in sessions
        ]
        instance_rollout_activity = session_stage.rollout_activity
        active_session_keys = session_stage.active_session_keys

        collector_health = [
            item
            for item in self.collectors.snapshot()
            if item.name in {"process", "socket"} or item.name.endswith(f":{instance_id}")
        ]

        instance_snapshot = InstanceSnapshot(
            instance_id=instance_id,
            paths=resolved.paths,
            display_codex_home=compact_path(resolved.paths.codex_home),
            identity=instance_identity,
            display_sqlite_home=compact_path(resolved.paths.sqlite_home),
            discovery_method=resolved.method,
            capabilities=store.capabilities,
            protocol_capabilities=self._merge_protocol_capabilities(sessions),
            collector_health=collector_health,
            adapter_results=tuple(adapter_results),
            diagnostics=instance_diagnostics,
            unknown_event_types=self.rollouts.unknown_counts(instance_rollouts),
            protocol_shape_families=self.rollouts.shape_counts(instance_rollouts),
            protocol_family_counters=self.rollouts.family_counter_summary(instance_rollouts),
            observed_codex_versions=self.rollouts.version_counts(instance_rollouts),
            rollout_context_truncated=(self.rollouts.has_truncated_context(instance_rollouts)),
            rollout_activity=instance_rollout_activity,
            process_data_stale_age_seconds=(
                now_monotonic - self.discovery_stale_since
                if self.discovery_stale_since is not None
                else None
            ),
            socket_data_stale_age_seconds=(
                now_monotonic - self.socket_stale_since
                if self.socket_stale_since is not None
                else None
            ),
            auto_compact_token_limit=codex_config.auto_compact_token_limit,
            auto_compact_token_limit_scope=(codex_config.auto_compact_token_limit_scope),
            compact_prompt_overridden=codex_config.compact_prompt_overridden,
            auto_compact_config_source=codex_config.source,
            processes=enriched,
            sessions=sessions,
        )
        return instance_snapshot, active_session_keys, active_rollouts
